"""Growth HTTP API: telemetry ingest (tenants), analytics and experiments (staff)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from growth_service.analytics import AttributionModel
from growth_service.experiments import Variant
from growth_service.models import Experiment
from growth_service.service import ClientEvent, GrowthService
from platform_core.context import Principal
from platform_core.errors import ValidationFailed
from platform_core.rbac import Permission
from platform_core.service import require, require_authenticated

router = APIRouter(tags=["growth"])
STAFF = False  # analytics endpoints are cross-tenant: no tenant context required


def _service(request: Request) -> GrowthService:
    service = request.app.state.growth
    assert isinstance(service, GrowthService)
    return service


class ClientEventIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    id: str | None = Field(default=None, max_length=64)
    anonymous_id: str | None = Field(default=None, max_length=64)
    occurred_at: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class TelemetryBatchIn(BaseModel):
    events: list[ClientEventIn] = Field(min_length=1, max_length=100)


class VariantIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0)


class ExperimentIn(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    name: str = Field(min_length=1, max_length=200)
    variants: list[VariantIn] = Field(min_length=2, max_length=10)
    primary_metric: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    unit: Literal["user", "tenant", "anonymous"] = "user"
    min_sample_per_variant: int = Field(default=200, ge=10)
    confidence: float = Field(default=0.95, ge=0.8, le=0.999)
    auto_decide: bool = True


class SpendIn(BaseModel):
    channel: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    amount_cents: int = Field(ge=0)


def _experiment_out(experiment: Experiment) -> dict[str, Any]:
    return {
        "key": experiment.key,
        "name": experiment.name,
        "status": experiment.status,
        "variants": experiment.variants,
        "primary_metric": experiment.primary_metric,
        "unit": experiment.unit,
        "winner": experiment.winner,
        "decision": experiment.decision,
        "started_at": experiment.started_at,
        "ended_at": experiment.ended_at,
    }


# ------------------------------------------------------------------ telemetry
@router.post("/v1/telemetry/events", status_code=202)
async def ingest_events(
    body: TelemetryBatchIn,
    request: Request,
    principal: Principal = require(Permission.TELEMETRY_WRITE),
) -> dict[str, int]:
    """Product analytics from the web app / SDKs (batched, de-duplicated by id)."""
    events = [
        ClientEvent(
            name=e.name,
            id=e.id,
            anonymous_id=e.anonymous_id,
            occurred_at=e.occurred_at,
            properties=e.properties,
        )
        for e in body.events
    ]
    return await _service(request).ingest(
        events, source="client", tenant_id=principal.tenant_id, user_id=principal.subject
    )


# ------------------------------------------------------------------ analytics
@router.get("/v1/analytics/overview")
async def overview(
    request: Request, _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF)
) -> dict[str, Any]:
    return await _service(request).overview()


@router.get("/v1/analytics/live")
async def live(
    request: Request,
    minutes: int = Query(default=15, ge=1, le=240),
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    return await _service(request).live(minutes)


@router.get("/v1/analytics/funnel")
async def funnel_report(
    request: Request,
    steps: str = Query(default="signed_up,document_indexed,agent_run_succeeded,converted_to_paid"),
    window_days: int = Query(default=30, ge=1, le=365),
    since: datetime | None = None,
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    names = [s.strip() for s in steps.split(",") if s.strip()]
    if len(names) < 2:
        raise ValidationFailed("a funnel needs at least two steps")
    result = await _service(request).funnel_report(names, window_days=window_days, since=since)
    return {"window_days": window_days, "steps": [asdict(step) for step in result]}


@router.get("/v1/analytics/cohorts")
async def cohorts(
    request: Request,
    period: Literal["day", "week", "month"] = "week",
    periods: int = Query(default=8, ge=1, le=52),
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    rows = await _service(request).cohort_report(period=period, periods=periods)
    return {
        "period": period,
        "cohorts": [
            {"cohort_start": r.cohort_start.isoformat(), "size": r.size, "retention": r.retention}
            for r in rows
        ],
    }


@router.get("/v1/analytics/attribution")
async def attribution(
    request: Request,
    model: AttributionModel = AttributionModel.LINEAR,
    lookback_days: int = Query(default=90, ge=1, le=365),
    since: datetime | None = None,
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    return await _service(request).attribution_report(
        model, lookback_days=lookback_days, since=since
    )


@router.get("/v1/analytics/unit-economics")
async def unit_economics_report(
    request: Request,
    months: int = Query(default=3, ge=1, le=24),
    model: AttributionModel = AttributionModel.LINEAR,
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    return await _service(request).unit_economics_report(months=months, model=model)


@router.put("/v1/analytics/marketing-spend", status_code=204)
async def marketing_spend(
    body: SpendIn,
    request: Request,
    _: Principal = require(Permission.EXPERIMENTS_MANAGE, tenant=STAFF),
) -> None:
    await _service(request).set_marketing_spend(body.channel, body.month, body.amount_cents)


@router.get("/v1/analytics/pqls")
async def product_qualified_leads(
    request: Request, _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF)
) -> list[dict[str, Any]]:
    return await _service(request).pqls()


@router.post("/v1/analytics/jobs/{job}")
async def run_job(
    job: Literal["pql_scoring", "experiment_autopilot"],
    request: Request,
    _: Principal = require(Permission.PLATFORM_ADMIN, tenant=STAFF),
) -> dict[str, Any]:
    service = _service(request)
    if job == "pql_scoring":
        return await service.score_all_pqls()
    return {"decisions": await service.run_autopilot()}


# ---------------------------------------------------------------- experiments
@router.post("/v1/experiments", status_code=201)
async def create_experiment(
    body: ExperimentIn,
    request: Request,
    _: Principal = require(Permission.EXPERIMENTS_MANAGE, tenant=STAFF),
) -> dict[str, Any]:
    experiment = await _service(request).create_experiment(
        key=body.key,
        name=body.name,
        variants=[Variant(v.name, v.weight) for v in body.variants],
        primary_metric=body.primary_metric,
        unit=body.unit,
        min_sample_per_variant=body.min_sample_per_variant,
        confidence=body.confidence,
        auto_decide=body.auto_decide,
    )
    return _experiment_out(experiment)


@router.get("/v1/experiments")
async def list_experiments(
    request: Request, _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF)
) -> list[dict[str, Any]]:
    return [_experiment_out(e) for e in await _service(request).list_experiments()]


@router.post("/v1/experiments/{key}/start")
async def start_experiment(
    key: str,
    request: Request,
    _: Principal = require(Permission.EXPERIMENTS_MANAGE, tenant=STAFF),
) -> dict[str, Any]:
    return _experiment_out(await _service(request).set_status(key, "running"))


@router.post("/v1/experiments/{key}/stop")
async def stop_experiment(
    key: str,
    request: Request,
    _: Principal = require(Permission.EXPERIMENTS_MANAGE, tenant=STAFF),
) -> dict[str, Any]:
    return _experiment_out(await _service(request).set_status(key, "halted"))


@router.get("/v1/experiments/{key}/results")
async def experiment_results(
    key: str,
    request: Request,
    _: Principal = require(Permission.ANALYTICS_READ, tenant=STAFF),
) -> dict[str, Any]:
    return await _service(request).results(key)


@router.get("/v1/experiments/{key}/assignment")
async def assignment(
    key: str,
    request: Request,
    unit_id: str | None = Query(default=None, max_length=64),
    principal: Principal = require_authenticated(),
) -> dict[str, Any]:
    """Which variant should this unit see? Deterministic and logged as an exposure."""
    unit = unit_id or principal.subject
    variant, experiment = await _service(request).assign(key, unit)
    return {"experiment": key, "unit_id": unit, "variant": variant, "status": experiment.status}
