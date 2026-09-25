"""Growth use cases: event projection, telemetry ingest, analytics, experiments, PQLs."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from growth_service.analytics import (
    AttributionModel,
    CohortRow,
    FunnelStep,
    Touch,
    attribute,
    funnel,
    monthly_churn_rate,
    retention_cohorts,
    unit_economics,
)
from growth_service.experiments import Variant, assign_variant, evaluate
from growth_service.models import (
    Account,
    AnalyticsEvent,
    Experiment,
    ExperimentExposure,
    Identity,
    MarketingSpend,
)
from growth_service.settings import GrowthSettings
from platform_core.catalog import Events
from platform_core.errors import Conflict, NotFound, ValidationFailed
from platform_core.events import Event
from platform_core.ids import new_id
from platform_core.service import ServiceRuntime

PROJECTED: dict[str, str] = {
    Events.TENANT_CREATED: "workspace_created",
    Events.USER_SIGNED_UP: "signed_up",
    Events.USER_LOGGED_IN: "logged_in",
    Events.MEMBER_ADDED: "member_added",
    Events.DOCUMENT_INDEXED: "document_indexed",
    Events.SEARCH_PERFORMED: "search_performed",
    Events.AGENT_RUN_COMPLETED: "agent_run",  # suffixed with the run status
    Events.AGENT_FEEDBACK_SUBMITTED: "feedback_submitted",
    Events.QUOTA_THRESHOLD_REACHED: "quota_threshold_reached",
    Events.SUBSCRIPTION_CHANGED: "subscription_changed",
}
ACTIVITY_EVENTS = (
    "logged_in",
    "document_indexed",
    "search_performed",
    "agent_run_succeeded",
    "feedback_submitted",
)
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SEARCH_ENGINES = ("google.", "bing.", "duckduckgo.", "yahoo.", "baidu.")
_SOCIAL = ("linkedin.", "twitter.", "x.com", "facebook.", "reddit.", "youtube.")


def classify_channel(properties: dict[str, Any]) -> str | None:
    """Marketing channel from UTM parameters, falling back to the referrer."""
    source = properties.get("utm_source")
    if source:
        return re.sub(r"[^a-z0-9_]+", "_", str(source).strip().lower())[:64] or None
    referrer = properties.get("referrer")
    if referrer:
        host = urlparse(str(referrer)).netloc.lower()
        if any(engine in host for engine in _SEARCH_ENGINES):
            return "organic_search"
        if any(site in host for site in _SOCIAL):
            return "social"
        return "referral" if host else None
    return None


def month_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


@dataclass(frozen=True)
class ClientEvent:
    name: str
    id: str | None = None
    anonymous_id: str | None = None
    user_id: str | None = None
    occurred_at: datetime | None = None
    properties: dict[str, Any] = field(default_factory=dict)


class GrowthService:
    def __init__(self, runtime: ServiceRuntime, settings: GrowthSettings) -> None:
        self.runtime = runtime
        self.settings = settings

    # ======================================================== write path
    async def _insert(self, session: AsyncSession, **values: Any) -> bool:
        if await session.get(AnalyticsEvent, values["id"]) is not None:
            return False
        values.setdefault("received_at", self.runtime.clock.now())
        values.setdefault("properties", {})
        session.add(AnalyticsEvent(**values))
        return True

    async def _account(self, session: AsyncSession, tenant_id: str, seen_at: datetime) -> Account:
        account = await session.get(Account, tenant_id)
        if account is None:
            account = Account(
                tenant_id=tenant_id,
                signed_up_at=seen_at,
                acquisition_channel="direct",
                plan="free",
                mrr_cents=0,
                pql_score=0,
            )
            session.add(account)
        return account

    def _count(self, milestone: str) -> None:
        self.runtime.metrics.business_events.labels(self.runtime.name, milestone).inc()

    async def project(self, event: Event) -> dict[str, Any]:
        """Turn a domain event into analytics events and account state (idempotent)."""
        data = event.data
        name = PROJECTED[event.type]
        if event.type == Events.AGENT_RUN_COMPLETED:
            name = f"agent_run_{data.get('status', 'unknown')}"
        user_id = data.get("user_id") or data.get("created_by")
        tenant_id = event.tenantid
        attribution = data.get("attribution") or {}
        async with self.runtime.db.session() as session:
            inserted = await self._insert(
                session,
                id=event.id,
                name=name,
                tenant_id=tenant_id,
                user_id=user_id,
                anonymous_id=data.get("anonymous_id"),
                source="server",
                channel=classify_channel(attribution) if attribution else None,
                occurred_at=event.time,
                properties=_public_properties(data),
            )
            if not inserted:
                return {"duplicate": True}
            if tenant_id is None:
                return {"projected": name}
            account = await self._account(session, tenant_id, event.time)
            if name in ACTIVITY_EVENTS:
                account.last_active_at = max(filter(None, [account.last_active_at, event.time]))
            if event.type in (Events.TENANT_CREATED, Events.USER_SIGNED_UP):
                account.signed_up_at = min(account.signed_up_at, event.time)
            if event.type == Events.USER_SIGNED_UP:
                await self._on_signup(session, account, event)
            elif name == self.settings.activation_event and account.activated_at is None:
                account.activated_at = event.time
                await self._insert(
                    session,
                    id=f"{event.id}:activated",
                    name="activated",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source="server",
                    occurred_at=event.time,
                )
                self._count("activation")
            elif event.type == Events.SUBSCRIPTION_CHANGED:
                await self._on_subscription_change(session, account, event)
        if event.type in (Events.QUOTA_THRESHOLD_REACHED, Events.AGENT_RUN_COMPLETED):
            await self.score_pql(tenant_id)
        return {"projected": name}

    async def _on_signup(self, session: AsyncSession, account: Account, event: Event) -> None:
        data = event.data
        user_id, anonymous_id = data.get("user_id"), data.get("anonymous_id")
        account.owner_user_id = account.owner_user_id or user_id
        channel = None
        if anonymous_id and user_id:
            if await session.get(Identity, anonymous_id) is None:
                session.add(
                    Identity(
                        anonymous_id=anonymous_id,
                        user_id=user_id,
                        tenant_id=event.tenantid,
                        linked_at=event.time,
                    )
                )
            first_touch = (
                await session.execute(
                    select(AnalyticsEvent.channel)
                    .where(
                        AnalyticsEvent.anonymous_id == anonymous_id,
                        AnalyticsEvent.channel.is_not(None),
                    )
                    .order_by(AnalyticsEvent.occurred_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            channel = first_touch
        account.acquisition_channel = (
            channel or classify_channel(data.get("attribution") or {}) or "direct"
        )
        self._count("signup")

    async def _on_subscription_change(
        self, session: AsyncSession, account: Account, event: Event
    ) -> None:
        data = event.data
        new, previous = (
            int(data.get("recurring_cents", 0)),
            int(data.get("previous_recurring_cents", 0)),
        )
        account.plan = str(data.get("plan", account.plan))
        account.mrr_cents = new
        derived: str | None = None
        value = new
        if previous == 0 and new > 0:
            derived = "converted_to_paid" if account.first_paid_at is None else "reactivated"
            account.first_paid_at = account.first_paid_at or event.time
            account.churned_at = None
            self._count("conversion")
        elif previous > 0 and new == 0:
            derived, value = "churned", previous
            account.churned_at = event.time
        elif new > previous:
            derived, value = "expanded", new - previous
        elif new < previous:
            derived, value = "contracted", previous - new
        if derived:
            await self._insert(
                session,
                id=f"{event.id}:{derived}",
                name=derived,
                tenant_id=account.tenant_id,
                user_id=account.owner_user_id,
                source="server",
                occurred_at=event.time,
                value_cents=value,
                properties={"plan": account.plan},
            )

    async def ingest(
        self,
        events: list[ClientEvent],
        *,
        source: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """Ingest product/marketing telemetry (deduplicated by event id)."""
        now = self.runtime.clock.now()
        accepted = duplicates = 0
        seen: set[str] = set()
        async with self.runtime.db.session() as session:
            for item in events:
                if not _EVENT_NAME.match(item.name):
                    raise ValidationFailed(f"invalid event name '{item.name}' (use snake_case)")
                event_id = f"{source}:{item.id or new_id('tel')}"
                if event_id in seen:
                    duplicates += 1
                    continue
                seen.add(event_id)
                occurred = min(item.occurred_at or now, now)  # clamp clock-skewed clients
                inserted = await self._insert(
                    session,
                    id=event_id,
                    name=item.name,
                    tenant_id=tenant_id,
                    user_id=user_id or item.user_id,
                    anonymous_id=item.anonymous_id,
                    channel=classify_channel(item.properties),
                    source=source,
                    occurred_at=occurred,
                    properties=dict(list(item.properties.items())[:50]),
                )
                accepted += inserted
                duplicates += not inserted
        return {"accepted": accepted, "duplicates": duplicates}

    # ========================================================= read path
    async def _events(
        self, names: Iterable[str] | None = None, since: datetime | None = None
    ) -> list[AnalyticsEvent]:
        query = select(AnalyticsEvent)
        if names is not None:
            query = query.where(AnalyticsEvent.name.in_(list(names)))
        if since is not None:
            query = query.where(AnalyticsEvent.occurred_at >= since)
        async with self.runtime.db.session() as session:
            return list((await session.execute(query)).scalars())

    async def _identities(self) -> dict[str, str]:
        async with self.runtime.db.session() as session:
            rows = await session.execute(select(Identity.anonymous_id, Identity.tenant_id))
            return {anonymous: tenant for anonymous, tenant in rows if tenant}

    @staticmethod
    def _unit(event: AnalyticsEvent, identities: dict[str, str]) -> str | None:
        if event.tenant_id:
            return event.tenant_id
        if event.anonymous_id:
            return identities.get(event.anonymous_id, f"anon:{event.anonymous_id}")
        return None

    async def _accounts(self) -> list[Account]:
        async with self.runtime.db.session() as session:
            return list((await session.execute(select(Account))).scalars())

    async def funnel_report(
        self, steps: list[str], *, window_days: int, since: datetime | None
    ) -> list[FunnelStep]:
        identities = await self._identities()
        by_unit: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
        for event in await self._events(steps, since):
            unit = self._unit(event, identities)
            if unit:
                by_unit[unit].append((event.name, event.occurred_at))
        return funnel(steps, by_unit, window=timedelta(days=window_days))

    async def cohort_report(
        self, *, period: str, periods: int, activity: tuple[str, ...] = ACTIVITY_EVENTS
    ) -> list[CohortRow]:
        accounts = await self._accounts()
        signups = {a.tenant_id: a.signed_up_at for a in accounts}
        by_tenant: dict[str, list[datetime]] = defaultdict(list)
        for event in await self._events(activity):
            if event.tenant_id:
                by_tenant[event.tenant_id].append(event.occurred_at)
        return retention_cohorts(
            signups, by_tenant, period=period, periods=periods, now=self.runtime.clock.now()
        )

    async def _attributed(
        self, model: AttributionModel, *, since: datetime | None, lookback_days: int
    ) -> tuple[dict[str, float], dict[str, float], int]:
        """Per channel: (fractional new customers, attributed MRR cents), and conversions."""
        identities = await self._identities()
        conversions = await self._events(["converted_to_paid"], since)
        touches: dict[str, list[Touch]] = defaultdict(list)
        async with self.runtime.db.session() as session:
            touch_rows = (
                await session.execute(
                    select(AnalyticsEvent).where(AnalyticsEvent.channel.is_not(None))
                )
            ).scalars()
            for row in touch_rows:
                unit = self._unit(row, identities)
                if unit and row.channel:
                    touches[unit].append(
                        Touch(
                            row.channel, row.occurred_at, is_lead_creation=row.name == "signed_up"
                        )
                    )
        customers: dict[str, float] = defaultdict(float)
        revenue: dict[str, float] = defaultdict(float)
        lookback = timedelta(days=lookback_days)
        for conversion in conversions:
            unit = self._unit(conversion, identities)
            journey = [
                t for t in touches.get(unit or "", []) if conversion.occurred_at - lookback <= t.at
            ]
            for channel, share in attribute(
                journey, model, converted_at=conversion.occurred_at
            ).items():
                customers[channel] += share
                revenue[channel] += share * (conversion.value_cents or 0)
        return customers, revenue, len(conversions)

    async def attribution_report(
        self, model: AttributionModel, *, lookback_days: int, since: datetime | None
    ) -> dict[str, Any]:
        customers, revenue, total = await self._attributed(
            model, since=since, lookback_days=lookback_days
        )
        channels = [
            {
                "channel": ch,
                "conversions": round(customers[ch], 3),
                "mrr_cents": round(revenue[ch], 2),
            }
            for ch in sorted(customers, key=lambda c: -customers[c])
        ]
        return {"model": model.value, "conversions": total, "channels": channels}

    async def unit_economics_report(
        self, *, months: int, model: AttributionModel
    ) -> dict[str, Any]:
        now = self.runtime.clock.now()
        window_start = now - timedelta(days=30 * months)
        async with self.runtime.db.session() as session:
            spend_rows = (
                await session.execute(
                    select(MarketingSpend).where(MarketingSpend.month >= month_key(window_start))
                )
            ).scalars()
            spend: dict[str, int] = defaultdict(int)
            for row in spend_rows:
                spend[row.channel] += row.amount_cents
        customers, _, _ = await self._attributed(model, since=window_start, lookback_days=90)
        accounts = await self._accounts()
        paying = [a.mrr_cents for a in accounts if a.mrr_cents > 0]
        arpa = statistics.fmean(paying) if paying else 0.0
        at_start = [
            a
            for a in accounts
            if a.first_paid_at
            and a.first_paid_at < window_start
            and (a.churned_at is None or a.churned_at >= window_start)
        ]
        churned = [a for a in at_start if a.churned_at and a.churned_at <= now]
        churn = monthly_churn_rate(len(churned), len(at_start), months)
        gm = self.settings.gross_margin

        def economics(spend_cents: float, new_customers: float) -> dict[str, Any]:
            return unit_economics(
                arpa_cents=arpa,
                gross_margin=gm,
                monthly_churn=churn,
                spend_cents=spend_cents,
                new_customers=new_customers,
            ).__dict__

        channels = sorted(set(spend) | set(customers))
        return {
            "window_months": months,
            "attribution_model": model.value,
            "blended": economics(sum(spend.values()), sum(customers.values())),
            "channels": [
                {
                    "channel": ch,
                    "spend_cents": spend.get(ch, 0),
                    "new_customers": round(customers.get(ch, 0.0), 3),
                    **economics(spend.get(ch, 0), customers.get(ch, 0.0)),
                }
                for ch in channels
            ],
        }

    async def set_marketing_spend(self, channel: str, month: str, amount_cents: int) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValidationFailed("month must look like 2026-03")
        async with self.runtime.db.session() as session:
            row = (
                await session.execute(
                    select(MarketingSpend).where(
                        MarketingSpend.channel == channel, MarketingSpend.month == month
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    MarketingSpend(
                        id=new_id("spend"), channel=channel, month=month, amount_cents=amount_cents
                    )
                )
            else:
                row.amount_cents = amount_cents

    async def live(self, minutes: int) -> dict[str, Any]:
        """Real-time view: event counts per minute over the last few minutes."""
        now = self.runtime.clock.now()
        since = now - timedelta(minutes=minutes)
        per_minute: dict[str, Counter[str]] = defaultdict(Counter)
        totals: Counter[str] = Counter()
        for event in await self._events(since=since):
            minute = event.occurred_at.replace(second=0, microsecond=0).isoformat()
            per_minute[minute][event.name] += 1
            totals[event.name] += 1
        return {
            "since": since.isoformat(),
            "totals": dict(totals.most_common()),
            "per_minute": {m: dict(c) for m, c in sorted(per_minute.items())},
        }

    async def overview(self) -> dict[str, Any]:
        now = self.runtime.clock.now()
        accounts = await self._accounts()
        window = timedelta(days=self.settings.activation_window_days)
        matured = [a for a in accounts if a.signed_up_at <= now - window] or accounts
        activated = [
            a for a in matured if a.activated_at and a.activated_at - a.signed_up_at <= window
        ]
        ttv = [
            (a.activated_at - a.signed_up_at).total_seconds() / 3600
            for a in accounts
            if a.activated_at
        ]
        paying = [a for a in accounts if a.mrr_cents > 0]
        return {
            "accounts": len(accounts),
            "signups_last_7d": sum(a.signed_up_at >= now - timedelta(days=7) for a in accounts),
            "signups_last_30d": sum(a.signed_up_at >= now - timedelta(days=30) for a in accounts),
            "activation_rate": round(len(activated) / len(matured), 4) if matured else None,
            "median_hours_to_value": round(statistics.median(ttv), 2) if ttv else None,
            "paying_accounts": len(paying),
            "free_to_paid_conversion": round(len(paying) / len(accounts), 4) if accounts else None,
            "mrr_cents": sum(a.mrr_cents for a in paying),
            "product_qualified_leads": sum(1 for a in accounts if a.pql_at and not a.mrr_cents),
            "acquisition_channels": dict(Counter(a.acquisition_channel for a in accounts)),
        }

    # ============================================================= PQLs
    async def score_pql(self, tenant_id: str) -> int:
        """Product-qualified-lead score: usage that predicts willingness to pay.

        Free workspaces crossing the threshold are announced (``growth.pql.identified``)
        so sales or an in-app upgrade prompt can engage at exactly the right moment.
        """
        now = self.runtime.clock.now()
        since = now - timedelta(days=14)
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            account = await session.get(Account, tenant_id)
            if account is None:
                return 0
            rows = (
                await session.execute(
                    select(
                        AnalyticsEvent.name, AnalyticsEvent.user_id, AnalyticsEvent.properties
                    ).where(
                        AnalyticsEvent.tenant_id == tenant_id, AnalyticsEvent.occurred_at >= since
                    )
                )
            ).all()
            counts = Counter(name for name, _, _ in rows)
            users = {user for _, user, _ in rows if user}
            thresholds = {
                int(props.get("threshold", 0))
                for name, _, props in rows
                if name == "quota_threshold_reached"
            }
            signals = {
                "successful_agent_runs": min(30, 3 * counts["agent_run_succeeded"]),
                "documents_indexed": min(20, 2 * counts["document_indexed"]),
                "active_users": min(30, 10 * len(users)),
                "searches": min(10, counts["search_performed"]),
                "quota_pressure": 20 if 100 in thresholds else 10 if 80 in thresholds else 0,
            }
            score = sum(signals.values())
            account.pql_score = score
            if (
                score >= self.settings.pql_threshold
                and account.pql_at is None
                and account.mrr_cents == 0
            ):
                account.pql_at = now
                await uow.publish(
                    self.runtime.event(
                        Events.PQL_IDENTIFIED,
                        {"score": score, "signals": signals, "plan": account.plan},
                        tenant_id=tenant_id,
                        subject=tenant_id,
                    )
                )
                self._count("pql")
        return score

    async def score_all_pqls(self) -> dict[str, int]:
        accounts = await self._accounts()
        for account in accounts:
            await self.score_pql(account.tenant_id)
        return {"scored": len(accounts)}

    async def pqls(self) -> list[dict[str, Any]]:
        accounts = sorted(await self._accounts(), key=lambda a: -a.pql_score)
        return [
            {
                "tenant_id": a.tenant_id,
                "score": a.pql_score,
                "plan": a.plan,
                "identified_at": a.pql_at.isoformat() if a.pql_at else None,
                "acquisition_channel": a.acquisition_channel,
            }
            for a in accounts
            if a.pql_score > 0
        ]

    # ====================================================== experiments
    async def create_experiment(
        self,
        *,
        key: str,
        name: str,
        variants: list[Variant],
        primary_metric: str,
        unit: str,
        min_sample_per_variant: int,
        confidence: float,
        auto_decide: bool,
    ) -> Experiment:
        if len(variants) < 2 or len({v.name for v in variants}) != len(variants):
            raise ValidationFailed("an experiment needs at least two uniquely named variants")
        if any(v.weight <= 0 for v in variants):
            raise ValidationFailed("variant weights must be positive")
        experiment = Experiment(
            key=key,
            name=name,
            status="draft",
            variants=[{"name": v.name, "weight": v.weight} for v in variants],
            primary_metric=primary_metric,
            unit=unit,
            min_sample_per_variant=min_sample_per_variant,
            confidence=confidence,
            auto_decide=auto_decide,
            created_at=self.runtime.clock.now(),
        )
        try:
            async with self.runtime.db.session() as session:
                session.add(experiment)
        except IntegrityError as exc:
            raise Conflict(f"experiment '{key}' already exists") from exc
        return experiment

    async def get_experiment(self, key: str) -> Experiment:
        async with self.runtime.db.session() as session:
            experiment = await session.get(Experiment, key)
        if experiment is None:
            raise NotFound(f"experiment '{key}' not found")
        return experiment

    async def list_experiments(self) -> list[Experiment]:
        async with self.runtime.db.session() as session:
            return list((await session.execute(select(Experiment))).scalars())

    async def set_status(self, key: str, status: str) -> Experiment:
        async with self.runtime.db.session() as session:
            experiment = await session.get(Experiment, key)
            if experiment is None:
                raise NotFound(f"experiment '{key}' not found")
            allowed = {"running": {"draft"}, "halted": {"running"}}
            if experiment.status not in allowed.get(status, set()):
                raise Conflict(f"cannot move experiment from '{experiment.status}' to '{status}'")
            experiment.status = status
            now = self.runtime.clock.now()
            if status == "running":
                experiment.started_at = now
            else:
                experiment.ended_at = now
        return experiment

    async def assign(self, key: str, unit_id: str) -> tuple[str, Experiment]:
        experiment = await self.get_experiment(key)
        variants = [Variant(v["name"], float(v["weight"])) for v in experiment.variants]
        if experiment.status == "completed" and experiment.winner:
            return experiment.winner, experiment  # the winner is rolled out to everyone
        if experiment.status != "running":
            return variants[0].name, experiment
        variant = assign_variant(key, unit_id, variants)
        try:
            async with self.runtime.db.session() as session:
                session.add(
                    ExperimentExposure(
                        id=new_id("exp"),
                        experiment_key=key,
                        unit_id=unit_id,
                        variant=variant,
                        exposed_at=self.runtime.clock.now(),
                    )
                )
        except IntegrityError:
            pass  # already exposed: first exposure wins, the variant is the same anyway
        return variant, experiment

    async def results(self, key: str) -> dict[str, Any]:
        experiment = await self.get_experiment(key)
        variants = [Variant(v["name"], float(v["weight"])) for v in experiment.variants]
        column = {
            "user": AnalyticsEvent.user_id,
            "tenant": AnalyticsEvent.tenant_id,
            "anonymous": AnalyticsEvent.anonymous_id,
        }[experiment.unit]
        async with self.runtime.db.session() as session:
            exposures = list(
                (
                    await session.execute(
                        select(ExperimentExposure).where(ExperimentExposure.experiment_key == key)
                    )
                ).scalars()
            )
            metric_rows = await session.execute(
                select(column, AnalyticsEvent.occurred_at).where(
                    AnalyticsEvent.name == experiment.primary_metric, column.is_not(None)
                )
            )
            conversions_at: dict[str, list[datetime]] = defaultdict(list)
            for unit_id, occurred in metric_rows:
                if unit_id is not None:
                    conversions_at[unit_id].append(occurred)
        exposed: Counter[str] = Counter()
        converted: Counter[str] = Counter()
        for exposure in exposures:
            exposed[exposure.variant] += 1
            if any(t >= exposure.exposed_at for t in conversions_at.get(exposure.unit_id, [])):
                converted[exposure.variant] += 1
        rows, srm_p, decision = evaluate(
            variants,
            dict(exposed),
            dict(converted),
            min_sample_per_variant=experiment.min_sample_per_variant,
            confidence=experiment.confidence,
        )
        return {
            "key": key,
            "status": experiment.status,
            "primary_metric": experiment.primary_metric,
            "winner": experiment.winner,
            "sample_ratio_mismatch_p": srm_p,
            "variants": [r.__dict__ for r in rows],
            "decision": decision.__dict__,
        }

    async def run_autopilot(self) -> list[dict[str, Any]]:
        """Automated A/B testing: conclude running experiments whose results are decisive."""
        applied = []
        for experiment in await self.list_experiments():
            if experiment.status != "running" or not experiment.auto_decide:
                continue
            report = await self.results(experiment.key)
            decision = report["decision"]
            if decision["action"] == "continue":
                continue
            async with self.runtime.unit_of_work() as uow:
                current = await uow.session.get(Experiment, experiment.key)
                assert current is not None
                current.status = "halted" if decision["action"] == "halt_srm" else "completed"
                current.winner = decision["winner"]
                current.decision = decision
                current.ended_at = self.runtime.clock.now()
                await uow.publish(
                    self.runtime.event(
                        Events.EXPERIMENT_CONCLUDED,
                        {
                            "key": experiment.key,
                            "action": decision["action"],
                            "winner": decision["winner"],
                            "reason": decision["reason"],
                        },
                        subject=experiment.key,
                    )
                )
            applied.append({"key": experiment.key, **decision})
        return applied


def _public_properties(data: dict[str, Any]) -> dict[str, Any]:
    """Copy scalar event fields into analytics properties (no nested blobs or PII)."""
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, str | int | float | bool) and k not in {"email", "question"}
    }
