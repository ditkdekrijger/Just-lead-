"""Configuratie via omgevingsvariabelen (zie .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimale .env-lader (geen extra dependency). Bestaande env-vars winnen."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path.cwd() / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    mode: str = field(default_factory=lambda: _env("ADAGENT_MODE", "simulate").lower())
    db_url: str = field(default_factory=lambda: _env("ADAGENT_DB_URL", "sqlite:///adagent.db"))
    timezone: str = field(default_factory=lambda: _env("ADAGENT_TIMEZONE", "Europe/Amsterdam"))
    daily_hour: int = field(default_factory=lambda: _int("ADAGENT_DAILY_HOUR", 7))
    currency: str = field(default_factory=lambda: _env("ADAGENT_CURRENCY", "EUR"))

    max_daily_budget_change: float = field(
        default_factory=lambda: _float("ADAGENT_MAX_DAILY_BUDGET_CHANGE", 0.2)
    )
    learning_days: int = field(default_factory=lambda: _int("ADAGENT_LEARNING_DAYS", 3))
    max_new_ads_per_day: int = field(default_factory=lambda: _int("ADAGENT_MAX_NEW_ADS_PER_DAY", 4))

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: _env("ADAGENT_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _env("ADAGENT_EFFORT", "high"))

    meta_access_token: str = field(default_factory=lambda: _env("META_ACCESS_TOKEN"))
    meta_api_version: str = field(default_factory=lambda: _env("META_API_VERSION", "v23.0"))

    google_developer_token: str = field(default_factory=lambda: _env("GOOGLE_ADS_DEVELOPER_TOKEN"))
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_ADS_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_ADS_CLIENT_SECRET"))
    google_refresh_token: str = field(default_factory=lambda: _env("GOOGLE_ADS_REFRESH_TOKEN"))
    google_login_customer_id: str = field(
        default_factory=lambda: _env("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "")
    )
    google_api_version: str = field(default_factory=lambda: _env("GOOGLE_ADS_API_VERSION", "v21"))

    linkedin_access_token: str = field(default_factory=lambda: _env("LINKEDIN_ACCESS_TOKEN"))
    linkedin_api_version: str = field(default_factory=lambda: _env("LINKEDIN_API_VERSION", "202510"))

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def ai_enabled(self) -> bool:
        # De SDK kan ook via ANTHROPIC_AUTH_TOKEN / `ant auth login` authenticeren.
        return bool(
            self.anthropic_api_key
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ADAGENT_FORCE_AI") == "1"
        )


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
