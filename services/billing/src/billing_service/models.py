"""Tables owned by the billing service."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from platform_core.db import TenantScoped, UtcDateTime


class Base(DeclarativeBase):
    pass


class Subscription(Base):
    __tablename__ = "subscriptions"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))  # active | past_due | canceled
    seats: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(20), default="manual")  # manual | stripe
    provider_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    canceled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class UsageRecord(TenantScoped, Base):
    """One metered usage event. The unique key makes recording idempotent at the
    database level: a redelivered event can never be billed twice."""

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meter: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[int] = mapped_column(BigInteger)
    millicredits: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    period_start: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)


class CreditLedgerEntry(TenantScoped, Base):
    """Append-only credit ledger: grants (+) and consumption (-) per billing period."""

    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_ledger_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_type: Mapped[str] = mapped_column(String(20))  # grant | consume | adjust
    millicredits: Mapped[int] = mapped_column(BigInteger)
    period_start: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class QuotaNotification(TenantScoped, Base):
    """Ensures each usage threshold fires exactly once per tenant per period."""

    __tablename__ = "quota_notifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_start", "threshold", name="uq_quota_threshold"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(UtcDateTime)
    threshold: Mapped[int] = mapped_column(Integer)  # percent: 80 | 100
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class MrrSnapshot(TenantScoped, Base):
    """Month-end (or month-to-date) recurring revenue per tenant, for ARR reporting."""

    __tablename__ = "mrr_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "month", name="uq_snapshot_month"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "2026-09"
    plan: Mapped[str] = mapped_column(String(32))
    seats: Mapped[int] = mapped_column(Integer)
    recurring_cents: Mapped[int] = mapped_column(BigInteger)
    usage_cents: Mapped[int] = mapped_column(BigInteger)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    embedding_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


TENANT_TABLES = (
    "subscriptions",
    "usage_records",
    "credit_ledger",
    "quota_notifications",
    "mrr_snapshots",
)
