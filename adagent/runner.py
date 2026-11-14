"""Orkestratie: setup bij nieuwe klant, dagelijkse analyse + optimalisatie voor bestaande klanten."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import llm
from .analysis import snapshot, sync_metrics
from .clock import today as _today
from .config import settings
from .copywriting import make_copy
from .executor import Executor, GuardrailError
from .fmt import action_nl, euro, num
from .models import OBJECTIVE_LABELS, PLATFORM_LABELS, Action, AgentRun, Campaign, Client, Metric
from .optimizer import propose
from .planner import launch_plan

log = logging.getLogger(__name__)
RESYNC_DAYS = 3  # conversies worden vaak met vertraging toegerekend
MAX_BACKFILL = 7


def _days_to_sync(session: Session, client: Client, today: dt.date) -> list[dt.date]:
    launches = [c.launched_on for c in client.campaigns if c.launched_on]
    if not launches:
        return []
    yesterday = today - dt.timedelta(days=1)
    last = session.scalar(
        select(func.max(Metric.date)).join(Campaign).where(Campaign.client_id == client.id)
    )
    start = min(
        (last + dt.timedelta(days=1)) if last else min(launches),
        today - dt.timedelta(days=RESYNC_DAYS),
    )
    start = max(start, min(launches), today - dt.timedelta(days=MAX_BACKFILL))
    return [start + dt.timedelta(days=i) for i in range((yesterday - start).days + 1)]


def _covered_themes(camp: Campaign) -> set[str]:
    return {camp.theme.strip().lower()} | {a.theme.strip().lower() for a in camp.ads if a.theme}


def _sync_themes(session: Session, client: Client, today: dt.date, run: AgentRun) -> list[str]:
    """Nieuwe thema's lanceren, verwijderde thema's pauzeren."""
    from .executor import set_client_paused

    wanted = {t.strip().lower() for t in client.themes if t.strip()}
    live = [c for c in client.campaigns if c.status != "error"]
    covered = set().union(*(_covered_themes(c) for c in live)) if live else set()
    notes = []
    new = [t for t in client.themes if t.strip() and t.strip().lower() not in covered]
    if new:
        launch_plan(session, client, today, run.id, themes=new)
        notes.append("Nieuwe thema's gelanceerd: " + ", ".join(new))
    gone = [c for c in live if c.status == "active" and not (_covered_themes(c) & wanted)]
    if gone:
        set_client_paused(session, client, True, by="theme", only=gone)
        notes.append("Campagnes gepauzeerd voor verwijderde thema's: " + ", ".join(c.theme for c in gone))
    if new or gone:
        session.flush()
    return notes


def _apply_rules(ex: Executor, client: Client, proposals: list[dict]) -> None:
    for p in proposals:
        try:
            if p["type"] == "pause_campaign":
                ex.pause_campaign(p["campaign_id"], p["reason"])
            elif p["type"] == "set_budget":
                ex.set_budget(p["campaign_id"], p["value"], p["reason"])
            elif p["type"] == "pause_ad":
                ex.pause_ad(p["ad_id"], p["reason"])
            elif p["type"] == "create_ad":
                camp = ex.s.get(Campaign, p["campaign_id"])
                avoid = [a.headlines[0] for a in camp.ads if a.headlines]
                copy = make_copy(client, p["theme"], camp.objective, camp.platform,
                                 variant=len(camp.ads), learnings=p.get("learnings", ""), avoid=avoid)
                ex.create_ad(p["campaign_id"], copy, p["reason"], theme=p["theme"])
        except GuardrailError as e:
            log.info("Voorstel overgeslagen: %s", e)


def _rules_report(client: Client, snap: dict, run: AgentRun) -> str:
    t = snap["totals_last_7_days"]
    pc = snap["pacing"]
    lines = [
        f"Dagrapport {client.name} — {snap['date']}",
        "",
        "Resultaat afgelopen 7 dagen",
        f"Besteed {euro(t['spend'])} · {num(t['impressions'])} vertoningen · {num(t['clicks'])} kliks · "
        f"{num(t['leads'])} leads · {num(t['conversions'])} sales (omzet {euro(t['revenue'])}).",
        f"Maand: {euro(pc['spent_mtd'])} van {euro(pc['monthly_budget'])} besteed; "
        f"doel per dag nu {euro(pc['target_daily_total'])} ({pc['days_left']} dagen te gaan).",
        "",
        "Per doel",
    ]
    for o, b in snap["benchmarks"].items():
        ref = b["reference"]
        lines.append(f"- {OBJECTIVE_LABELS[o]}: referentie {b['kpi'].upper()} "
                     + (num(ref, 2) if ref else "nog onbekend") + (" (klantdoel)" if b["target"] else ""))
    applied = [a for a in run.actions if a.status == "applied"]
    lines += ["", "Aanpassingen vandaag"]
    lines += [f"- {action_nl(a.type)}: {a.message or a.reason}" for a in applied] or ["- Geen; campagnes verzamelen nog data of presteren conform plan."]
    proposed = [a for a in run.actions if a.status == "proposed"]
    if proposed:
        lines += ["", "Voorstellen (autopilot staat uit)"]
        lines += [f"- {action_nl(a.type)}: {a.reason}" for a in proposed]
    return "\n".join(lines)


def run_client(session: Session, client: Client, today: dt.date | None = None, kind: str = "daily") -> AgentRun:
    today = today or _today()
    run = AgentRun(client_id=client.id, run_date=today, kind=kind,
                   mode="ai" if settings.ai_enabled else "rules")
    session.add(run)
    session.flush()
    try:
        if not client.campaigns:
            run.kind = "setup"
            camps = launch_plan(session, client, today, run.id)
            session.flush()
            ok = [c for c in camps if c.status != "error"]
            lines = [f"Opstart {client.name} — {today.isoformat()}", "",
                     f"{len(ok)} campagnes aangemaakt ({'live' if client.autopilot else 'gepauzeerd, autopilot uit'}), "
                     f"totaal {euro(sum(c.daily_budget for c in ok))} per dag.", ""]
            for c in camps:
                lines.append(f"- {PLATFORM_LABELS[c.platform]} · {OBJECTIVE_LABELS[c.objective]} · {c.theme}: "
                             f"{euro(c.daily_budget)}/dag, {len(c.ads)} advertenties"
                             + (f" — FOUT: {c.error[:200]}" if c.status == "error" else ""))
            strat = session.scalar(select(Action).where(Action.run_id == run.id, Action.type == "strategy"))
            if strat:
                lines += ["", "Strategie", strat.reason]
            run.report = "\n".join(lines)
            run.status = "done"
            return run

        theme_notes = _sync_themes(session, client, today, run)
        errors = []
        for d in _days_to_sync(session, client, today):
            errors += sync_metrics(session, client, d)
        snap = snapshot(session, client, today)
        proposals = propose(snap)
        run.analysis = {"snapshot": snap, "proposals": proposals, "sync_errors": errors}
        ex = Executor(session, client, run, today, snap["pacing"]["target_daily_total"])

        if client.status == "paused":
            run.report = "Klant staat op pauze; alleen cijfers bijgewerkt."
        elif not client.autopilot:
            for p in proposals:
                session.add(Action(client_id=client.id, run_id=run.id, campaign_id=p.get("campaign_id"),
                                   ad_id=p.get("ad_id"), type=p["type"], params=p, reason=p["reason"],
                                   status="proposed"))
            session.flush()
            session.refresh(run)
            run.report = _rules_report(client, snap, run)
        else:
            report = ""
            if settings.ai_enabled:
                try:
                    from .agent import run_agent

                    report = run_agent(client, snap, proposals, ex)
                except llm.LLMError as e:
                    log.warning("AI-agent faalde, terugval op regels: %s", e)
                    run.mode = "rules"
                    run.error = str(e)
            if run.mode == "rules":
                _apply_rules(ex, client, proposals)
            ex.enforce_pacing()
            session.flush()
            session.refresh(run)
            run.report = report or _rules_report(client, snap, run)
        if theme_notes:
            run.report = "\n".join(theme_notes) + "\n\n" + run.report
        if errors:
            run.report += "\n\nLet op — cijfers niet volledig opgehaald:\n" + "\n".join(f"- {e}" for e in errors[:10])
        run.status = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("Run mislukt voor %s", client.name)
        run.status = "error"
        run.error = str(e)[:4000]
    finally:
        run.finished_at = dt.datetime.now(dt.timezone.utc)
    return run


def run_all(session_factory, today: dt.date | None = None) -> list[int]:
    """Dagelijkse job: alle klanten, elk in een eigen transactie."""
    ids = []
    with session_factory() as s:
        client_ids = [c for (c,) in s.execute(select(Client.id).where(Client.status != "archived"))]
    for cid in client_ids:
        with session_factory() as s:
            client = s.get(Client, cid)
            run = run_client(s, client, today)
            s.commit()
            ids.append(run.id)
    return ids
