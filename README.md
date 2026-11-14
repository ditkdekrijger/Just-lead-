# JUST.LEAD Advertentie Agent

Autonome advertentie-agent per klant voor **Meta (Facebook/Instagram)**, **Google Ads** en **LinkedIn**.
Je voert per klant **budget en thema's** in; de agent doet de rest:

1. **Strategie** – verdeelt het maandbudget over de doelen *bereik, kliks, leads, sales* en over de platformen (met Claude, of met ingebouwde vuistregels; B2B-klanten krijgen automatisch meer LinkedIn).
2. **Opbouw** – maakt campagnes, doelgroepen, zoekwoorden en advertentieteksten per platform (binnen de tekenlimieten) en lanceert ze.
3. **Dagelijkse analyse** – haalt elke ochtend de cijfers op, berekent KPI's per campagne en advertentie en bewaakt de budget-pacing.
4. **Dagelijkse verbetering** – verschuift budget naar winnaars, pauzeert verliezers, vervangt zwakke advertenties door nieuwe varianten en schrijft een dagrapport.

## Snel starten

```bash
pip install -r requirements.txt
cp .env.example .env          # standaard: sandbox-modus, geen echte uitgaven
python -m adagent demo        # voorbeeldklant + 21 gesimuleerde dagen
python -m adagent serve       # dashboard op http://localhost:8000
```

Of met Docker: `docker build -t adagent . && docker run -p 8000:8000 --env-file .env -v adagent-data:/data adagent`

## Hoe de agent beslist

| Doel   | Meta               | Google Ads                       | LinkedIn            | Primaire KPI |
|--------|--------------------|----------------------------------|---------------------|--------------|
| Bereik | OUTCOME_AWARENESS  | Search · Target Impression Share | BRAND_AWARENESS     | CPM          |
| Kliks  | OUTCOME_TRAFFIC    | Search · Maximize Clicks         | WEBSITE_VISIT       | CPC          |
| Leads  | OUTCOME_LEADS      | Search · Maximize Conversions    | LEAD_GENERATION     | CPL          |
| Sales  | OUTCOME_SALES      | Search · Maximize Conv. Value    | WEBSITE_CONVERSION  | ROAS         |

Elke dag (standaard 07:00, `ADAGENT_DAILY_HOUR`):

- **Cijfers ophalen** van gisteren, plus de 3 dagen daarvoor opnieuw (conversies komen vaak later binnen).
- **Pacing**: dagdoel = resterend maandbudget ÷ resterende dagen. Het totaal van alle dagbudgetten komt nooit boven dit doel (+5%).
- **Beoordelen**: per doel wordt elke campagne vergeleken met het KPI-doel van de klant, of — zonder doel — met de mediaan van de eigen campagnes. Alleen bij voldoende data en na de leerfase.
- **Regelvoorstellen**: budget binnen hetzelfde doel naar de best presterende campagnes (convergerend, zonder campagnes uit te hongeren), structureel slechte campagnes pauzeren, advertenties met CTR < 60% van het campagnegemiddelde vervangen, altijd ≥ 2 varianten in de test.
- **AI-agent** (met `ANTHROPIC_API_KEY`): Claude krijgt de volledige momentopname + de regelvoorstellen, beslist zelf via tools (budget, pauzeren, hervatten, nieuwe advertentie) en schrijft het dagrapport. Zonder key of bij een storing worden de regelvoorstellen direct uitgevoerd.

### Guardrails (in code, niet in de prompt)

- Totaal dagbudget ≤ pacing-plafond; laatste vangnet schaalt alles terug.
- Max. budgetwijziging per campagne per dag (`ADAGENT_MAX_DAILY_BUDGET_CHANGE`, standaard 20%) en max. één wijziging per dag.
- Geen ingrepen tijdens de leerfase (`ADAGENT_LEARNING_DAYS`, standaard 3 dagen).
- Minimumbudgetten per platform (LinkedIn €10/dag).
- Max. nieuwe advertenties per klant per dag (`ADAGENT_MAX_NEW_ADS_PER_DAY`).
- Altijd minimaal één actieve advertentie per campagne; dubbele koppen worden geweigerd.
- **Autopilot uit** per klant: campagnes starten gepauzeerd en de agent doet alleen voorstellen.

Elke actie — uitgevoerd, geweigerd of mislukt — wordt gelogd met reden en is zichtbaar in het dashboard.

## Thema's en budget wijzigen

Pas de klant aan in het dashboard. Budget gaat direct mee in de pacing. Nieuwe thema's worden bij de volgende run gelanceerd; campagnes van verwijderde thema's worden gepauzeerd.

## Live gaan (echte campagnes)

Zet `ADAGENT_MODE=live` en vul per platform de bureau-credentials in `.env` in; per klant de account-ID's in het klantformulier.

| Platform | Bureau (`.env`) | Per klant |
|---|---|---|
| Meta | `META_ACCESS_TOKEN` (System User token met `ads_management`) | ad account ID, pagina-ID, pixel-ID (voor leads/sales-optimalisatie) |
| Google Ads | developer token, OAuth client ID/secret, refresh token, MCC-ID | customer ID; conversieacties ingesteld in Google Ads |
| LinkedIn | `LINKEDIN_ACCESS_TOKEN` (Marketing API, `rw_ads`) | ad account ID, organisatie-ID, lead form ID (voor leads) |

Belangrijk vóór de eerste live klant:

- De live-koppelingen zijn gebouwd op de openbare API-documentatie en getest tegen nagebootste API's, **niet tegen echte accounts**. Start de eerste klant met **autopilot uit** (campagnes gepauzeerd), controleer de aangemaakte structuur in de platformen en zet dan autopilot aan.
- API-versies zijn instelbaar (`META_API_VERSION`, `GOOGLE_ADS_API_VERSION`, `LINKEDIN_API_VERSION`); controleer of ze nog ondersteund worden.
- Meta-advertenties zonder `image_url` gebruiken de link-preview van de website. Voor sterke creatives: voeg beeldmateriaal toe.
- Zet `ADAGENT_PASSWORD` voor het dashboard.

## JSON-API (voor koppelingen, bijv. n8n/Make)

- `POST /api/clients` — klant aanmaken en direct lanceren (`name`, `website`, `monthly_budget`, `themes`, optioneel doelen/platformen/KPI's/account-ID's)
- `GET /api/clients` — overzicht met pacing
- `GET /api/clients/{id}/snapshot` — volledige analyse
- `POST /api/clients/{id}/run` — nu analyseren en optimaliseren

## Commando's

```bash
python -m adagent serve              # dashboard + dagelijkse planner
python -m adagent run-daily          # één run voor alle klanten (voor externe cron)
python -m adagent simulate --days 14 # sandbox: dagen vooruit simuleren
python -m pytest                     # tests
```

## Structuur

```
adagent/
  planner.py      budget + thema's → campagneplan (AI-strategie of vuistregels)
  analysis.py     cijfers ophalen, KPI's, pacing, momentopname
  optimizer.py    regelgebaseerde voorstellen
  agent.py        Claude-agent met tools
  executor.py     uitvoeren met guardrails + logging
  runner.py       orkestratie per klant (setup / dagelijks)
  copywriting.py  advertentieteksten (AI + templates, platformlimieten)
  platforms/      meta.py, google_ads.py, linkedin.py, simulator.py
  web/            dashboard + JSON-API
```
