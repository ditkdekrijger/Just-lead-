"""Live-koppelingen tegen een nagebootste API: controleert request-vorm en parsing."""

import datetime as dt
import json
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest

from adagent.config import settings
from adagent.models import Ad, Campaign


@pytest.fixture()
def creds(monkeypatch):
    for k, v in dict(meta_access_token="tok", google_developer_token="dev", google_client_id="id",
                     google_client_secret="sec", google_refresh_token="ref", linkedin_access_token="li").items():
        monkeypatch.setattr(settings, k, v)


def _camp(objective="leads", platform="meta"):
    c = Campaign(id=7, platform=platform, objective=objective, theme="Warmtepomp", name="Test | camp",
                 daily_budget=25.5, targeting={"keywords": ["warmtepomp kopen"]}, status="active")
    ad = Ad(id=3, headlines=["Kop 1", "Kop 2", "Kop 3"], descriptions=["Besch 1", "Besch 2"],
            primary_text="Tekst", cta="SIGN_UP", landing_url="https://example.nl", status="active")
    c.ads = [ad]
    return c, ad


def test_meta(creds, make_client):
    from adagent.platforms.meta import MetaConnector

    seen = []

    def handler(req: httpx.Request):
        body = parse_qs(req.content.decode()) if req.method == "POST" else dict(req.url.params)
        seen.append((req.method, req.url.path, body))
        path = req.url.path
        if path.endswith("/campaigns"):
            return httpx.Response(200, json={"id": "c1"})
        if path.endswith("/adsets"):
            return httpx.Response(200, json={"id": "as1"})
        if path.endswith("/adcreatives"):
            return httpx.Response(200, json={"id": "cr1"})
        if path.endswith("/ads"):
            return httpx.Response(200, json={"id": "ad1"})
        if path.endswith("/insights"):
            return httpx.Response(200, json={"data": [{"ad_id": "ad1", "impressions": "1000", "reach": "800",
                                                       "clicks": "20", "spend": "12.34",
                                                       "actions": [{"action_type": "lead", "value": "2"}],
                                                       "action_values": []}]})
        return httpx.Response(200, json={"success": True})

    client = make_client(meta_ad_account_id="123", meta_page_id="pg", meta_pixel_id="px")
    conn = MetaConnector(client, http=httpx.Client(transport=httpx.MockTransport(handler)))
    camp, ad = _camp()
    conn.create_campaign(camp, [ad], active=True)
    assert camp.external_id == "c1" and camp.external_refs["adset_id"] == "as1" and ad.external_id == "ad1"
    c_body = seen[0][2]
    assert seen[0][1].endswith("act_123/campaigns")
    assert c_body["objective"] == ["OUTCOME_LEADS"] and c_body["daily_budget"] == ["2550"]
    adset = seen[1][2]
    assert json.loads(adset["promoted_object"][0]) == {"pixel_id": "px", "custom_event_type": "LEAD"}
    stats = conn.fetch_stats(camp, dt.date(2026, 3, 3))
    assert stats[0].leads == 2 and stats[0].spend == 12.34 and stats[0].ad_external_id == "ad1"
    conn.set_daily_budget(camp, 30)
    assert seen[-1][2]["daily_budget"] == ["3000"]


def test_google(creds, make_client):
    from adagent.platforms.google_ads import GoogleAdsConnector

    GoogleAdsConnector._token = None
    seen = []

    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert req.headers["developer-token"] == "dev"
        body = json.loads(req.content)
        seen.append((req.url.path, body))
        res = req.url.path.rsplit("/", 1)[-1]
        names = {
            "campaignBudgets:mutate": "customers/1234567890/campaignBudgets/11",
            "campaigns:mutate": "customers/1234567890/campaigns/22",
            "adGroups:mutate": "customers/1234567890/adGroups/33",
            "adGroupAds:mutate": "customers/1234567890/adGroupAds/33~44",
        }
        if res == "googleAds:search":
            return httpx.Response(200, json={"results": [{"adGroupAd": {"ad": {"id": "44"}}, "metrics": {
                "impressions": "500", "clicks": "40", "costMicros": "15000000", "conversions": 3.0,
                "conversionsValue": 0}}]})
        return httpx.Response(200, json={"results": [{"resourceName": names.get(res, "x")}]})

    client = make_client(google_customer_id="123-456-7890")
    conn = GoogleAdsConnector(client, http=httpx.Client(transport=httpx.MockTransport(handler)))
    camp, ad = _camp("leads", "google")
    conn.create_campaign(camp, [ad], active=False)
    assert camp.external_id == "22" and ad.external_id == "44"
    camp_op = next(b for p, b in seen if p.endswith("campaigns:mutate"))["operations"][0]["create"]
    assert camp_op["status"] == "PAUSED" and "maximizeConversions" in camp_op
    assert next(b for p, b in seen if p.endswith("campaignBudgets:mutate"))["operations"][0]["create"]["amountMicros"] == "25500000"
    rsa = next(b for p, b in seen if p.endswith("adGroupAds:mutate"))["operations"][0]["create"]["ad"]
    assert len(rsa["responsiveSearchAd"]["headlines"]) == 3
    stats = conn.fetch_stats(camp, dt.date(2026, 3, 3))
    assert stats[0].spend == 15.0 and stats[0].leads == 3.0 and stats[0].ad_external_id == "44"


def test_linkedin(creds, make_client):
    from adagent.platforms.linkedin import LinkedInConnector

    seen = []
    ids = iter(["900", "901", "urn:li:share:1", "555"])

    def handler(req: httpx.Request):
        assert req.headers["LinkedIn-Version"] == settings.linkedin_api_version
        seen.append((req.method, str(req.url), json.loads(req.content) if req.content else None, req.headers))
        if req.method == "GET":
            return httpx.Response(200, json={"elements": [{"pivotValues": ["urn:li:sponsoredCreative:555"],
                                                           "impressions": 1000, "clicks": 9,
                                                           "costInLocalCurrency": "40.5", "oneClickLeads": 2}]})
        if req.headers.get("X-RestLi-Method") == "PARTIAL_UPDATE":
            return httpx.Response(204)
        return httpx.Response(201, headers={"x-restli-id": next(ids)})

    client = make_client(linkedin_ad_account_id="42", linkedin_org_id="77", linkedin_lead_form_id="88")
    conn = LinkedInConnector(client, http=httpx.Client(transport=httpx.MockTransport(handler)))
    camp, ad = _camp("leads", "linkedin")
    conn.create_campaign(camp, [ad], active=True)
    assert camp.external_id == "901" and ad.external_id == "555"
    body = seen[1][2]
    assert body["objectiveType"] == "LEAD_GENERATION" and body["dailyBudget"]["amount"] == "25.50"
    creative = seen[3][2]
    assert creative["leadgenCallToAction"]["destination"] == "urn:li:adForm:88"
    conn.set_daily_budget(camp, 30)
    assert seen[-1][2] == {"patch": {"$set": {"dailyBudget": {"amount": "30.00", "currencyCode": "EUR"}}}}
    stats = conn.fetch_stats(camp, dt.date(2026, 3, 3))
    assert "List(urn%3Ali%3AsponsoredCampaign%3A901)" in seen[-1][1]
    assert stats[0].ad_external_id == "555" and stats[0].leads == 2 and stats[0].spend == 40.5


def test_missing_credentials_raise(make_client, monkeypatch):
    from adagent.platforms.base import PlatformError
    from adagent.platforms.meta import MetaConnector

    monkeypatch.setattr(settings, "meta_access_token", "")
    with pytest.raises(PlatformError):
        MetaConnector(make_client())
