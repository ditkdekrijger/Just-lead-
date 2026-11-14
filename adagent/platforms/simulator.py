"""Sandbox-platform: gedraagt zich als een echt platform, maar genereert realistische,
reproduceerbare resultaten. Hiermee draait de volledige agent zonder echte uitgaven."""

from __future__ import annotations

import datetime as dt
import hashlib
import random

from ..models import Ad, Campaign
from .base import Connector, CopyLimits, DailyStats

# Benchmarks per platform (EUR): cpm, ctr, conversieratio lead, conversieratio sale, orderwaarde
BASE = {
    "meta": dict(cpm=7.0, ctr=0.012, lead_cvr=0.03, sale_cvr=0.010, aov=85.0, freq=1.6),
    "google": dict(cpm=28.0, ctr=0.055, lead_cvr=0.05, sale_cvr=0.018, aov=110.0, freq=1.3),
    "linkedin": dict(cpm=38.0, ctr=0.0065, lead_cvr=0.07, sale_cvr=0.010, aov=450.0, freq=1.9),
}
# Effect van campagnedoel op de mix
OBJ = {
    "reach": dict(cpm=0.55, ctr=0.6, lead=0.15, sale=0.1),
    "clicks": dict(cpm=1.0, ctr=1.35, lead=0.4, sale=0.35),
    "leads": dict(cpm=1.25, ctr=1.0, lead=1.0, sale=0.3),
    "sales": dict(cpm=1.3, ctr=0.95, lead=0.25, sale=1.0),
}


def _seed(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def quality(*parts: object, lo: float = 0.55, hi: float = 1.55) -> float:
    return lo + (hi - lo) * random.Random(_seed(*parts)).random()


class SimulatorConnector(Connector):
    min_daily_budget = 1.0

    def __init__(self, client, platform: str):
        super().__init__(client)
        self.platform = platform
        self.min_daily_budget = {"linkedin": 10.0, "google": 1.0, "meta": 1.0}[platform]
        self.copy_limits = {
            "google": CopyLimits(30, 90, 90, 15, 4, 3, 2),
            "meta": CopyLimits(40, 30, 500, 5, 5),
            "linkedin": CopyLimits(70, 100, 600, 1, 1),
        }[platform]

    def create_campaign(self, campaign: Campaign, ads: list[Ad], *, active: bool) -> None:
        campaign.external_id = f"sim-{self.platform}-{campaign.id}"
        campaign.external_refs = {"simulated": True}
        for ad in ads:
            self.create_ad(campaign, ad)

    def set_daily_budget(self, campaign: Campaign, amount: float) -> None:
        return None

    def set_campaign_status(self, campaign: Campaign, active: bool) -> None:
        return None

    def create_ad(self, campaign: Campaign, ad: Ad) -> None:
        ad.external_id = f"sim-ad-{campaign.id}-{ad.id}"

    def set_ad_status(self, campaign: Campaign, ad: Ad, active: bool) -> None:
        return None

    def fetch_stats(self, campaign: Campaign, day: dt.date) -> list[DailyStats]:
        if campaign.status != "active" or not campaign.launched_on or day < campaign.launched_on:
            return []
        active_ads = [a for a in campaign.ads if a.status == "active"]
        if not active_ads:
            return []
        b, o = BASE[self.platform], OBJ[campaign.objective]
        rng = random.Random(_seed("day", campaign.id, day.isoformat()))
        camp_q = quality("camp", self.client.id, campaign.platform, campaign.objective, campaign.theme)
        # Leerfase: eerste dagen minder efficiënt
        age = (day - campaign.launched_on).days
        learning = 0.75 + 0.25 * min(1.0, age / 5)

        ad_q = {
            a.id: quality("ad", campaign.id, a.headlines[:1], a.primary_text[:40], a.generation)
            * (1 + 0.07 * (a.generation - 1))
            for a in active_ads
        }
        total_q = sum(ad_q.values())
        spend_total = campaign.daily_budget * rng.uniform(0.86, 1.0)
        out: list[DailyStats] = []
        for ad in active_ads:
            share = ad_q[ad.id] / total_q
            spend = round(spend_total * share, 2)
            noise = rng.uniform(0.82, 1.18)
            cpm = b["cpm"] * o["cpm"] / (camp_q ** 0.3) * rng.uniform(0.9, 1.1)
            impressions = int(spend / cpm * 1000)
            ctr = b["ctr"] * o["ctr"] * camp_q * ad_q[ad.id] * learning * noise
            clicks = int(impressions * ctr)
            leads = clicks * b["lead_cvr"] * o["lead"] * camp_q * rng.uniform(0.7, 1.3)
            sales = clicks * b["sale_cvr"] * o["sale"] * camp_q * rng.uniform(0.7, 1.3)
            out.append(
                DailyStats(
                    date=day,
                    ad_external_id=ad.external_id,
                    impressions=impressions,
                    reach=int(impressions / (b["freq"] * rng.uniform(0.9, 1.1))),
                    clicks=clicks,
                    spend=spend,
                    leads=round(leads, 2),
                    conversions=round(sales, 2),
                    revenue=round(sales * b["aov"] * rng.uniform(0.8, 1.2), 2),
                )
            )
        return out
