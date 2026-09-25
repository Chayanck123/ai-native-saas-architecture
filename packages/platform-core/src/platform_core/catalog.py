"""The event catalog: every event type exchanged between services.

This is the contract of the event-driven architecture. Producers and consumers
import these names instead of typing strings, and ``docs/events.md`` documents
each payload.
"""

from __future__ import annotations


class Events:
    # gateway (identity & tenancy)
    TENANT_CREATED = "tenant.created"
    USER_SIGNED_UP = "user.signed_up"
    USER_LOGGED_IN = "user.logged_in"
    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    API_KEY_CREATED = "api_key.created"

    # knowledge (RAG)
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_INDEXED = "document.indexed"
    DOCUMENT_FAILED = "document.failed"
    DOCUMENT_DELETED = "document.deleted"
    SEARCH_PERFORMED = "search.performed"

    # agents
    AGENT_RUN_REQUESTED = "agent.run.requested"
    AGENT_RUN_COMPLETED = "agent.run.completed"
    AGENT_RUN_REVIEWED = "agent.run.reviewed"
    AGENT_FEEDBACK_SUBMITTED = "agent.feedback.submitted"

    # metering (produced by any service, consumed by billing)
    USAGE_RECORDED = "usage.recorded"

    # billing
    SUBSCRIPTION_CHANGED = "billing.subscription.changed"
    QUOTA_THRESHOLD_REACHED = "billing.quota.threshold_reached"
    PROVIDER_SUBSCRIPTION_UPDATED = "billing.provider.subscription_updated"
    PROVIDER_INVOICE_PAID = "billing.provider.invoice_paid"
    PROVIDER_PAYMENT_FAILED = "billing.provider.payment_failed"

    # growth
    TELEMETRY_BATCH_RECEIVED = "telemetry.batch.received"
    PQL_IDENTIFIED = "growth.pql.identified"
    EXPERIMENT_CONCLUDED = "growth.experiment.concluded"

    # platform
    SCHEDULED_JOB_REQUESTED = "scheduler.job.requested"
    TASK_DEAD_LETTERED = "platform.task.dead_lettered"


class Meters:
    """Usage meters recorded by services and priced by billing."""

    LLM_INPUT_TOKENS = "llm_input_tokens"
    LLM_OUTPUT_TOKENS = "llm_output_tokens"
    EMBEDDING_TOKENS = "embedding_tokens"
    AGENT_RUNS = "agent_runs"
    SEARCHES = "searches"

    ALL = (LLM_INPUT_TOKENS, LLM_OUTPUT_TOKENS, EMBEDDING_TOKENS, AGENT_RUNS, SEARCHES)


# Public events that tenants may subscribe to through outbound webhooks.
WEBHOOK_EVENT_TYPES: tuple[str, ...] = (
    Events.DOCUMENT_INDEXED,
    Events.DOCUMENT_FAILED,
    Events.AGENT_RUN_COMPLETED,
    Events.AGENT_RUN_REVIEWED,
    Events.SUBSCRIPTION_CHANGED,
    Events.QUOTA_THRESHOLD_REACHED,
    Events.MEMBER_ADDED,
)
