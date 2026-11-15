import datetime as dt

import pytest

from adagent.config import settings
from adagent.copywriting import AdCopy
from adagent.executor import Executor, GuardrailError
from adagent.models import Action
from adagent.runner import run_client

from .conftest import START


def _simulate(session, client, days):
    runs = []
    for i in range(days + 1):
        runs.append(run_client(session, client, START + dt.timedelta(days=i)))
        session.flush()
    return runs


def test_setup_launches_campaigns(session, make_client):
    c = make_client()
    run = run_client(session, c, START)
    assert run.kind == "setup" and run.status == "done"
    assert c.campaigns and all(x.status == "active" and x.external_id for x in c.campaigns)
    assert all(len(x.ads) >= 1 for x in c.campaigns)
    daily = c.monthly_budget / 31
    assert abs(sum(x.daily_budget for x in c.campaigns) - daily) < daily * 0.1


def test_full_month_stays_within_budget(session, make_client):
    c = make_client()
    runs = _simulate(session, c, 30)
    assert all(r.status == "done" for r in runs), [r.error for r in runs if r.error]
    from adagent.analysis import kpis

    spent = kpis(session, campaign_ids=[x.id for x in c.campaigns], start=START, end=START + dt.timedelta(days=29)).spend
    assert spent <= c.monthly_budget * 1.03
    # Geen enkele dag ver boven het dagbudget (bewaakt ook tegen dubbeltelling bij opnieuw ophalen)
    for i in range(30):
        day = START + dt.timedelta(days=i)
        assert kpis(session, campaign_ids=[x.id for x in c.campaigns], start=day, end=day).spend <= c.monthly_budget / 31 * 1.25
    assert spent >= c.monthly_budget * 0.8
    applied = session.query(Action).filter(Action.status == "applied", Action.type == "set_budget").count()
    assert applied > 0
    # Elke actieve campagne heeft nog minstens één actieve advertentie
    for x in c.campaigns:
        if x.status == "active":
            assert any(a.status == "active" for a in x.ads)


def test_no_changes_during_learning_phase(session, make_client):
    c = make_client()
    _simulate(session, c, settings.learning_days - 1)
    assert session.query(Action).filter(Action.type.in_(["set_budget", "pause_campaign", "pause_ad"]),
                                        Action.status == "applied").count() == 0


def test_guardrails(session, make_client):
    c = make_client()
    run_client(session, c, START)
    today = START + dt.timedelta(days=10)
    ex = Executor(session, c, None, today, target_daily_total=sum(x.daily_budget for x in c.campaigns) * 0.95)
    camp = c.campaigns[0]
    with pytest.raises(GuardrailError, match="Wijziging groter"):
        ex.set_budget(camp.id, camp.daily_budget * 3, "test")
    with pytest.raises(GuardrailError, match="pacing-plafond"):
        ex.set_budget(camp.id, camp.daily_budget * 1.15, "test")
    with pytest.raises(GuardrailError, match="Minimum"):
        ex.set_budget(camp.id, 0.5, "test")
    lone = next(x for x in c.campaigns if len(x.ads) >= 1)
    for a in lone.ads[1:]:
        a.status = "paused"
    with pytest.raises(GuardrailError, match="laatste actieve"):
        ex.pause_ad(lone.ads[0].id, "test")
    with pytest.raises(GuardrailError, match="bestaat niet"):
        ex.pause_campaign(99999, "test")
    # Leerfase
    ex2 = Executor(session, c, None, START + dt.timedelta(days=1), 10_000)
    with pytest.raises(GuardrailError, match="leerfase"):
        ex2.pause_campaign(camp.id, "test")
    # Maximaal aantal nieuwe ads per dag
    ex.new_ads_today = settings.max_new_ads_per_day
    with pytest.raises(GuardrailError, match="nieuwe advertenties"):
        ex.create_ad(camp.id, AdCopy(headlines=["Nieuw"], descriptions=["d"], primary_text="p"), "test")
    assert session.query(Action).filter(Action.status == "rejected").count() >= 6


def test_enforce_pacing_scales_down(session, make_client):
    c = make_client()
    run_client(session, c, START)
    total = sum(x.daily_budget for x in c.campaigns)
    ex = Executor(session, c, None, START + dt.timedelta(days=5), target_daily_total=total / 2)
    ex.enforce_pacing()
    assert sum(x.daily_budget for x in c.campaigns if x.status == "active") <= total / 2 * 1.05


def test_autopilot_off_only_proposes(session, make_client):
    c = make_client(autopilot=False)
    run_client(session, c, START)
    assert all(x.status == "paused" for x in c.campaigns)


def test_theme_changes(session, make_client):
    c = make_client(themes=["Warmtepomp"])
    _simulate(session, c, 4)
    n = len(c.campaigns)
    c.themes = ["Zonnepanelen"]
    run = run_client(session, c, START + dt.timedelta(days=5))
    assert "Nieuwe thema's" in run.report
    assert len(c.campaigns) > n
    old = [x for x in c.campaigns if x.theme == "Warmtepomp"]
    assert old and all(x.status == "paused" for x in old)
    assert sum(x.daily_budget for x in c.campaigns if x.status == "active") <= c.monthly_budget / 31 * 1.06


def test_ai_agent_path(session, make_client, monkeypatch):
    """De AI-agent krijgt tools; we simuleren Claude en checken dat acties via de guardrails lopen."""
    from adagent import agent, llm

    c = make_client()
    _simulate(session, c, 5)
    monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))
    calls = []

    def fake_loop(system, prompt, tools, handler, **kw):
        assert {t["name"] for t in tools} >= {"set_campaign_budget", "create_ad_variant", "pause_ad"}
        cid = c.campaigns[0].id
        calls.append(handler("set_campaign_budget", {"campaign_id": cid, "new_daily_budget": 9999, "reason": "x"}))
        calls.append(handler("create_ad_variant", {
            "campaign_id": cid, "theme": "Warmtepomp", "headlines": ["Bespaar met een warmtepomp", "Gratis advies"],
            "descriptions": ["Vraag vandaag advies aan."], "primary_text": "Nieuwe invalshoek", "reason": "test"}))
        return "Dagrapport: alles onder controle."

    monkeypatch.setattr(llm, "run_tool_loop", fake_loop)
    monkeypatch.setattr(agent.llm, "run_tool_loop", fake_loop)
    run = run_client(session, c, START + dt.timedelta(days=6))
    assert run.mode == "ai" and run.status == "done"
    assert calls[0].startswith("GEWEIGERD")
    assert "Nieuwe advertentie" in calls[1]
    assert run.report.startswith("Dagrapport")


def test_ai_failure_falls_back_to_rules(session, make_client, monkeypatch):
    from adagent import llm

    c = make_client()
    _simulate(session, c, 5)
    monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))

    def boom(*a, **k):
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "run_tool_loop", boom)
    monkeypatch.setattr(llm, "structured", boom)
    run = run_client(session, c, START + dt.timedelta(days=6))
    assert run.status == "done" and run.mode == "rules" and "down" in run.error
