"""Google Ads API (REST).

Structuur: Budget -> Zoekcampagne -> advertentiegroep (zoekwoorden) -> responsive search ads.
- bereik: Target Impression Share (zichtbaarheid bovenaan)
- kliks:  Maximize Clicks
- leads:  Maximize Conversions
- sales:  Maximize Conversion Value
Docs: https://developers.google.com/google-ads/api/rest/overview
"""

from __future__ import annotations

import datetime as dt
import time

import httpx2 as httpx

from ..config import settings
from ..models import Ad, Campaign
from .base import GOOGLE_GEO, GOOGLE_LANG, Connector, CopyLimits, DailyStats, PlatformError, raise_for


def _micros(eur: float) -> str:
    return str(int(round(eur * 1_000_000)))


class GoogleAdsConnector(Connector):
    platform = "google"
    min_daily_budget = 1.0
    copy_limits = CopyLimits(
        headline=30, description=90, primary_text=90, max_headlines=15, max_descriptions=4,
        min_headlines=3, min_descriptions=2,
    )

    _token: str | None = None
    _token_exp: float = 0.0

    def __init__(self, client, http: httpx.Client | None = None):
        super().__init__(client)
        missing = [
            n for n, v in {
                "GOOGLE_ADS_DEVELOPER_TOKEN": settings.google_developer_token,
                "GOOGLE_ADS_CLIENT_ID": settings.google_client_id,
                "GOOGLE_ADS_CLIENT_SECRET": settings.google_client_secret,
                "GOOGLE_ADS_REFRESH_TOKEN": settings.google_refresh_token,
            }.items() if not v
        ]
        if missing:
            raise PlatformError("Google Ads configuratie ontbreekt: " + ", ".join(missing))
        if not client.google_customer_id:
            raise PlatformError(f"Klant {client.name}: Google Ads customer ID ontbreekt")
        self.http = http or httpx.Client(timeout=60)
        self.cid = client.google_customer_id.replace("-", "")
        self.base = f"https://googleads.googleapis.com/{settings.google_api_version}/customers/{self.cid}"

    # --- helpers ---
    def _access_token(self) -> str:
        cls = type(self)
        if cls._token and time.time() < cls._token_exp - 60:
            return cls._token
        resp = self.http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": settings.google_refresh_token,
            },
        )
        raise_for(resp, "Google OAuth")
        body = resp.json()
        cls._token = body["access_token"]
        cls._token_exp = time.time() + int(body.get("expires_in", 3600))
        return cls._token

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token()}",
            "developer-token": settings.google_developer_token,
            "Content-Type": "application/json",
        }
        if settings.google_login_customer_id:
            h["login-customer-id"] = settings.google_login_customer_id
        return h

    def _mutate(self, resource: str, operations: list[dict]) -> list[str]:
        resp = self.http.post(
            f"{self.base}/{resource}:mutate", headers=self._headers(), json={"operations": operations}
        )
        raise_for(resp, "Google Ads")
        return [r.get("resourceName", "") for r in resp.json().get("results", [])]

    def _bidding(self, objective: str) -> dict:
        if objective == "reach":
            return {
                "targetImpressionShare": {
                    "location": "TOP_OF_PAGE",
                    "locationFractionMicros": "700000",
                    "cpcBidCeilingMicros": _micros(3.0),
                }
            }
        if objective == "clicks":
            return {"targetSpend": {}}
        if objective == "leads":
            return {"maximizeConversions": {}}
        return {"maximizeConversionValue": {}}

    # --- interface ---
    def create_campaign(self, campaign: Campaign, ads: list[Ad], *, active: bool) -> None:
        status = "ENABLED" if active else "PAUSED"
        (budget_rn,) = self._mutate(
            "campaignBudgets",
            [{"create": {
                "name": f"{campaign.name} budget {campaign.id}",
                "amountMicros": _micros(campaign.daily_budget),
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            }}],
        )
        camp = {
            "name": campaign.name,
            "status": status,
            "advertisingChannelType": "SEARCH",
            "campaignBudget": budget_rn,
            "networkSettings": {
                "targetGoogleSearch": True,
                "targetSearchNetwork": True,
                "targetContentNetwork": False,
                "targetPartnerSearchNetwork": False,
            },
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
            **self._bidding(campaign.objective),
        }
        (camp_rn,) = self._mutate("campaigns", [{"create": camp}])
        campaign.external_id = camp_rn.rsplit("/", 1)[-1]

        criteria = [
            {"create": {"campaign": camp_rn, "location": {"geoTargetConstant": f"geoTargetConstants/{GOOGLE_GEO[c]}"}}}
            for c in (self.client.locations or ["NL"]) if c in GOOGLE_GEO
        ]
        lang = GOOGLE_LANG.get(self.client.language)
        if lang:
            criteria.append({"create": {"campaign": camp_rn, "language": {"languageConstant": f"languageConstants/{lang}"}}})
        if criteria:
            self._mutate("campaignCriteria", criteria)

        (ag_rn,) = self._mutate(
            "adGroups",
            [{"create": {
                "name": f"{campaign.theme or campaign.name}"[:250],
                "campaign": camp_rn,
                "status": "ENABLED",
                "type": "SEARCH_STANDARD",
            }}],
        )
        keywords = (campaign.targeting or {}).get("keywords") or [campaign.theme]
        self._mutate(
            "adGroupCriteria",
            [{"create": {"adGroup": ag_rn, "status": "ENABLED", "keyword": {"text": k[:80], "matchType": "PHRASE"}}}
             for k in keywords[:50] if k],
        )
        campaign.external_refs = {"budget": budget_rn, "campaign": camp_rn, "ad_group": ag_rn}
        for ad in ads:
            self.create_ad(campaign, ad)

    def set_daily_budget(self, campaign: Campaign, amount: float) -> None:
        self._mutate(
            "campaignBudgets",
            [{"update": {"resourceName": campaign.external_refs["budget"], "amountMicros": _micros(amount)},
              "updateMask": "amountMicros"}],
        )

    def set_campaign_status(self, campaign: Campaign, active: bool) -> None:
        self._mutate(
            "campaigns",
            [{"update": {"resourceName": campaign.external_refs["campaign"],
                         "status": "ENABLED" if active else "PAUSED"},
              "updateMask": "status"}],
        )

    def create_ad(self, campaign: Campaign, ad: Ad) -> None:
        (rn,) = self._mutate(
            "adGroupAds",
            [{"create": {
                "adGroup": campaign.external_refs["ad_group"],
                "status": "ENABLED" if ad.status == "active" else "PAUSED",
                "ad": {
                    "finalUrls": [ad.landing_url or self.client.website],
                    "responsiveSearchAd": {
                        "headlines": [{"text": h} for h in ad.headlines[:15]],
                        "descriptions": [{"text": d} for d in ad.descriptions[:4]],
                    },
                },
            }}],
        )
        ad.external_id = rn.rsplit("~", 1)[-1]

    def set_ad_status(self, campaign: Campaign, ad: Ad, active: bool) -> None:
        ag_id = campaign.external_refs["ad_group"].rsplit("/", 1)[-1]
        self._mutate(
            "adGroupAds",
            [{"update": {"resourceName": f"customers/{self.cid}/adGroupAds/{ag_id}~{ad.external_id}",
                         "status": "ENABLED" if active else "PAUSED"},
              "updateMask": "status"}],
        )

    def fetch_stats(self, campaign: Campaign, day: dt.date) -> list[DailyStats]:
        query = (
            "SELECT ad_group_ad.ad.id, metrics.impressions, metrics.clicks, metrics.cost_micros, "
            "metrics.conversions, metrics.conversions_value FROM ad_group_ad "
            f"WHERE campaign.id = {int(campaign.external_id)} AND segments.date = '{day.isoformat()}'"
        )
        resp = self.http.post(f"{self.base}/googleAds:search", headers=self._headers(), json={"query": query})
        raise_for(resp, "Google Ads")
        out = []
        for row in resp.json().get("results", []):
            m = row.get("metrics", {})
            conv = float(m.get("conversions", 0) or 0)
            imp = int(m.get("impressions", 0) or 0)
            out.append(
                DailyStats(
                    date=day,
                    ad_external_id=str(row.get("adGroupAd", {}).get("ad", {}).get("id")),
                    impressions=imp,
                    reach=imp,  # Search levert geen unieke reach; impressies als benadering
                    clicks=int(m.get("clicks", 0) or 0),
                    spend=int(m.get("costMicros", 0) or 0) / 1_000_000,
                    leads=conv if campaign.objective in ("leads", "clicks", "reach") else 0.0,
                    conversions=conv if campaign.objective == "sales" else 0.0,
                    revenue=float(m.get("conversionsValue", 0) or 0),
                )
            )
        return out
