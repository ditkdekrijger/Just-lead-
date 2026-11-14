"""Voert acties uit op de platformen — mét harde guardrails.

Alles wat de AI of de regels willen doen loopt hierdoor. De guardrails staan in code,
niet in de prompt: budgetplafond, maximale wijziging per dag, leerfase, minimumbudgetten,
maximaal aantal nieuwe advertenties en altijd minimaal één actieve advertentie.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import in_learning
from .config import settings
from .fmt import euro
from .copywriting import CTA_BY_OBJECTIVE, AdCopy, enforce, template_copy
from .models import Action, Ad, AgentRun, Campaign, Client
from .platforms import MIN_DAILY_BUDGET, PlatformError, copy_limits_for, get_connector

log = logging.getLogger(__name__)
PACING_TOLERANCE = 1.05


class GuardrailError(ValueError):
    pass


class Executor:
    def __init__(self, session: Session, client: Client, run: AgentRun | None, today: dt.date,
                 target_daily_total: float):
        self.s = session
        self.client = client
        self.run = run
        self.today = today
        self.target_total = target_daily_total
        self._connectors: dict = {}
        start = dt.datetime.combine(today, dt.time.min, tzinfo=dt.timezone.utc)
        self.new_ads_today = self.s.scalar(
            select(func.count(Action.id)).where(
                Action.client_id == client.id, Action.type == "create_ad",
                Action.status == "applied", Action.created_at >= start)
        ) or 0

    # ---------- helpers ----------
    def _conn(self, platform: str):
        if platform not in self._connectors:
            self._connectors[platform] = get_connector(self.client, platform)
        return self._connectors[platform]

    def _campaign(self, campaign_id: int) -> Campaign:
        camp = self.s.get(Campaign, int(campaign_id))
        if camp is None or camp.client_id != self.client.id:
            raise GuardrailError(f"Campagne {campaign_id} bestaat niet voor deze klant")
        return camp

    def _ad(self, ad_id: int) -> Ad:
        ad = self.s.get(Ad, int(ad_id))
        if ad is None or ad.campaign.client_id != self.client.id:
            raise GuardrailError(f"Advertentie {ad_id} bestaat niet voor deze klant")
        return ad

    def _log(self, type_: str, *, campaign: Campaign | None = None, ad: Ad | None = None,
             params: dict | None = None, reason: str = "", status: str = "applied", message: str = "") -> Action:
        a = Action(client_id=self.client.id, run_id=self.run.id if self.run else None,
                   campaign_id=campaign.id if campaign else None, ad_id=ad.id if ad else None,
                   type=type_, params=params or {}, reason=reason, status=status, message=message)
        self.s.add(a)
        self.s.flush()
        return a

    def active_total(self) -> float:
        return sum(c.daily_budget for c in self.client.campaigns if c.status == "active")

    def _guarded(self, type_: str, fn, *, campaign=None, ad=None, params=None, reason=""):
        try:
            msg = fn()
        except GuardrailError as e:
            self._log(type_, campaign=campaign, ad=ad, params=params, reason=reason, status="rejected", message=str(e))
            raise
        except (PlatformError, Exception) as e:  # noqa: BLE001
            log.exception("Actie %s mislukt", type_)
            self._log(type_, campaign=campaign, ad=ad, params=params, reason=reason, status="failed", message=str(e)[:2000])
            raise GuardrailError(f"Platformfout: {e}") from e
        self._log(type_, campaign=campaign, ad=ad, params=params, reason=reason, message=msg)
        return msg

    # ---------- acties ----------
    def set_budget(self, campaign_id: int, new_budget: float, reason: str, *, pacing: bool = False) -> str:
        camp = self._campaign(campaign_id)
        new_budget = round(float(new_budget), 2)
        old = camp.daily_budget

        def do() -> str:
            if camp.status != "active":
                raise GuardrailError("Campagne is niet actief")
            if camp.last_budget_change == self.today and not pacing:
                raise GuardrailError("Budget is vandaag al aangepast")
            if in_learning(camp, self.today) and not pacing:
                raise GuardrailError("Campagne zit nog in de leerfase; geen budgetwijziging")
            mn = MIN_DAILY_BUDGET[camp.platform]
            if new_budget < mn:
                raise GuardrailError(f"Minimum dagbudget voor {camp.platform} is €{mn:.2f}")
            m = settings.max_daily_budget_change
            if old > 0 and abs(new_budget / old - 1) > m + 0.005 and not (pacing and new_budget < old):
                raise GuardrailError(f"Wijziging groter dan {m:.0%} per dag (van €{old:.2f} naar €{new_budget:.2f})")
            if new_budget > old:
                total_after = self.active_total() - old + new_budget
                if total_after > self.target_total * PACING_TOLERANCE:
                    raise GuardrailError(
                        f"Totaal dagbudget zou €{total_after:.2f} worden; pacing-plafond is €{self.target_total:.2f}"
                    )
            self._conn(camp.platform).set_daily_budget(camp, new_budget)
            camp.daily_budget = new_budget
            camp.last_budget_change = self.today
            return f"Budget {camp.name}: {euro(old)} → {euro(new_budget)}"

        return self._guarded("set_budget", do, campaign=camp, params={"from": old, "to": new_budget}, reason=reason)

    def pause_campaign(self, campaign_id: int, reason: str) -> str:
        camp = self._campaign(campaign_id)

        def do() -> str:
            if camp.status != "active":
                raise GuardrailError("Campagne is al gepauzeerd")
            if in_learning(camp, self.today):
                raise GuardrailError("Campagne zit nog in de leerfase; eerst data verzamelen")
            self._conn(camp.platform).set_campaign_status(camp, False)
            camp.status = "paused"
            camp.paused_by = "agent"
            return f"Campagne gepauzeerd: {camp.name}"

        return self._guarded("pause_campaign", do, campaign=camp, reason=reason)

    def resume_campaign(self, campaign_id: int, reason: str) -> str:
        camp = self._campaign(campaign_id)

        def do() -> str:
            if camp.status != "paused":
                raise GuardrailError("Campagne is niet gepauzeerd")
            if self.active_total() + camp.daily_budget > self.target_total * PACING_TOLERANCE:
                raise GuardrailError("Hervatten past niet binnen het pacing-plafond; verlaag eerst andere budgetten")
            self._conn(camp.platform).set_campaign_status(camp, True)
            camp.status = "active"
            camp.paused_by = ""
            return f"Campagne hervat: {camp.name}"

        return self._guarded("resume_campaign", do, campaign=camp, reason=reason)

    def pause_ad(self, ad_id: int, reason: str) -> str:
        ad = self._ad(ad_id)
        camp = ad.campaign

        def do() -> str:
            if ad.status != "active":
                raise GuardrailError("Advertentie is al gepauzeerd")
            others = [a for a in camp.ads if a.status == "active" and a.id != ad.id]
            if not others:
                raise GuardrailError("Dit is de laatste actieve advertentie; maak eerst een nieuwe variant")
            self._conn(camp.platform).set_ad_status(camp, ad, False)
            ad.status = "paused"
            return f"Advertentie {ad.id} gepauzeerd in {camp.name}"

        return self._guarded("pause_ad", do, campaign=camp, ad=ad, reason=reason)

    def create_ad(self, campaign_id: int, copy: AdCopy, reason: str, theme: str | None = None) -> str:
        camp = self._campaign(campaign_id)
        theme = theme or camp.theme
        limits = copy_limits_for(camp.platform)
        fitted = enforce(copy, limits, fallback=template_copy(self.client, theme, camp.objective, camp.platform))
        params = fitted.model_dump()

        def do() -> str:
            if self.new_ads_today >= settings.max_new_ads_per_day:
                raise GuardrailError(f"Maximaal {settings.max_new_ads_per_day} nieuwe advertenties per dag")
            if camp.status == "error":
                raise GuardrailError("Campagne staat in foutstatus")
            existing = {h.lower() for a in camp.ads for h in a.headlines[:1]}
            if fitted.headlines and fitted.headlines[0].lower() in existing:
                raise GuardrailError("Deze kop bestaat al in de campagne; schrijf een echt nieuwe invalshoek")
            gen = max((a.generation for a in camp.ads), default=0) + 1
            ad = Ad(campaign_id=camp.id, theme=theme, headlines=fitted.headlines, descriptions=fitted.descriptions,
                    primary_text=fitted.primary_text, cta=CTA_BY_OBJECTIVE[camp.objective],
                    landing_url=self.client.website, generation=gen)
            camp.ads.append(ad)
            self.s.flush()
            self._conn(camp.platform).create_ad(camp, ad)
            self.new_ads_today += 1
            return f"Nieuwe advertentie {ad.id} (generatie {gen}) in {camp.name}"

        return self._guarded("create_ad", do, campaign=camp, params=params, reason=reason)

    # ---------- harde pacing-bewaking ----------
    def enforce_pacing(self) -> str | None:
        """Laatste vangnet: het totale dagbudget mag nooit boven het pacing-plafond uitkomen."""
        total = self.active_total()
        cap = self.target_total * PACING_TOLERANCE
        if total <= cap or total == 0:
            return None
        factor = self.target_total / total
        msgs = []
        for c in [c for c in self.client.campaigns if c.status == "active"]:
            new = max(round(c.daily_budget * factor, 2), MIN_DAILY_BUDGET[c.platform])
            if new < c.daily_budget - 0.01:
                try:
                    msgs.append(self.set_budget(c.id, new, "Pacing: maandbudget bewaken", pacing=True))
                except GuardrailError as e:
                    msgs.append(str(e))
        # Als minimumbudgetten het plafond nog steeds breken: zwakste (laatst gestarte) campagnes pauzeren
        while self.active_total() > cap:
            active = [c for c in self.client.campaigns if c.status == "active"]
            if len(active) <= 1:
                break
            victim = max(active, key=lambda c: (c.launched_on or self.today, c.id))
            victim.status = "paused"
            victim.paused_by = "agent"
            try:
                self._conn(victim.platform).set_campaign_status(victim, False)
                self._log("pause_campaign", campaign=victim, reason="Pacing: maandbudget is (bijna) op")
            except Exception as e:  # noqa: BLE001
                self._log("pause_campaign", campaign=victim, status="failed", message=str(e)[:2000])
        return "; ".join(msgs)


def set_client_paused(session: Session, client: Client, paused: bool, *, by: str = "user",
                      only: list[Campaign] | None = None) -> list[str]:
    """Pauzeer of hervat (een deel van) de campagnes van een klant op de platformen."""
    msgs = []
    conns: dict = {}
    targets = only if only is not None else client.campaigns
    for c in targets:
        if paused and c.status != "active":
            continue
        if not paused and not (c.status == "paused" and c.paused_by == by):
            continue
        try:
            conn = conns.setdefault(c.platform, get_connector(client, c.platform))
            conn.set_campaign_status(c, not paused)
            c.status = "paused" if paused else "active"
            c.paused_by = by if paused else ""
            status, msg = "applied", c.name
        except Exception as e:  # noqa: BLE001
            status, msg = "failed", str(e)[:2000]
        session.add(Action(client_id=client.id, campaign_id=c.id, type="pause_campaign" if paused else "resume_campaign",
                           reason={"user": "Handmatig via dashboard", "theme": "Thema verwijderd"}.get(by, by),
                           status=status, message=msg))
        msgs.append(msg)
    if only is None:
        client.status = "paused" if paused else "active"
    return msgs
