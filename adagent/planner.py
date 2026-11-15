"""Van budget + thema's naar een concreet campagneplan per platform en doel."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import llm
from .clock import days_in_month
from .config import settings
from .approval import prepare_ad
from .copywriting import CTA_BY_OBJECTIVE, AdCopy, enforce, make_copy, template_copy
from .models import OBJECTIVE_LABELS, OBJECTIVES, PLATFORM_LABELS, PLATFORMS, Action, Ad, Campaign, Client
from .platforms import MIN_DAILY_BUDGET, PlatformError, copy_limits_for, get_connector

log = logging.getLogger(__name__)

# Standaard funnelverdeling en platform-fit per doel (worden door AI per klant verfijnd)
DEFAULT_OBJECTIVE_SHARE = {"reach": 0.15, "clicks": 0.20, "leads": 0.35, "sales": 0.30}
DEFAULT_PLATFORM_FIT = {
    "reach": {"meta": 0.60, "google": 0.15, "linkedin": 0.25},
    "clicks": {"meta": 0.45, "google": 0.40, "linkedin": 0.15},
    "leads": {"meta": 0.35, "google": 0.40, "linkedin": 0.25},
    "sales": {"meta": 0.45, "google": 0.50, "linkedin": 0.05},
}
B2B_HINTS = ("b2b", "zakelijk", "bedrijven", "mkb", "ondernemer", "hr", "directeur", "manager", "beslisser", "saas")
#: Minimaal zinvol dagbudget per campagne (genoeg data om te leren)
MIN_USEFUL_BUDGET = {"meta": 5.0, "google": 5.0, "linkedin": 10.0}


@dataclass
class CampaignSpec:
    platform: str
    objective: str
    theme: str
    daily_budget: float
    themes: list[str]
    targeting: dict = field(default_factory=dict)


# ---------- AI-strategie ----------
class PlatformWeights(BaseModel):
    meta: float = Field(ge=0, le=1)
    google: float = Field(ge=0, le=1)
    linkedin: float = Field(ge=0, le=1)


class ObjectiveStrategy(BaseModel):
    objective: str = Field(description="reach | clicks | leads | sales")
    budget_share: float = Field(ge=0, le=1)
    platform_weights: PlatformWeights


class ThemeCopy(BaseModel):
    theme: str
    google_keywords: list[str] = Field(description="10-20 zoekwoorden (phrase match) met koopintentie")
    meta: AdCopy
    google: AdCopy
    linkedin: AdCopy


class StrategyPlan(BaseModel):
    business_model: str = Field(description="B2B, B2C of beide")
    rationale: str = Field(description="Korte onderbouwing van de verdeling (Nederlands, max 80 woorden)")
    age_min: int = Field(ge=18, le=65)
    age_max: int = Field(ge=18, le=65)
    objectives: list[ObjectiveStrategy]
    themes: list[ThemeCopy]


STRATEGY_SYSTEM = (
    "Je bent strateeg bij JUST.LEAD (powermarketing). Je verdeelt mediabudget over funnel-doelen "
    "(bereik, kliks, leads, sales) en platformen (Meta, Google Ads, LinkedIn) op basis van het "
    "verdienmodel, de doelgroep en de thema's van de klant, en schrijft per thema startcopy. "
    "Wees realistisch: LinkedIn is duur en alleen zinvol bij zakelijke doelgroepen; Google Search "
    "vangt bestaande vraag; Meta is sterk voor bereik en B2C-conversie."
)


def ai_strategy(client: Client, themes: list[str]) -> StrategyPlan:
    prompt = f"""Maak een mediaplan voor deze klant.

Klant: {client.name} — {client.website}
Omschrijving: {client.description or '-'}
Doelgroep: {client.audience or '-'}
Regio: {', '.join(client.locations or ['NL'])} — taal: {client.language}
Maandbudget: €{client.monthly_budget:,.0f}
Thema's: {'; '.join(themes)}
Gekozen doelen: {', '.join(OBJECTIVE_LABELS[o] for o in client.objectives)} (gebruik exact: {', '.join(client.objectives)})
Gekozen platformen: {', '.join(PLATFORM_LABELS[p] for p in client.platforms)} (niet-gekozen platformen gewicht 0)

