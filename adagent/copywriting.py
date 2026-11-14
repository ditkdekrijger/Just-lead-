"""Advertentieteksten: platformlimieten afdwingen, templates en AI-generatie."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from . import llm
from .config import settings
from .models import OBJECTIVE_LABELS, Client
from .platforms import CopyLimits, copy_limits_for

log = logging.getLogger(__name__)

CTA_BY_OBJECTIVE = {"reach": "LEARN_MORE", "clicks": "LEARN_MORE", "leads": "SIGN_UP", "sales": "SHOP_NOW"}


class AdCopy(BaseModel):
    headlines: list[str] = Field(description="Koppen; Google: 8-15 stuks, Meta/LinkedIn: 1-3")
    descriptions: list[str] = Field(description="Beschrijvingen; Google: 2-4 stuks")
    primary_text: str = Field(description="Hoofdtekst / intro-tekst (Meta/LinkedIn)")


def fit(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:-–| ")
    return cut or text[:limit]


def enforce(copy: AdCopy, limits: CopyLimits, *, fallback: AdCopy | None = None) -> AdCopy:
    """Kort teksten in tot de limieten, ontdubbel en vul aan tot het minimum."""

    def clean(items: list[str], limit: int, mx: int) -> list[str]:
        seen, out = set(), []
        for it in items:
            f = fit(it, limit)
            if f and f.lower() not in seen:
                seen.add(f.lower())
                out.append(f)
        return out[:mx]

    heads = clean(copy.headlines, limits.headline, limits.max_headlines)
    descs = clean(copy.descriptions, limits.description, limits.max_descriptions)
    if fallback is not None:
        if len(heads) < limits.min_headlines:
            heads = clean(heads + fallback.headlines, limits.headline, limits.max_headlines)
        if len(descs) < limits.min_descriptions:
            descs = clean(descs + fallback.descriptions, limits.description, limits.max_descriptions)
    return AdCopy(headlines=heads, descriptions=descs, primary_text=fit(copy.primary_text, limits.primary_text))


ANGLES = [
    # (koppen, hoofdtekst) per invalshoek; {t}=thema, {n}=klant, {o}=aanbod
    (["{t} | {n}", "{o}", "Ontdek {t}"],
     "Op zoek naar {t}? {n} helpt je verder. {o}."),
    (["Meer weten over {t}?", "Persoonlijk advies van {n}", "{o}"],
     "{t} hoeft niet ingewikkeld te zijn. Bij {n} krijg je persoonlijk advies en een aanpak die werkt. {o}."),
    (["Waarom kiezen voor {n}?", "Specialist in {t}", "Heldere afspraken"],
     "Twijfel je over {t}? Onze specialisten denken met je mee en geven eerlijk advies. {o}."),
    (["{t} zonder gedoe", "Wij regelen het voor je", "{o}"],
     "Geen gedoe met {t}: {n} regelt het van begin tot eind. {o}."),
    (["Start vandaag met {t}", "Snel geschakeld", "Ervaren team van {n}"],
     "Waarom wachten? Met {n} zet je vandaag de eerste stap richting {t}. {o}."),
    (["Klanten kiezen {n}", "{t}: zo pak je het aan", "Bekijk de mogelijkheden"],
     "Benieuwd hoe anderen {t} hebben aangepakt? Ontdek wat {n} voor jou kan betekenen. {o}."),
]


def template_copy(client: Client, theme: str, objective: str, platform: str, variant: int = 0) -> AdCopy:
    """Degelijke Nederlandstalige basiscopy zonder AI; elke variant een andere invalshoek."""
    n, t = client.name, theme.strip()
    o = {
        "reach": f"Maak kennis met {n}",
        "clicks": f"Bekijk {t} bij {n}",
        "leads": "Vraag vrijblijvend advies aan",
        "sales": "Bestel vandaag nog",
    }[objective]
    heads_tpl, primary_tpl = ANGLES[variant % len(ANGLES)]
    fmt = lambda x: x.format(t=t, n=n, o=o)  # noqa: E731
    heads = [fmt(h) for h in heads_tpl]
    # Aanvullen met overige invalshoeken (nodig voor Google: 3-15 koppen)
    for i in range(1, len(ANGLES)):
        heads += [fmt(h) for h in ANGLES[(variant + i) % len(ANGLES)][0]]
    descs = [
        f"{n} helpt je verder met {t}. {o}.",
        f"Persoonlijk advies over {t}, afgestemd op jouw situatie. Neem vandaag contact op.",
        "Ontdek waarom klanten voor ons kiezen. Heldere afspraken en snel geschakeld.",
        f"{o} en ervaar het verschil.",
    ]
    descs = descs[variant % 4:] + descs[: variant % 4]
    return enforce(AdCopy(headlines=heads, descriptions=descs, primary_text=fmt(primary_tpl)), copy_limits_for(platform))


COPY_SYSTEM = (
    "Je bent een senior performance-copywriter bij JUST.LEAD, een Nederlands powermarketing-bureau. "
    "Je schrijft scherpe, eerlijke advertentieteksten in de taal van de klant die converteren. "
    "Geen superlatieven die niet te onderbouwen zijn, geen verzonnen prijzen, kortingen of claims, "
    "en je houdt je strikt aan de platformregels (Google: geen uitroeptekens in koppen, geen HOOFDLETTERS)."
)


def ai_copy(
    client: Client,
    theme: str,
    objective: str,
    platform: str,
    *,
    learnings: str = "",
    avoid: list[str] | None = None,
) -> AdCopy:
    limits = copy_limits_for(platform)
    prompt = f"""Schrijf één nieuwe advertentievariant.

Klant: {client.name} ({client.website})
Omschrijving: {client.description or '-'}
Doelgroep: {client.audience or '-'}
Thema: {theme}
Campagnedoel: {OBJECTIVE_LABELS[objective]}
Platform: {platform}
Taal: {client.language}

Limieten (tekens): kop ≤ {limits.headline} ({limits.min_headlines}-{limits.max_headlines} stuks), beschrijving ≤ {limits.description} ({limits.min_descriptions}-{limits.max_descriptions} stuks), hoofdtekst ≤ {min(limits.primary_text, 300)}.
{('Wat we geleerd hebben uit de resultaten: ' + learnings) if learnings else ''}
{('Vermijd herhaling van deze bestaande koppen: ' + '; '.join(avoid)) if avoid else ''}"""
    copy = llm.structured(COPY_SYSTEM, prompt, AdCopy, max_tokens=4000)
    return enforce(copy, limits, fallback=template_copy(client, theme, objective, platform))


def make_copy(client: Client, theme: str, objective: str, platform: str, *, variant: int = 0,
              learnings: str = "", avoid: list[str] | None = None) -> AdCopy:
    if settings.ai_enabled:
        try:
            return ai_copy(client, theme, objective, platform, learnings=learnings, avoid=avoid)
        except llm.LLMError as e:
            log.warning("AI-copy mislukt, template gebruikt: %s", e)
    return template_copy(client, theme, objective, platform, variant)
