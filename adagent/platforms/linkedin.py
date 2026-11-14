"""LinkedIn Marketing API (REST, versioned).

Structuur: Campagnegroep -> campagne (Sponsored Content) -> creatives (Direct Sponsored Content posts).
Docs: https://learn.microsoft.com/linkedin/marketing/
"""

from __future__ import annotations

import datetime as dt
import time
from urllib.parse import quote

import httpx2 as httpx

from ..config import settings
from ..models import Ad, Campaign
from .base import LINKEDIN_GEO, Connector, CopyLimits, DailyStats, PlatformError, raise_for

API = "https://api.linkedin.com/rest"
OBJECTIVE = {
    "reach": "BRAND_AWARENESS",
    "clicks": "WEBSITE_VISIT",
    "leads": "LEAD_GENERATION",
    "sales": "WEBSITE_CONVERSION",
}
CTA_LABEL = {"SIGN_UP": "SIGN_UP", "GET_QUOTE": "REQUEST_DEMO", "DOWNLOAD": "DOWNLOAD", "APPLY_NOW": "APPLY"}


class LinkedInConnector(Connector):
    platform = "linkedin"
    min_daily_budget = 10.0
    copy_limits = CopyLimits(headline=70, description=100, primary_text=600, max_headlines=1, max_descriptions=1)

    def __init__(self, client, http: httpx.Client | None = None):
        super().__init__(client)
        if not settings.linkedin_access_token:
            raise PlatformError("LINKEDIN_ACCESS_TOKEN ontbreekt")
        if not client.linkedin_ad_account_id or not client.linkedin_org_id:
            raise PlatformError(f"Klant {client.name}: LinkedIn ad account / organisatie-ID ontbreekt")
        self.http = http or httpx.Client(timeout=60)
        self.acc = client.linkedin_ad_account_id
        self.acc_urn = f"urn:li:sponsoredAccount:{self.acc}"

    def _headers(self, method: str | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {settings.linkedin_access_token}",
            "LinkedIn-Version": settings.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        if method:
            h["X-RestLi-Method"] = method
        return h

    def _create(self, path: str, body: dict) -> str:
        resp = self.http.post(f"{API}/{path}", headers=self._headers(), json=body)
        raise_for(resp, "LinkedIn")
        new_id = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
        if not new_id:
            try:
                new_id = str(resp.json().get("id", ""))
            except ValueError:
                new_id = ""
        if not new_id:
            raise PlatformError(f"LinkedIn gaf geen ID terug voor {path}")
        return new_id

    def _patch(self, path: str, fields: dict) -> None:
        resp = self.http.post(
            f"{API}/{path}", headers=self._headers("PARTIAL_UPDATE"), json={"patch": {"$set": fields}}
        )
        raise_for(resp, "LinkedIn")

    def _budget(self, amount: float) -> dict:
        return {"amount": f"{amount:.2f}", "currencyCode": settings.currency}

    # --- interface ---
    def create_campaign(self, campaign: Campaign, ads: list[Ad], *, active: bool) -> None:
        status = "ACTIVE" if active else "PAUSED"
        start = int(time.time() * 1000)
        group_id = self._create(
            f"adAccounts/{self.acc}/adCampaignGroups",
            {"account": self.acc_urn, "name": f"{self.client.name} | {campaign.theme or campaign.objective}"[:100],
             "status": "ACTIVE", "runSchedule": {"start": start}},
        )
        locations = [f"urn:li:geo:{LINKEDIN_GEO[c]}" for c in (self.client.locations or ["NL"]) if c in LINKEDIN_GEO]
        include = [{"or": {"urn:li:adTargetingFacet:locations": locations or [f"urn:li:geo:{LINKEDIN_GEO['NL']}"]}}]
        t = campaign.targeting or {}
        for facet_key, facet in (
            ("linkedin_job_functions", "urn:li:adTargetingFacet:jobFunctions"),
            ("linkedin_seniorities", "urn:li:adTargetingFacet:seniorities"),
            ("linkedin_industries", "urn:li:adTargetingFacet:industries"),
            ("linkedin_staff_counts", "urn:li:adTargetingFacet:staffCountRanges"),
        ):
            if t.get(facet_key):
                include.append({"or": {facet: t[facet_key]}})
        body = {
            "account": self.acc_urn,
            "campaignGroup": f"urn:li:sponsoredCampaignGroup:{group_id}",
            "name": campaign.name[:100],
            "type": "SPONSORED_UPDATES",
            "costType": "CPM",
            "objectiveType": OBJECTIVE[campaign.objective],
            "dailyBudget": self._budget(campaign.daily_budget),
            "locale": {"country": (self.client.locations or ["NL"])[0], "language": self.client.language or "nl"},
            "status": status,
            "runSchedule": {"start": start},
            "offsiteDeliveryEnabled": False,
            "audienceExpansionEnabled": True,
            "politicalIntent": "NOT_POLITICAL",
            "targetingCriteria": {"include": {"and": include}},
        }
        camp_id = self._create(f"adAccounts/{self.acc}/adCampaigns", body)
        campaign.external_id = camp_id
        campaign.external_refs = {"campaign_group": group_id}
        for ad in ads:
            self.create_ad(campaign, ad)

    def set_daily_budget(self, campaign: Campaign, amount: float) -> None:
        self._patch(f"adAccounts/{self.acc}/adCampaigns/{campaign.external_id}", {"dailyBudget": self._budget(amount)})

    def set_campaign_status(self, campaign: Campaign, active: bool) -> None:
        self._patch(
            f"adAccounts/{self.acc}/adCampaigns/{campaign.external_id}", {"status": "ACTIVE" if active else "PAUSED"}
        )

    def create_ad(self, campaign: Campaign, ad: Ad) -> None:
        link = ad.landing_url or self.client.website
        post = {
            "author": f"urn:li:organization:{self.client.linkedin_org_id}",
            "commentary": ad.primary_text,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "NONE", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "adContext": {"dscAdAccount": self.acc_urn, "dscStatus": "ACTIVE"},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
            "content": {"article": {"source": link, "title": (ad.headlines or [""])[0],
                                    "description": (ad.descriptions or [""])[0]}},
        }
        if campaign.objective == "leads" and self.client.linkedin_lead_form_id:
            post.pop("content")
        post_urn = self._create("posts", post)
        creative: dict = {
            "campaign": f"urn:li:sponsoredCampaign:{campaign.external_id}",
            "content": {"reference": post_urn},
            "intendedStatus": "ACTIVE" if ad.status == "active" else "PAUSED",
        }
        if campaign.objective == "leads" and self.client.linkedin_lead_form_id:
            creative["leadgenCallToAction"] = {
                "destination": f"urn:li:adForm:{self.client.linkedin_lead_form_id}",
                "label": CTA_LABEL.get(ad.cta, "LEARN_MORE"),
            }
        ad.external_id = self._create(f"adAccounts/{self.acc}/creatives", creative)

    def set_ad_status(self, campaign: Campaign, ad: Ad, active: bool) -> None:
        self._patch(
            f"adAccounts/{self.acc}/creatives/{quote(ad.external_id, safe='')}",
            {"intendedStatus": "ACTIVE" if active else "PAUSED"},
        )

    def fetch_stats(self, campaign: Campaign, day: dt.date) -> list[DailyStats]:
        rng = f"(start:(year:{day.year},month:{day.month},day:{day.day}),end:(year:{day.year},month:{day.month},day:{day.day}))"
        camp_urn = quote(f"urn:li:sponsoredCampaign:{campaign.external_id}", safe="")
        fields = (
            "impressions,clicks,costInLocalCurrency,oneClickLeads,externalWebsiteConversions,"
            "conversionValueInLocalCurrency,approximateMemberReach,pivotValues"
        )
        url = (
            f"{API}/adAnalytics?q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
            f"&dateRange={rng}&campaigns=List({camp_urn})&fields={fields}"
        )
        resp = self.http.get(url, headers=self._headers())
        raise_for(resp, "LinkedIn")
        out = []
        for row in resp.json().get("elements", []):
            creative_urn = (row.get("pivotValues") or [""])[0]
            out.append(
                DailyStats(
                    date=day,
                    ad_external_id=creative_urn.rsplit(":", 1)[-1] if creative_urn else None,
                    impressions=int(row.get("impressions", 0) or 0),
                    reach=int(row.get("approximateMemberReach", 0) or 0),
                    clicks=int(row.get("clicks", 0) or 0),
                    spend=float(row.get("costInLocalCurrency", 0) or 0),
                    leads=float(row.get("oneClickLeads", 0) or 0),
                    conversions=float(row.get("externalWebsiteConversions", 0) or 0),
                    revenue=float(row.get("conversionValueInLocalCurrency", 0) or 0),
                )
            )
        return out
