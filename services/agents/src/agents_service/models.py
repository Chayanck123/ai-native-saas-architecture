"""Tables owned by the agents service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from platform_core.db import JsonType, TenantScoped, UtcDateTime


class Base(DeclarativeBase):
    pass


class AgentRun(TenantScoped, Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    # queued | running | succeeded | needs_review | rejected | refused | failed
    status: Mapped[str] = mapped_column(String(20), index=True)
    plan: Mapped[list[str]] = mapped_column(JsonType, default=list)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    critic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    critic_issues: Mapped[list[str]] = mapped_column(JsonType, default=list)
    knowledge_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    review: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class RunFeedback(TenantScoped, Base):
    """End-user feedback: the raw material of the data flywheel."""

    __tablename__ = "run_feedback"
    __table_args__ = (UniqueConstraint("run_id", "user_id", name="uq_feedback_per_user"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    rating: Mapped[int] = mapped_column(Integer)  # +1 / -1
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


TENANT_TABLES = ("agent_runs", "run_feedback")
