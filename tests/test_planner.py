from adagent.copywriting import AdCopy, enforce, template_copy
from adagent.planner import MIN_USEFUL_BUDGET, allocate, default_weights
from adagent.platforms import copy_limits_for


def test_allocate_respects_budget_and_minimums(make_client):
    c = make_client()
    shares, fit = default_weights(c)
    specs = allocate(c, 100.0, shares, fit)
    assert specs
    assert abs(sum(s.daily_budget for s in specs) - 100.0) < 1.0
    for s in specs:
        assert s.daily_budget >= MIN_USEFUL_BUDGET[s.platform] - 0.01


def test_small_budget_consolidates_themes_and_drops_combos(make_client):
    c = make_client(monthly_budget=300, themes=["A", "B", "C"])
    shares, fit = default_weights(c)
    specs = allocate(c, 10.0, shares, fit)
    assert 1 <= len(specs) <= 2
    assert all(s.platform != "linkedin" for s in specs)  # €10/dag is te weinig voor LinkedIn naast de rest
    assert all(len(s.themes) == 3 for s in specs)


def test_b2b_boosts_linkedin(make_client):
    b2c = make_client(description="Webshop voor kinderkleding")
    b2b = make_client(description="SaaS voor HR-managers in het MKB")
    assert default_weights(b2b)[1]["leads"]["linkedin"] > default_weights(b2c)[1]["leads"]["linkedin"]


def test_copy_limits_enforced(make_client):
    c = make_client()
    lim = copy_limits_for("google")
    copy = enforce(AdCopy(headlines=["x" * 50, "Kort", "kort"], descriptions=["y" * 200], primary_text="z"), lim,
                   fallback=template_copy(c, "Warmtepomp", "leads", "google"))
    assert all(len(h) <= 30 for h in copy.headlines)
    assert len(copy.headlines) >= 3
    assert len({h.lower() for h in copy.headlines}) == len(copy.headlines)
    assert all(len(d) <= 90 for d in copy.descriptions) and len(copy.descriptions) >= 2


def test_template_variants_differ(make_client):
    c = make_client()
    heads = {template_copy(c, "Warmtepomp", "leads", "meta", v).headlines[0] for v in range(6)}
    assert len(heads) == 6
