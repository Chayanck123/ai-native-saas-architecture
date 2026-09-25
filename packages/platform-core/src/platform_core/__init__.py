"""Shared kernel used by every service in the platform.

Every microservice builds on the same small set of building blocks so that
cross-cutting concerns behave identically everywhere:

* ``context`` / ``rbac`` / ``security`` - who is calling, for which tenant, and what they may do
* ``events`` - CloudEvents envelopes on a publish/subscribe event bus
* ``tasks`` / ``worker`` / ``idempotency`` / ``retry`` - background work that is
  safe to deliver more than once and survives transient failures
* ``ratelimit`` - per-tenant token buckets (noisy-neighbour protection)
* ``db`` - tenant-scoped persistence with PostgreSQL row-level security
* ``observability`` - JSON logs, OpenTelemetry traces and Prometheus metrics
* ``service`` / ``clients`` - the FastAPI app factory and service-to-service HTTP client
"""

__version__ = "0.1.0"
