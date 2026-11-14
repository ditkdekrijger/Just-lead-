"""Platformselectie: simulator in sandbox-modus, echte API's in live-modus."""

from __future__ import annotations

from ..config import settings
from ..models import Client
from .base import Connector, CopyLimits, DailyStats, PlatformError
from .simulator import SimulatorConnector

__all__ = ["Connector", "CopyLimits", "DailyStats", "PlatformError", "get_connector", "copy_limits_for"]


def get_connector(client: Client, platform: str) -> Connector:
    if not settings.live:
        return SimulatorConnector(client, platform)
    if platform == "meta":
        from .meta import MetaConnector

        return MetaConnector(client)
    if platform == "google":
        from .google_ads import GoogleAdsConnector

        return GoogleAdsConnector(client)
    if platform == "linkedin":
        from .linkedin import LinkedInConnector

        return LinkedInConnector(client)
    raise PlatformError(f"Onbekend platform: {platform}")


def copy_limits_for(platform: str) -> CopyLimits:
    from .google_ads import GoogleAdsConnector
    from .linkedin import LinkedInConnector
    from .meta import MetaConnector

    return {
        "meta": MetaConnector.copy_limits,
        "google": GoogleAdsConnector.copy_limits,
        "linkedin": LinkedInConnector.copy_limits,
    }[platform]


MIN_DAILY_BUDGET = {"meta": 1.0, "google": 1.0, "linkedin": 10.0}