Lever per gekozen doel een budgetaandeel (som 1) en platformgewichten, en per thema copy voor alle drie platformen.
Copylimieten — Google: koppen ≤30 tekens (10-15 stuks), beschrijvingen ≤90 (4 stuks). Meta: kop ≤40, hoofdtekst ≤250. LinkedIn: kop ≤70, intro ≤300."""
    return llm.structured(STRATEGY_SYSTEM, prompt, StrategyPlan, max_tokens=32000)


# ---------- Allocatie ----------
def _is_b2b(client: Client) -> bool:
    text = f"{client.description} {client.audience}".lower()
    return any(h in text for h in B2B_HINTS)


def default_weights(client: Client) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    shares = {o: DEFAULT_OBJECTIVE_SHARE[o] for o in client.objectives}
    fit = {o: dict(DEFAULT_PLATFORM_FIT[o]) for o in client.objectives}
    if _is_b2b(client):
        for o in fit:
            fit[o]["linkedin"] *= 2.2
    return shares, fit


def allocate(client: Client, daily_total: float, shares: dict[str, float],
             fit: dict[str, dict[str, float]], themes: list[str] | None = None) -> list[CampaignSpec]:
    """Verdeel het dagbudget over (doel, platform, thema) met respect voor minimumbudgetten."""
    objectives = [o for o in client.objectives if o in OBJECTIVES]
    platforms = [p for p in client.platforms if p in PLATFORMS]
    if not objectives or not platforms or daily_total <= 0:
        return []

    weights: dict[tuple[str, str], float] = {}
    s_total = sum(max(shares.get(o, 0), 0) for o in objectives) or 1.0
    for o in objectives:
        f_total = sum(max(fit.get(o, {}).get(p, 0), 0) for p in platforms)
        for p in platforms:
            w = (max(shares.get(o, 0), 0) / s_total) * (
                (max(fit.get(o, {}).get(p, 0), 0) / f_total) if f_total else 1 / len(platforms)
            )
            if w > 0:
                weights[(o, p)] = w

    # Combinaties onder het minimum schrappen (zwakste eerst) en herverdelen
    while weights:
        total_w = sum(weights.values())
        budgets = {k: daily_total * w / total_w for k, w in weights.items()}
        short = {k: b / MIN_USEFUL_BUDGET[k[1]] for k, b in budgets.items() if b < MIN_USEFUL_BUDGET[k[1]]}
        if not short:
            break
        if len(weights) == 1:
            break
        worst = min(short, key=short.get)
        weights.pop(worst)
    if not weights:
        return []

    total_w = sum(weights.values())
    specs: list[CampaignSpec] = []
    themes = [t for t in (themes if themes is not None else client.themes) if t.strip()] or [client.name]
    for (o, p), w in sorted(weights.items(), key=lambda kv: (OBJECTIVES.index(kv[0][0]), kv[0][1])):
        budget = daily_total * w / total_w
        budget = max(budget, MIN_DAILY_BUDGET[p])
        per_theme = budget / len(themes)
        if len(themes) > 1 and per_theme >= MIN_USEFUL_BUDGET[p]:
            for t in themes:
                specs.append(CampaignSpec(p, o, t, round(per_theme, 2), [t]))
        else:
            label = themes[0] if len(themes) == 1 else "Alle thema's"
            specs.append(CampaignSpec(p, o, label, round(budget, 2), themes))
    return specs


# ---------- Voorvertoning (voor het invoerscherm) ----------
def preview_plan(client: Client, daily_total: float) -> dict:
    """Wat de agent zou bouwen met deze instellingen (zonder AI, zonder iets aan te maken)."""
    shares, fit = default_weights(client)
    specs = allocate(client, daily_total, shares, fit)
    themes = [t for t in client.themes if t.strip()]
    camps = [{
        "platform": s.platform, "objective": s.objective, "theme": s.theme,
        "daily_budget": round(s.daily_budget, 2),
        "ads": len(s.themes) * (2 if len(s.themes) <= 2 else 1),
    } for s in specs]
    notes: list[dict] = []
    used_p = {s.platform for s in specs}
    used_o = {s.objective for s in specs}
    for p in client.platforms:
        if p in PLATFORMS and p not in used_p and specs:
            notes.append({"level": "warn", "title": f"{PLATFORM_LABELS[p]} valt af.",
                          "text": f"Bij €{daily_total:.0f} per dag blijft er te weinig over voor minimaal "
                                  f"€{MIN_USEFUL_BUDGET[p]:.0f} per campagne. Verhoog het budget of kies minder doelen."})
    lost = [OBJECTIVE_LABELS[o] for o in client.objectives if o in OBJECTIVES and o not in used_o and specs]
    if lost:
        notes.append({"level": "warn", "title": f"{' en '.join(lost)} {'vallen' if len(lost) > 1 else 'valt'} af.",
                      "text": "Het budget is te klein om alle doelen goed te testen; de agent kiest voor de doelen "
                              "die het dichtst bij omzet zitten."})
    if len(themes) > 1 and any(len(s.themes) > 1 for s in specs):
        notes.append({"level": "info", "title": "Thema's samengevoegd",
                      "text": "in één campagne waar het budget per thema te klein is. Elk thema krijgt wel eigen advertenties."})
    if _is_b2b(client) and "linkedin" in used_p:
        notes.append({"level": "info", "title": "Zakelijke doelgroep herkend.",
                      "text": "LinkedIn krijgt ruim twee keer zoveel gewicht."})
    if "meta" in used_p and used_o & {"leads", "sales"} and not client.meta_pixel_id:
        notes.append({"level": "info", "title": "Meta-pixel nog leeg.",
                      "text": "Zonder pixel optimaliseren leads- en salescampagnes op kliks in plaats van conversies."})
    if specs and settings.ai_enabled:
        notes.append({"level": "info", "title": "Met Claude",
                      "text": "worden doelverdeling en teksten bij het lanceren per klant bepaald. Dit is de standaardverdeling."})
    return {"daily_total": round(daily_total, 2), "campaigns": camps, "notes": notes}


# ---------- Lancering ----------
def launch_plan(session: Session, client: Client, today: dt.date, run_id: int | None = None,
                themes: list[str] | None = None) -> list[Campaign]:
    """Bouwt het plan en zet campagnes live (of gepauzeerd als autopilot uit staat).

    Met `themes` wordt alleen voor die (nieuwe) thema's gelanceerd, met een evenredig deel van het budget;
    de pacing-bewaking schaalt de bestaande campagnes daarna mee terug.
    """
    all_themes = [t for t in client.themes if t.strip()]
    themes = themes or all_themes
    share = len(themes) / len(all_themes) if all_themes else 1.0
    daily_total = client.monthly_budget / days_in_month(today) * share
    strategy: StrategyPlan | None = None
    if settings.ai_enabled:
        try:
            strategy = ai_strategy(client, themes)
        except llm.LLMError as e:
            log.warning("AI-strategie mislukt, standaardverdeling gebruikt: %s", e)

    shares, fit = default_weights(client)
    copy_by_theme: dict[str, ThemeCopy] = {}
    age = (18, 65)
    if strategy:
        for s in strategy.objectives:
            if s.objective in shares:
                shares[s.objective] = s.budget_share
                fit[s.objective] = s.platform_weights.model_dump()
        copy_by_theme = {tc.theme.strip().lower(): tc for tc in strategy.themes}
        age = (min(strategy.age_min, strategy.age_max), max(strategy.age_min, strategy.age_max))
        session.add(Action(client_id=client.id, run_id=run_id, type="strategy", status="applied",
                           reason=strategy.rationale, params={"business_model": strategy.business_model}))

    specs = allocate(client, daily_total, shares, fit, themes)
    created: list[Campaign] = []
    for spec in specs:
        camp = Campaign(
            platform=spec.platform,
            objective=spec.objective,
            theme=spec.theme,
            name=f"{client.name} | {PLATFORM_LABELS[spec.platform]} | {OBJECTIVE_LABELS[spec.objective]} | {spec.theme}"[:250],
            daily_budget=spec.daily_budget,
            status="active",
            launched_on=today,
            targeting={"age_min": age[0], "age_max": age[1]},
        )
        keywords: list[str] = []
        ads: list[Ad] = []
        limits = copy_limits_for(spec.platform)
        for theme in spec.themes:
            tc = copy_by_theme.get(theme.strip().lower())
            if tc:
                keywords += tc.google_keywords
                variants = [enforce(getattr(tc, spec.platform), limits,
                                    fallback=template_copy(client, theme, spec.objective, spec.platform))]
            else:
                keywords += [theme]
                variants = [make_copy(client, theme, spec.objective, spec.platform, variant=0)]
            # Tweede variant voor A/B-test (bij max. 2 thema's, anders worden het te veel ads)
            if len(spec.themes) <= 2:
                variants.append(template_copy(client, theme, spec.objective, spec.platform, variant=1))
            for v in variants:
                ads.append(Ad(theme=theme, headlines=v.headlines, descriptions=v.descriptions,
                              primary_text=v.primary_text, cta=CTA_BY_OBJECTIVE[spec.objective],
                              landing_url=client.website))
        camp.targeting["keywords"] = list(dict.fromkeys(k for k in keywords if k))[:50]
        camp.ads = ads
        client.campaigns.append(camp)
        session.flush()  # ids nodig voor externe namen en bestandsnamen
        for ad in ads:
            prepare_ad(client, camp, ad)

        if client.creative_approval:
            # Tussenstap: niets publiceren tot het materiaal is goedgekeurd
            camp.status, camp.launched_on = "pending", None
            session.add(Action(client_id=client.id, run_id=run_id, campaign_id=camp.id, type="prepare_campaign",
                               params={"daily_budget": camp.daily_budget, "ads": len(ads)},
                               reason=f"Startplan: {OBJECTIVE_LABELS[spec.objective]} via {PLATFORM_LABELS[spec.platform]}",
                               status="applied", message=f"{len(ads)} advertenties klaargezet voor akkoord"))
            created.append(camp)
            continue
        try:
            connector = get_connector(client, spec.platform)
            connector.create_campaign(camp, ads, active=client.autopilot)
            if not client.autopilot:
                camp.status = "paused"
            session.add(Action(client_id=client.id, run_id=run_id, campaign_id=camp.id, type="launch_campaign",
                               params={"daily_budget": camp.daily_budget, "ads": len(ads)},
                               reason=f"Startplan: {OBJECTIVE_LABELS[spec.objective]} via {PLATFORM_LABELS[spec.platform]}",
                               status="applied"))
        except (PlatformError, Exception) as e:  # noqa: BLE001
            log.exception("Lancering mislukt")
            camp.status = "error"
            camp.error = str(e)[:2000]
            session.add(Action(client_id=client.id, run_id=run_id, campaign_id=camp.id, type="launch_campaign",
                               status="failed", message=str(e)[:2000]))
        created.append(camp)
    if client.status == "new" and not client.creative_approval:
        client.status = "active"
    return created
