"""Regelgebaseerde optimalisatie (werkt altijd, ook zonder AI).

Produceert voorstellen; de AI-agent gebruikt ze als advies, zonder AI worden ze direct uitgevoerd.
"""

from __future__ import annotations

import statistics

from .config import settings
from .platforms import MIN_DAILY_BUDGET

PAUSE_SCORE = 0.4
AD_CTR_RATIO = 0.6
#: Kleinere wijzigingen laten we liggen (minder onrust in de leeralgoritmes van platformen)
MIN_CHANGE = 0.08
#: Grenzen voor het budgetgewicht van een campagne binnen zijn doel
MIN_WEIGHT, MAX_WEIGHT = 0.4, 2.5


def campaign_score(c: dict, bench: dict) -> float | None:
    """>1 = beter dan referentie, <1 = slechter. None = (nog) niet te beoordelen."""
    if c["in_learning_phase"] or not c["enough_data"]:
        return None
    b = bench.get(c["objective"]) or {}
    ref = b.get("reference")
    if not ref:
        return None
    k7 = c["last_7_days"]
    value = k7.get(c["primary_kpi"])
    if c["primary_kpi_higher_is_better"]:
        return (value or 0.0) / ref
    if value is None:
        # Geen enkel resultaat: score afhankelijk van hoeveel er al uitgegeven is
        return 0.0 if k7["spend"] >= 2 * ref else None
    return ref / value


def propose(snap: dict) -> list[dict]:
    m = settings.max_daily_budget_change
    bench = snap["benchmarks"]
    target_total = snap["pacing"]["target_daily_total"]
    active = [c for c in snap["campaigns"] if c["status"] == "active"]
    proposals: list[dict] = []
    scores = {c["campaign_id"]: campaign_score(c, bench) for c in active}

    # 1) Structureel slechte campagnes pauzeren (nooit de laatste van een doel)
    paused: set[int] = set()
    for c in active:
        s = scores[c["campaign_id"]]
        if s is None or s >= PAUSE_SCORE:
            continue
        same_obj = [x for x in active if x["objective"] == c["objective"] and x["campaign_id"] not in paused]
        if len(same_obj) <= 1:
            continue
        paused.add(c["campaign_id"])
        proposals.append({
            "type": "pause_campaign", "campaign_id": c["campaign_id"],
            "reason": f"{c['primary_kpi'].upper()} structureel {1 / max(s, 0.01):.1f}x slechter dan referentie "
                      f"(7 dagen, €{c['last_7_days']['spend']:.0f} besteed)",
        })

    # 2) Budget herverdelen binnen elk doel (funnelmix blijft intact) + pacing richting maandbudget.
    #    Campagnes zonder oordeel behouden hun aandeel, zodat ze niet uitgehongerd raken.
    remaining = [c for c in active if c["campaign_id"] not in paused]
    current_total = sum(c["daily_budget"] for c in active) or 1.0
    pace = target_total / current_total if target_total > 0 else 1.0
    for objective in {c["objective"] for c in remaining}:
        group = [c for c in remaining if c["objective"] == objective]
        freed = sum(c["daily_budget"] for c in active if c["objective"] == objective and c["campaign_id"] in paused)
        pool = (sum(c["daily_budget"] for c in group) + freed) * pace
        # Campagnes zonder oordeel: budget alleen mee met pacing (niet uithongeren)
        fixed = {c["campaign_id"]: c["daily_budget"] * pace for c in group if scores[c["campaign_id"]] is None}
        scored = [c for c in group if scores[c["campaign_id"]] is not None]
        targets = dict(fixed)
        if scored:
            # Convergerend doel-aandeel (geen dagelijkse stapeling): gewicht = prestatie t.o.v. de mediaan
            # binnen het doel, begrensd; campagnes die het klantdoel halen houden een fatsoenlijk aandeel.
            med = statistics.median(scores[c["campaign_id"]] for c in scored) or 1.0
            weights = {}
            for c in scored:
                sc = scores[c["campaign_id"]]
                w = min(max(sc / med, MIN_WEIGHT), MAX_WEIGHT)
                if sc >= 1.0:
                    w = max(w, 0.75)
                weights[c["campaign_id"]] = w
            scored_pool = max(pool - sum(fixed.values()), 0.0)
            w_total = sum(weights.values()) or 1.0
            targets.update({cid: scored_pool * w / w_total for cid, w in weights.items()})
        for c in group:
            cid, old = c["campaign_id"], c["daily_budget"]
            if c["in_learning_phase"] or c["budget_changed_today"]:
                continue
            new = targets[cid]
            # Iets binnen de grens blijven i.v.m. afronding
            new = min(max(new, old * (1 - m) + 0.01), old * (1 + m) - 0.01)
            new = round(max(new, MIN_DAILY_BUDGET[c["platform"]]), 2)
            if old and abs(new / old - 1) >= MIN_CHANGE:
                s = scores[cid]
                why = (f"prestatiescore {s:.2f} binnen doel" if s is not None else "nog geen oordeel") + (
                    f"; pacing naar €{target_total:.2f}/dag totaal" + (f", €{freed:.2f} vrijgekomen" if freed else ""))
                proposals.append({"type": "set_budget", "campaign_id": cid, "value": new, "from": old, "reason": why})

    # 3) Advertenties: zwakke varianten vervangen, altijd minimaal 2 varianten testen
    for c in remaining:
        if c["in_learning_phase"]:
            continue
        live_ads = [a for a in c["ads"] if a["status"] == "active"]
        judged = [a for a in live_ads if a["enough_data"] and a["last_7_days"]["ctr"] is not None]
        camp_ctr = c["last_7_days"]["ctr"]
        refresh = False
        if len(judged) >= 2 and camp_ctr:
            for a in judged:
                if a["last_7_days"]["ctr"] < AD_CTR_RATIO * camp_ctr:
                    proposals.append({
                        "type": "pause_ad", "ad_id": a["ad_id"], "campaign_id": c["campaign_id"],
                        "reason": f"CTR {a['last_7_days']['ctr']:.2%} vs campagne {camp_ctr:.2%}",
                    })
                    refresh = True
        waiting = any(a["status"] == "pending" for a in c["ads"])
        if (refresh or len(live_ads) < 2) and not waiting:
            best = max(judged, key=lambda a: a["last_7_days"]["ctr"] or 0, default=None)
            proposals.append({
                "type": "create_ad", "campaign_id": c["campaign_id"], "theme": c["theme"],
                "learnings": (f"Best presterende kop tot nu toe: '{best['headline']}' (CTR {best['last_7_days']['ctr']:.2%})"
                              if best else ""),
                "reason": "Nieuwe variant om te testen tegen de huidige winnaar",
            })
    # create_ad voor pause_ad uitvoeren, zodat er nooit een campagne zonder advertentie ontstaat
    order = {"pause_campaign": 0, "create_ad": 1, "pause_ad": 2, "set_budget": 3}
    # Budgetverlagingen vóór verhogingen, zodat het pacing-plafond nooit tijdelijk breekt
    return sorted(proposals, key=lambda p: (order[p["type"]], p.get("value", 0) - p.get("from", 0)))
