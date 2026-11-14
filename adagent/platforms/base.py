"""Gemeenschappelijke interface voor advertentieplatformen."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx2 as httpx

from ..models import Ad, Campaign, Client


class PlatformError(RuntimeError):
    """Fout van een platform-API (wordt gelogd als mislukte actie)."""


@dataclass
class CopyLimits:
    headline: int
    description: int
    primary_text: int
    max_headlines: int
    max_descriptions: int
    min_headlines: int = 1
    min_descriptions: int = 1


@dataclass
class DailyStats:
    date: dt.date
    ad_external_id: str | None  # None = campagnetotaal
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    spend: float = 0.0
    leads: float = 0.0
    conversions: float = 0.0
    revenue: float = 0.0


class Connector(ABC):
    platform: str = ""
    #: Minimaal dagbudget dat het platform (praktisch) accepteert, in EUR
    min_daily_budget: float = 1.0
    copy_limits: CopyLimits

    def __init__(self, client: Client):
        self.client = client

    # --- levenscyclus ---
    @abstractmethod
    def create_campaign(self, campaign: Campaign, ads: list[Ad], *, active: bool) -> None:
        """Maakt campagne + onderliggende structuur + advertenties aan. Zet external ids."""

    @abstractmethod
    def set_daily_budget(self, campaign: Campaign, amount: float) -> None: ...

    @abstractmethod
    def set_campaign_status(self, campaign: Campaign, active: bool) -> None: ...

    @abstractmethod
    def create_ad(self, campaign: Campaign, ad: Ad) -> None: ...

    @abstractmethod
    def set_ad_status(self, campaign: Campaign, ad: Ad, active: bool) -> None: ...

    # --- rapportage ---
    @abstractmethod
    def fetch_stats(self, campaign: Campaign, day: dt.date) -> list[DailyStats]:
        """Cijfers per advertentie voor één dag (ad_external_id gevuld)."""


def raise_for(resp: httpx.Response, platform: str) -> None:
    if resp.status_code >= 400:
        body = resp.text[:800]
        raise PlatformError(f"{platform} API {resp.status_code}: {body}")


# Landcodes -> platform-specifieke geo-ID's (uitbreidbaar)
GOOGLE_GEO = {"NL": "2528", "BE": "2056", "DE": "2276", "FR": "2250", "GB": "2826", "US": "2840"}
GOOGLE_LANG = {"nl": "1010", "en": "1000", "de": "1001", "fr": "1002"}
LINKEDIN_GEO = {
    "NL": "102890719",
    "BE": "100565514",
    "DE": "101282230",
    "FR": "105015875",
    "GB": "101165590",
    "US": "103644278",
}
