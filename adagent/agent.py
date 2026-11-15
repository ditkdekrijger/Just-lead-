"""De AI-agent: Claude analyseert de dagcijfers en stuurt bij via tools (met guardrails)."""

from __future__ import annotations

import json
import logging
from typing import Any

from . import llm
from .copywriting import AdCopy
from .executor import Executor, GuardrailError
from .models import Client

log = logging.getLogger(__name__)

SYSTEM = """Je bent de autonome advertentie-agent van JUST.LEAD (powermarketing). Je beheert de campagnes van één klant op Meta, Google Ads en LinkedIn en optimaliseert dagelijks op de doelen van de klant: bereik (CPM), kliks (CPC), leads (CPL) en sales (ROAS).

Werkwijze:
1. Lees de momentopname: pacing, benchmarks, KPI's per campagne en per advertentie.
2. Bepaal wat er vandaag moet gebeuren. De regelgebaseerde voorstellen zijn advies; neem ze over, pas ze aan of verwerp ze als de data iets anders laat zien.
3. Voer de acties uit met de tools. De tools handhaven harde grenzen (pacing-plafond, max. budgetwijziging per dag, leerfase, minimumbudgetten, max. nieuwe advertenties). Krijg je een weigering, pas je plan aan en ga door; herhaal niet dezelfde actie.
4. Sluit af met een dagrapport voor de accountmanager.

Principes:
- Statistische voorzichtigheid: oordeel niet over campagnes of advertenties zonder voldoende data (enough_data=false) of in de leerfase.
- Verschuif budget naar wat bewezen werkt, binnen hetzelfde doel; houd het totaal rond target_daily_total uit de pacing.
- Vervang zwakke advertenties door nieuwe varianten met een andere invalshoek (voordeel, pijnpunt, bewijs, urgentie); leer van de best presterende kop. Schrijf in de taal van de klant, eerlijk, zonder verzonnen claims, kortingen of prijzen.
- Verlaag budget eerst voordat je elders verhoogt.
- Elke nieuwe advertentie krijgt automatisch een bijpassend beeld. Bij klanten met een akkoordstap wordt hij pas gepubliceerd na goedkeuring; maak dan geen tweede variant zolang er een wacht. Afgewezen versies en hun feedback staan in de momentopname: leer daarvan.
- Doe niets als niets nodig is; stilstaan is ook een keuze.

Dagrapport (Nederlands, max. 200 woorden, platte tekst met korte kopjes): 1) Resultaat gisteren en afgelopen 7 dagen t.o.v. doelen, 2) Wat je vandaag hebt aangepast en waarom, 3) Aandachtspunten / advies aan de klant (bijv. landingspagina, conversietracking, extra creatives)."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_campaign_budget",
        "description": "Wijzig het dagbudget (EUR) van een actieve campagne. Max. wijziging per dag en pacing-plafond worden afgedwongen.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "new_daily_budget": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "new_daily_budget", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pause_campaign",
        "description": "Pauzeer een campagne die structureel onder de maat presteert.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["campaign_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resume_campaign",
        "description": "Hervat een gepauzeerde campagne (alleen als het binnen het pacing-plafond past).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["campaign_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pause_ad",
        "description": "Pauzeer een zwakke advertentie. Er blijft altijd minimaal één actieve advertentie per campagne.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"ad_id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["ad_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_ad_variant",
        "description": (
            "Voeg een nieuwe advertentievariant toe aan een campagne. Limieten — Google: 8-15 koppen ≤30 tekens, "
            "2-4 beschrijvingen ≤90 tekens. Meta: 1-3 koppen ≤40, 1-2 beschrijvingen ≤30, hoofdtekst ≤250. "
            "LinkedIn: 1 kop ≤70, 1 beschrijving ≤100, introtekst ≤300. Te lange teksten worden ingekort. "
            "Voor Meta en LinkedIn wordt automatisch een advertentiebeeld in de huisstijl van de klant gemaakt met "
            "visual_headline en visual_subline. Staat akkoord aan bij de klant, dan wacht de variant op goedkeuring."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "theme": {"type": "string"},
                "headlines": {"type": "array", "items": {"type": "string"}},
                "descriptions": {"type": "array", "items": {"type": "string"}},
                "primary_text": {"type": "string"},
                "visual_headline": {"type": "string", "description": "Kop op het advertentiebeeld, max 38 tekens"},
                "visual_subline": {"type": "string", "description": "Ondertitel op het beeld, max 70 tekens"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "theme", "headlines", "descriptions", "primary_text",
                         "visual_headline", "visual_subline", "reason"],
            "additionalProperties": False,
        },
    },
]


def run_agent(client: Client, snap: dict, proposals: list[dict], ex: Executor) -> str:
    def handler(name: str, args: dict[str, Any]) -> str:
        try:
            if name == "set_campaign_budget":
                return ex.set_budget(args["campaign_id"], args["new_daily_budget"], args["reason"])
            if name == "pause_campaign":
                return ex.pause_campaign(args["campaign_id"], args["reason"])
            if name == "resume_campaign":
                return ex.resume_campaign(args["campaign_id"], args["reason"])
            if name == "pause_ad":
                return ex.pause_ad(args["ad_id"], args["reason"])
            if name == "create_ad_variant":
                copy = AdCopy(headlines=args["headlines"], descriptions=args["descriptions"],
                              primary_text=args["primary_text"])
                visual = {"headline": args.get("visual_headline", ""), "subline": args.get("visual_subline", "")}
                return ex.create_ad(args["campaign_id"], copy, args["reason"], theme=args.get("theme"),
                                    visual={k: v for k, v in visual.items() if v})
        except GuardrailError as e:
            return f"GEWEIGERD: {e}"
        return f"Onbekende tool: {name}"

    prompt = (
        f"Klant: {client.name} ({client.website})\n"
        f"Omschrijving: {client.description or '-'}\nDoelgroep: {client.audience or '-'}\n\n"
        f"<momentopname>\n{json.dumps(snap, ensure_ascii=False, indent=1)}\n</momentopname>\n\n"
        f"<regelvoorstellen>\n{json.dumps(proposals, ensure_ascii=False, indent=1)}\n</regelvoorstellen>\n\n"
        "Voer de dagelijkse analyse en optimalisatie uit en sluit af met het dagrapport."
    )
    return llm.run_tool_loop(SYSTEM, prompt, TOOLS, handler)
