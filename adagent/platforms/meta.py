"""Meta Marketing API (Facebook + Instagram).

Structuur: Campagne (CBO, dagbudget) -> 1 advertentieset (targeting) -> advertenties.
Docs: https://developers.facebook.com/docs/marketing-api
"""

from __future__ import annotations

import datetime as dt
import json

import httpx2 as httpx

from ..config import settings
from ..models import Ad, Campaign
from .base import Connector, CopyLimits, DailyStats, PlatformError, raise_for

OBJECTIVE = {
    "reach": "OUTCOME_AWARENESS",
    "clicks": "OUTCOME_TRAFFIC",
    "leads": "OUTCOME_LEADS",
    "sales": "OUTCOME_SALES",
}
LEAD_ACTIONS = {"lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"}
SALE_ACTIONS = {"purchase", "offsite_conversion.fb_pixel_purchase", "omni_purchase"}
CTA = {"LEARN_MORE", "SIGN_UP", "SHOP_NOW", "CONTACT_US", "GET_QUOTE", "BOOK_TRAVEL", "APPLY_NOW", "DOWNLOAD"}


class MetaConnector(Connector):
    platform = "meta"
    min_daily_budget = 1.0
    copy_limits = CopyLimits(headline=40, description=30, primary_text=500, max_headlines=5, max_descriptions=5)

    def __init__(self, client, http: httpx.Client | None = None):
        super().__init__(client)
        if not settings.meta_access_token:
            raise PlatformError("META_ACCESS_TOKEN ontbreekt")
        if not client.meta_ad_account_id:
            raise PlatformError(f"Klant {client.name}: Meta ad account ID ontbreekt")
        self.http = http or httpx.Client(timeout=60)
        self.base = f"https://graph.facebook.com/{settings.meta_api_version}"
        acc = client.meta_ad_account_id
        self.account = acc if acc.startswith("act_") else f"act_{acc}"

    # --- helpers ---
    def _post(self, path: str, data: dict) -> dict:
        payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
        payload["access_token"] = settings.meta_access_token
        resp = self.http.post(f"{self.base}/{path}", data=payload)
        raise_for(resp, "Meta")
        return resp.json()

    def _get(self, path: str, params: dict) -> dict:
        params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
        params["access_token"] = settings.meta_access_token
        resp = self.http.get(f"{self.base}/{path}", params=params)
        raise_for(resp, "Meta")
        return resp.json()

    def _targeting(self, campaign: Campaign) -> dict:
        t = campaign.targeting or {}
        targeting: dict = {
            "geo_locations": {"countries": self.client.locations or ["NL"]},
            "age_min": int(t.get("age_min", 18)),
            "age_max": int(t.get("age_max", 65)),
            "targeting_automation": {"advantage_audience": 1},
        }
        interests = t.get("meta_interest_ids") or []
        if interests:
            targeting["flexible_spec"] = [{"interests": [{"id": i} for i in interests]}]
        return targeting

    def _optimization(self, campaign: Campaign) -> tuple[str, dict | None]:
        pixel = self.client.meta_pixel_id
        if campaign.objective == "reach":
            return "REACH", None
        if campaign.objective == "clicks":
            return "LINK_CLICKS", None
        if campaign.objective == "leads":
            if pixel:
                return "OFFSITE_CONVERSIONS", {"pixel_id": pixel, "custom_event_type": "LEAD"}
            return "LINK_CLICKS", None
        if pixel:
            return "OFFSITE_CONVERSIONS", {"pixel_id": pixel, "custom_event_type": "PURCHASE"}
        return "LINK_CLICKS", None

    # --- interface ---
    def create_campaign(self, campaign: Campaign, ads: list[Ad], *, active: bool) -> None:
        status = "ACTIVE" if active else "PAUSED"
        res = self._post(
            f"{self.account}/campaigns",
            {
                "name": campaign.name,
                "objective": OBJECTIVE[campaign.objective],
                "status": status,
                "special_ad_categories": [],
                "daily_budget": int(round(campaign.daily_budget * 100)),
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            },
        )
        campaign.external_id = res["id"]
        goal, promoted = self._optimization(campaign)
        adset = {
            "name": f"{campaign.name} | doelgroep",
            "campaign_id": campaign.external_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": goal,
            "targeting": self._targeting(campaign),
            "status": status,
            "start_time": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if promoted:
            adset["promoted_object"] = promoted
        res = self._post(f"{self.account}/adsets", adset)
        campaign.external_refs = {"adset_id": res["id"]}
        for ad in ads:
            self.create_ad(campaign, ad)

    def set_daily_budget(self, campaign: Campaign, amount: float) -> None:
        self._post(campaign.external_id, {"daily_budget": int(round(amount * 100))})

    def set_campaign_status(self, campaign: Campaign, active: bool) -> None:
        self._post(campaign.external_id, {"status": "ACTIVE" if active else "PAUSED"})

    def create_ad(self, campaign: Campaign, ad: Ad) -> None:
        if not self.client.meta_page_id:
            raise PlatformError("Meta page ID ontbreekt bij klant")
        link = ad.landing_url or self.client.website
        cta = ad.cta if ad.cta in CTA else "LEARN_MORE"
        link_data: dict = {
            "link": link,
            "message": ad.primary_text,
            "name": (ad.headlines or [""])[0],
            "description": (ad.descriptions or [""])[0],
            "call_to_action": {"type": cta, "value": {"link": link}},
        }
        if ad.image_url:
            link_data["picture"] = ad.image_url
        creative = self._post(
            f"{self.account}/adcreatives",
            {
                "name": f"{campaign.name} | creative {ad.id}",
                "object_story_spec": {"page_id": self.client.meta_page_id, "link_data": link_data},
            },
        )
        res = self._post(
            f"{self.account}/ads",
            {
                "name": f"{campaign.name} | ad {ad.id}",
                "adset_id": campaign.external_refs["adset_id"],
                "creative": {"creative_id": creative["id"]},
                "status": "ACTIVE" if ad.status == "active" else "PAUSED",
            },
        )
        ad.external_id = res["id"]

    def set_ad_status(self, campaign: Campaign, ad: Ad, active: bool) -> None:
        self._post(ad.external_id, {"status": "ACTIVE" if active else "PAUSED"})

    def fetch_stats(self, campaign: Campaign, day: dt.date) -> list[DailyStats]:
        d = day.isoformat()
        data = self._get(
            f"{campaign.external_id}/insights",
            {
                "level": "ad",
                "fields": "ad_id,impressions,reach,clicks,spend,actions,action_values",
                "time_range": {"since": d, "until": d},
                "limit": 500,
            },
        )
        out = []
        for row in data.get("data", []):
            actions = {a["action_type"]: float(a["value"]) for a in row.get("actions", [])}
            values = {a["action_type"]: float(a["value"]) for a in row.get("action_values", [])}
            out.append(
                DailyStats(
                    date=day,
                    ad_external_id=row.get("ad_id"),
                    impressions=int(row.get("impressions", 0)),
                    reach=int(row.get("reach", 0)),
                    clicks=int(row.get("clicks", 0)),
                    spend=float(row.get("spend", 0)),
                    leads=max((actions.get(k, 0.0) for k in LEAD_ACTIONS), default=0.0),
                    conversions=max((actions.get(k, 0.0) for k in SALE_ACTIONS), default=0.0),
                    revenue=max((values.get(k, 0.0) for k in SALE_ACTIONS), default=0.0),
                )
            )
        return out
