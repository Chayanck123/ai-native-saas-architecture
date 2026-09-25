"""Prometheus metrics shared by every service.

The golden signals (rate, errors, duration) for HTTP and background work, plus
AI-specific cost drivers (LLM tokens) and growth counters. Each service owns a
private registry so several services can run in one process (the devstack)
without their metrics colliding.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class ServiceMetrics:
    def __init__(self, service: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.http_requests = Counter(
            "http_requests_total",
            "HTTP requests handled",
            ["service", "method", "route", "status"],
            registry=r,
        )
        self.http_latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency",
            ["service", "method", "route"],
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.tasks = Counter(
            "tasks_processed_total",
            "Background tasks by outcome (succeeded, retried, dead_lettered, duplicate, deferred)",
            ["service", "queue", "task", "outcome"],
            registry=r,
        )
        self.task_latency = Histogram(
            "task_duration_seconds",
            "Background task execution time",
            ["service", "queue", "task"],
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.events_consumed = Counter(
            "events_consumed_total",
            "Events routed to this service",
            ["service", "type"],
            registry=r,
        )
        self.events_duplicates = Counter(
            "events_duplicates_total",
            "Redelivered events dropped by inbox de-duplication",
            ["service", "type"],
            registry=r,
        )
        self.events_published = Counter(
            "events_published_total",
            "Events published by this service",
            ["service", "type"],
            registry=r,
        )
        self.queue_depth = Gauge(
            "queue_depth",
            "Tasks per queue state (ready, delayed, in_flight, dead)",
            ["service", "queue", "state"],
            registry=r,
        )
        self.llm_tokens = Counter(
            "llm_tokens_total",
            "LLM tokens consumed",
            ["service", "model", "direction"],
            registry=r,
        )
        self.llm_latency = Histogram(
            "llm_request_duration_seconds",
            "LLM call latency",
            ["service", "model"],
            buckets=_LATENCY_BUCKETS,
            registry=r,
        )
        self.rate_limited = Counter(
            "rate_limited_requests_total",
            "Requests rejected by the rate limiter",
            ["service", "plan"],
            registry=r,
        )
        self.idempotent_replays = Counter(
            "idempotent_replays_total",
            "Requests answered from the idempotency cache",
            ["service"],
            registry=r,
        )
        self.business_events = Counter(
            "business_events_total",
            "Growth milestones (signups, activations, upgrades)",
            ["service", "event"],
            registry=r,
        )

    def counter(self, name: str, documentation: str, labels: list[str]) -> Counter:
        return Counter(name, documentation, ["service", *labels], registry=self.registry)

    def gauge(self, name: str, documentation: str, labels: list[str]) -> Gauge:
        return Gauge(name, documentation, ["service", *labels], registry=self.registry)

    def render(self) -> bytes:
        return generate_latest(self.registry)
