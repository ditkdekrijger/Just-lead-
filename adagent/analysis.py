"""Cijfers ophalen, KPI's berekenen en budget-pacing bepalen."""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clock import days_in_month, month_start
from .config import settings
from .models import Ad, Campaign, Client, Metric
from .platforms import PlatformError, get_connector

log = logging.getLogger(__name__)

#: Hoger = beter? (per doel de primaire KPI)
PRIMARY_KPI = {"reach": ("cpm", False), "clicks": ("cpc", False), "leads": ("cpl", False), "sales": ("roas", True)}
#: Minimale data voordat de agent een campagne/advertentie beoordeelt
MIN_IMPRESSIONS = {"meta": 1500, "google": 300, "linkedin": 800}
MIN_SPEND = 15.0


# ---------- Synchronisatie ----------
def sync_metrics(session: Session, client: Client, day: dt.date) -> list[str]:
    """Haalt cijfers van `day` op voor alle campagnes. Geeft foutmeldingen terug."""
    errors: list[str] = []
    for camp in client.campaigns:
        if not camp.external_id or camp.status == "error":
            continue
        if camp.launched_on and day < camp.launched_on:
            continue
        if not settings.live and session.scalar(
            select(func.count(Metric.id)).where(Metric.campaign_id == camp.id, Metric.date == day)
        ):
            # Sandbox: een dag is na de eerste keer 'historie' (zoals bij echte platformen); niet opnieuw genereren
            continue
        try:
            stats = get_connector(client, camp.platform).fetch_stats(camp, day)
        except (PlatformError, Exception) as e:  # noqa: BLE001
            log.warning("Cijfers ophalen mislukt voor %s: %s", camp.name, e)
            errors.append(f"{camp.name}: {e}")
            continue
        by_ext = {a.external_id: a for a in camp.ads if a.external_id}
        for s in stats:
            ad = by_ext.get(s.ad_external_id or "")
            ad_id = ad.id if ad else None
            row = session.scalar(
                select(Metric).where(Metric.campaign_id == camp.id, Metric.date == day,
                                     Metric.ad_id.is_(None) if ad_id is None else Metric.ad_id == ad_id)
            )
            if row is None:
                row = Metric(campaign_id=camp.id, ad_id=ad_id, date=day)
                session.add(row)
            for f in ("impressions", "reach", "clicks", "spend", "leads", "conversions", "revenue"):
                setattr(row, f, getattr(s, f))
    session.flush()
    return errors


# ---------- KPI's ----------
@dataclass
class KPIs:
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    spend: float = 0.0
    leads: float = 0.0
    conversions: float = 0.0
    revenue: float = 0.0

    @property
    def ctr(self) -> float | None:
        return self.clicks / self.impressions if self.impressions else None

    @property
    def cpm(self) -> float | None:
        return self.spend / self.impressions * 1000 if self.impressions else None

    @property
    def cpc(self) -> float | None:
        return self.spend / self.clicks if self.clicks else None

    @property
    def cpl(self) -> float | None:
        return self.spend / self.leads if self.leads else None

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions else None

    @property
    def roas(self) -> float | None:
        return self.revenue / self.spend if self.spend else None

    def results(self, objective: str) -> float:
        return {"reach": self.reach, "clicks": self.clicks, "leads": self.leads, "sales": self.conversions}[objective]

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("ctr", "cpm", "cpc", "cpl", "cpa", "roas"):
            v = getattr(self, k)
            d[k] = round(v, 4 if k == "ctr" else 2) if v is not None else None
        d["spend"] = round(self.spend, 2)
        d["leads"] = round(self.leads, 1)
        d["conversions"] = round(self.conversions, 1)
        d["revenue"] = round(self.revenue, 2)
        return d


def kpis(session: Session, *, campaign_id: int | None = None, ad_id: int | None = None,
         campaign_ids: list[int] | None = None, start: dt.date, end: dt.date) -> KPIs:
    cols = [func.coalesce(func.sum(getattr(Metric, f)), 0) for f in
            ("impressions", "reach", "clicks", "spend", "leads", "conversions", "revenue")]
    q = select(*cols).where(Metric.date >= start, Metric.date <= end)
    if ad_id is not None:
        q = q.where(Metric.ad_id == ad_id)
    elif campaign_id is not None:
        q = q.where(Metric.campaign_id == campaign_id)
    elif campaign_ids is not None:
        if not campaign_ids:
            return KPIs()
        q = q.where(Metric.campaign_id.in_(campaign_ids))
    row = session.execute(q).one()
    return KPIs(int(row[0]), int(row[1]), int(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[6]))


