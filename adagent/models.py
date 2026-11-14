"""Datamodel: klanten, campagnes, advertenties, dagcijfers, agent-runs en acties."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PLATFORMS = ("meta", "google", "linkedin")
OBJECTIVES = ("reach", "clicks", "leads", "sales")

PLATFORM_LABELS = {"meta": "Meta (FB/IG)", "google": "Google Ads", "linkedin": "LinkedIn"}
OBJECTIVE_LABELS = {"reach": "Bereik", "clicks": "Kliks", "leads": "Leads", "sales": "Sales"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    website: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(Text, default="")
    locations: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["NL"])
    language: Mapped[str] = mapped_column(String(10), default="nl")

    monthly_budget: Mapped[float] = mapped_column(Float, default=0.0)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)
    objectives: Mapped[list[str]] = mapped_column(JSON, default=list)
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Optionele KPI-doelen; leeg = agent bepaalt benchmark zelf
    target_cpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_cpc: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_cpl: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_roas: Mapped[float | None] = mapped_column(Float, nullable=True)

    autopilot: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new|active|paused

    # Platform-accounts van deze klant
    meta_ad_account_id: Mapped[str] = mapped_column(String(64), default="")
    meta_page_id: Mapped[str] = mapped_column(String(64), default="")
    meta_pixel_id: Mapped[str] = mapped_column(String(64), default="")
    google_customer_id: Mapped[str] = mapped_column(String(64), default="")
    google_conversion_action: Mapped[str] = mapped_column(String(200), default="")
    linkedin_ad_account_id: Mapped[str] = mapped_column(String(64), default="")
    linkedin_org_id: Mapped[str] = mapped_column(String(64), default="")
    linkedin_lead_form_id: Mapped[str] = mapped_column(String(64), default="")
    linkedin_conversion_id: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="Campaign.id"
    )
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="AgentRun.id.desc()"
    )

    def target_for(self, objective: str) -> float | None:
        return {
            "reach": self.target_cpm,
            "clicks": self.target_cpc,
            "leads": self.target_cpl,
            "sales": self.target_roas,
        }.get(objective)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(20))
    objective: Mapped[str] = mapped_column(String(20))
    theme: Mapped[str] = mapped_column(String(300), default="")
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|error
    paused_by: Mapped[str] = mapped_column(String(20), default="")  # agent|user|theme
    daily_budget: Mapped[float] = mapped_column(Float, default=0.0)
    targeting: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    external_id: Mapped[str] = mapped_column(String(200), default="")
    external_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    launched_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    last_budget_change: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    client: Mapped[Client] = relationship(back_populates="campaigns")
    ads: Mapped[list["Ad"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="Ad.id"
    )
    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    theme: Mapped[str] = mapped_column(String(300), default="")
    headlines: Mapped[list[str]] = mapped_column(JSON, default=list)
    descriptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    primary_text: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(String(50), default="LEARN_MORE")
    landing_url: Mapped[str] = mapped_column(String(1000), default="")
    image_url: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    external_id: Mapped[str] = mapped_column(String(200), default="")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    campaign: Mapped[Campaign] = relationship(back_populates="ads")


class Metric(Base):
    """Dagcijfers per campagne (ad_id leeg) of per advertentie."""

    __tablename__ = "metrics"
    __table_args__ = (UniqueConstraint("campaign_id", "ad_id", "date", name="uq_metric_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    ad_id: Mapped[int | None] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), nullable=True)
    date: Mapped[dt.date] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    leads: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)

    campaign: Mapped[Campaign] = relationship(back_populates="metrics")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    run_date: Mapped[dt.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(20), default="daily")  # setup|daily|manual
    mode: Mapped[str] = mapped_column(String(10), default="rules")  # ai|rules
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|done|error
    report: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship(back_populates="runs")
    actions: Mapped[list["Action"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Action.id"
    )


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ad_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="applied")  # applied|rejected|failed
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun | None] = relationship(back_populates="actions")
