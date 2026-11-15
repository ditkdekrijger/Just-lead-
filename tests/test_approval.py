import datetime as dt
from pathlib import Path

from PIL import Image

from adagent.analysis import kpis
from adagent.approval import approve, pending_ads, publish_approved, reject
from adagent.models import Action
from adagent.runner import run_client

from .conftest import START


def test_setup_waits_for_approval_then_publishes(session, make_client):
    c = make_client(creative_approval=True)
    run = run_client(session, c, START)
    assert "klaargezet voor akkoord" in run.report and "/akkoord/" in run.report
    assert all(x.status == "pending" and not x.external_id for x in c.campaigns)
    waiting = pending_ads(c)
    assert waiting
    # Beelden voor Meta en LinkedIn, geen voor Google Search
    for ad in waiting:
        if ad.campaign.platform == "meta":
            assert set(ad.assets) == {"4x5", "1x1"}
            assert Image.open(ad.assets["4x5"]).size == (1080, 1350)
        if ad.campaign.platform == "google":
            assert ad.assets == {}
        assert ad.visual.get("headline")

    # Dagelijkse run zonder akkoord: niets live, geen uitgaven
    run_client(session, c, START + dt.timedelta(days=1))
    assert all(x.status == "pending" for x in c.campaigns)
    assert kpis(session, campaign_ids=[x.id for x in c.campaigns], start=START, end=START + dt.timedelta(days=1)).spend == 0

    # Akkoord op één campagne → alleen die gaat live
    camp = c.campaigns[0]
    for ad in camp.ads:
        approve(session, c, ad, by="Test")
    msgs = publish_approved(session, c, START + dt.timedelta(days=1))
    assert msgs and camp.status == "active" and camp.external_id and camp.launched_on == START + dt.timedelta(days=1)
    assert all(x.status == "pending" for x in c.campaigns[1:])


def test_reject_creates_new_version_with_feedback(session, make_client):
    c = make_client(creative_approval=True, themes=["Warmtepomp"])
    run_client(session, c, START)
    ad = pending_ads(c)[0]
    new = reject(session, c, ad, "Te schreeuwerig, noem liever de subsidie", by="Klant")
    assert ad.status == "rejected" and new is not None
    assert new.approval == "pending" and new.generation == ad.generation + 1
    assert "subsidie" in new.feedback
    assert session.query(Action).filter_by(type="reject_ad").count() == 1


def test_auto_approve_after_timeout(session, make_client):
    c = make_client(creative_approval=True, approval_timeout_hours=24, themes=["Warmtepomp"])
    run_client(session, c, START)
    for ad in pending_ads(c):
        ad.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
    run = run_client(session, c, START + dt.timedelta(days=1))
    assert "automatisch goedgekeurd" in run.report
    assert not pending_ads(c)
    assert any(x.status == "active" for x in c.campaigns)


def test_new_variants_during_optimisation_wait_for_approval(session, make_client):
    c = make_client(creative_approval=False)
    for i in range(6):
        run_client(session, c, START + dt.timedelta(days=i))
    c.creative_approval = True
    for i in range(6, 14):
        run_client(session, c, START + dt.timedelta(days=i))
    created = [a for x in c.campaigns for a in x.ads if a.generation > 1]
    assert created and all(a.status in ("pending", "rejected") or a.approval != "pending" for a in created)
    assert any(a.status == "pending" for a in created)
    # Nooit meer dan één wachtende variant per campagne
    for x in c.campaigns:
        assert sum(a.status == "pending" for a in x.ads) <= 1


def test_creative_files_use_brand_and_photo(session, make_client, tmp_path):
    from adagent.creative import VisualBrief, render

    photo = tmp_path / "foto.jpg"
    Image.new("RGB", (800, 600), (40, 120, 90)).save(photo)
    c = make_client(brand_color="#123456", accent_color="#ffcc00", photos=[str(photo)])
    b = VisualBrief(headline="Een hele lange kop die echt moet passen op het beeld", subline="Ondertitel",
                    badge="Nieuw", bullets=["Een", "Twee", "Drie"], cta="Bekijk", template="statement")
    for tpl in ("statement", "checklist", "photo"):
        b.template = tpl
        for fmt, size in {"1x1": (1080, 1080), "4x5": (1080, 1350), "191x100": (1200, 628)}.items():
            im = render(c, b, fmt, str(photo))
            assert im.size == size
    assert render(c, b.model_copy(update={"template": "statement"}), "1x1").getpixel((5, 5)) == (0x12, 0x34, 0x56)