# ---------- Pacing ----------
@dataclass
class Pacing:
    monthly_budget: float
    spent_mtd: float
    days_left: int  # inclusief vandaag
    target_daily_total: float
    current_daily_total: float

    def to_dict(self) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in asdict(self).items()}


#: Het dagdoel mag maximaal zoveel keer het gemiddelde dagbudget zijn (geen inhaalrace aan het eind van de maand)
MAX_CATCH_UP = 1.3


def month_budget(client: Client, today: dt.date) -> float:
    """Budget voor de lopende maand; in de startmaand naar rato van de resterende dagen."""
    dim = days_in_month(today)
    launches = [c.launched_on for c in client.campaigns if c.launched_on]
    first = min(launches) if launches else today
    if first.year == today.year and first.month == today.month and first.day > 1:
        return client.monthly_budget * (dim - first.day + 1) / dim
    return client.monthly_budget


def pacing(session: Session, client: Client, today: dt.date) -> Pacing:
    ids = [c.id for c in client.campaigns]
    spent = kpis(session, campaign_ids=ids, start=month_start(today), end=today - dt.timedelta(days=1)).spend
    dim = days_in_month(today)
    days_left = dim - today.day + 1
    budget = month_budget(client, today)
    remaining = max(budget - spent, 0.0)
    target = min(remaining / days_left, client.monthly_budget / dim * MAX_CATCH_UP)
    current = sum(c.daily_budget for c in client.campaigns if c.status == "active")
    return Pacing(round(budget, 2), spent, days_left, target, current)


# ---------- Momentopname voor optimizer en AI ----------
def in_learning(camp: Campaign, today: dt.date) -> bool:
    return bool(camp.launched_on and (today - camp.launched_on).days < settings.learning_days)


def benchmark(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None and v > 0]
    return statistics.median(vals) if vals else None


def snapshot(session: Session, client: Client, today: dt.date) -> dict:
    """Volledig beeld van de klant: pacing + KPI's per campagne en advertentie."""
    yesterday = today - dt.timedelta(days=1)
    week = today - dt.timedelta(days=7)
    camps = []
    for c in client.campaigns:
        if c.status == "error":
            continue
        k7 = kpis(session, campaign_id=c.id, start=week, end=yesterday)
        k1 = kpis(session, campaign_id=c.id, start=yesterday, end=yesterday)
        ktot = kpis(session, campaign_id=c.id, start=c.launched_on or week, end=yesterday)
        ads = []
        for a in c.ads:
            ka = kpis(session, ad_id=a.id, start=week, end=yesterday)
            ads.append({
                "ad_id": a.id, "status": a.status, "theme": a.theme, "generation": a.generation,
                "headline": (a.headlines or [""])[0], "primary_text": a.primary_text[:160],
                "last_7_days": ka.to_dict(),
                "enough_data": ka.impressions >= MIN_IMPRESSIONS[c.platform],
            })
        kpi_name, higher = PRIMARY_KPI[c.objective]
        camps.append({
            "campaign_id": c.id, "name": c.name, "platform": c.platform, "objective": c.objective,
            "theme": c.theme, "status": c.status, "daily_budget": round(c.daily_budget, 2),
            "days_live": (today - c.launched_on).days if c.launched_on else 0,
            "in_learning_phase": in_learning(c, today),
            "budget_changed_today": c.last_budget_change == today,
            "primary_kpi": kpi_name, "primary_kpi_higher_is_better": higher,
            "yesterday": k1.to_dict(), "last_7_days": k7.to_dict(), "since_launch": ktot.to_dict(),
            "enough_data": k7.impressions >= MIN_IMPRESSIONS[c.platform] and k7.spend >= MIN_SPEND,
            "ads": ads,
        })
    # Benchmarks per doel: klantdoel indien opgegeven, anders mediaan van eigen campagnes
    bench = {}
    for o in client.objectives:
        kpi_name, _ = PRIMARY_KPI[o]
        target = client.target_for(o)
        observed = benchmark([c["last_7_days"][kpi_name] for c in camps if c["objective"] == o and c["enough_data"]])
        bench[o] = {"kpi": kpi_name, "target": target, "observed_median": observed, "reference": target or observed}
    total7 = kpis(session, campaign_ids=[c.id for c in client.campaigns], start=week, end=yesterday)
    return {
        "client": {"id": client.id, "name": client.name, "objectives": client.objectives,
                   "platforms": client.platforms, "themes": client.themes},
        "date": today.isoformat(),
        "pacing": pacing(session, client, today).to_dict(),
        "benchmarks": bench,
        "totals_last_7_days": total7.to_dict(),
        "campaigns": camps,
    }
