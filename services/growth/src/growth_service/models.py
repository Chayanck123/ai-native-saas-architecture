"""Tables owned by the growth service.

This is *platform* data (the SaaS operator's own analytics), read only by staff,
so it is not partitioned by tenant with row-level security; it is also where a
production system would plug in a columnar store (ClickHouse, BigQuery) once
event volume outgrows PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from platform_core.db import JsonType, UtcDateTime


class Base(DeclarativeBase):
    pass


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"
    __table_args__ = (Index("ix_analytics_name_time", "name", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # producer id -> dedupe
    name: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # client | server | edge
    value_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Identity(Base):
    """Stitches an anonymous visitor to the user and workspace they became."""

    __tablename__ = "identities"

    anonymous_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    linked_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Account(Base):
    """Per-workspace growth state, maintained from domain events."""

    __tablename__ = "accounts"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signed_up_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    acquisition_channel: Mapped[str] = mapped_column(String(64), default="direct")
    plan: Mapped[str] = mapped_column(String(32), default="free")
    mrr_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    first_paid_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    churned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    pql_score: Mapped[int] = mapped_column(Integer, default=0)
    pql_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class MarketingSpend(Base):
    __tablename__ = "marketing_spend"
    __table_args__ = (UniqueConstraint("channel", "month", name="uq_spend_channel_month"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(64))
    month: Mapped[str] = mapped_column(String(7))  # "2026-03"
    amount_cents: Mapped[int] = mapped_column(BigInteger)


class Experiment(Base):
    __tablename__ = "experiments"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))  # draft|running|completed|halted
    variants: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    primary_metric: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(16))  # user | tenant | anonymous
    min_sample_per_variant: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    auto_decide: Mapped[bool] = mapped_column(Boolean)
    winner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class ExperimentExposure(Base):
    __tablename__ = "experiment_exposures"
    __table_args__ = (UniqueConstraint("experiment_key", "unit_id", name="uq_exposure_unit"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_key: Mapped[str] = mapped_column(String(64), index=True)
    unit_id: Mapped[str] = mapped_column(String(64))
    variant: Mapped[str] = mapped_column(String(64))
    exposed_at: Mapped[datetime] = mapped_column(UtcDateTime)
