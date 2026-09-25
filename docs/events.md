# Event catalog

The contract between the services. Producers and consumers import these type
names from [`platform_core.catalog`](../packages/platform-core/src/platform_core/catalog.py)
instead of typing strings. How events travel is described in
[architecture.md](architecture.md#events-tasks-and-reliability).

## Envelope

Every event is a [CloudEvent 1.0](https://cloudevents.io) with two extensions:
`tenantid` (the workspace it belongs to, if any) and `traceparent` (the W3C
trace context, so traces continue across the bus).

```json
{
  "specversion": "1.0",
  "id": "evt_00mugqmp9902m3gv314s8nmik9",
  "source": "/services/knowledge",
  "type": "document.indexed",
  "subject": "doc_00mugqmp9902m3gv314s8nmik9",
  "time": "2026-09-25T09:07:48.912Z",
  "datacontenttype": "application/json",
  "tenantid": "ten_00mugqm8r0c1x7gq5d3p4v2h8e",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {"document_id": "doc_...", "chunks": 12, "embedding_model": "voyage-3.5", "duration_ms": 840}
}
```

Delivery is **at least once**. Consumers de-duplicate on `id`, and every
handler is idempotent. Events from external systems reuse the external id (for
example `stripe:evt_...`), so the external system's own retries are
de-duplicated too.

## Events

### Identity and tenancy (gateway)

| Type | Payload (`data`) | Consumed by |
| --- | --- | --- |
| `tenant.created` | `name`, `plan`, `owner_user_id` | billing (provision a free subscription), growth (`workspace_created`) |
| `user.signed_up` | `user_id`, `anonymous_id`, `email_domain`, `attribution` (UTM and referrer of the first touch) | growth (`signed_up`, stitches anonymous visits to the account) |
| `user.logged_in` | `user_id` | growth (`logged_in`) |
| `member.added` | `user_id`, `role`, `invited_by` | billing (seat added), growth (`member_added`), tenant webhooks |
| `member.role_changed` | `user_id`, `role`, `previous_role` | none yet (audit trail) |
| `member.removed` | `user_id` | billing (seat removed) |
| `api_key.created` | `key_id`, `scopes`, `created_by` | none yet (audit trail) |

### Knowledge (RAG)

| Type | Payload | Consumed by |
| --- | --- | --- |
| `document.uploaded` | `document_id`, `title`, `chars` | knowledge worker (chunk and embed) |
| `document.indexed` | `document_id`, `chunks`, `embedding_model`, `duration_ms` | growth (`document_indexed`, part of activation), tenant webhooks |
| `document.failed` | `document_id`, `error` | tenant webhooks |
| `document.deleted` | `document_id` | none yet |
| `search.performed` | `mode`, `results`, `query_chars`, `user_id` | growth (`search_performed`) |

### Agents

| Type | Payload | Consumed by |
| --- | --- | --- |
| `agent.run.requested` | `run_id`, `created_by`, `question_chars` | agents worker (executes the run) |
| `agent.run.completed` | `run_id`, `status` (`succeeded`, `needs_review`, `failed`), `created_by`, `critic_score`, `knowledge_gap`, `citations`, `tool_calls`, `input_tokens`, `output_tokens`, `duration_ms` | growth (`agent_run_<status>`), tenant webhooks |
| `agent.run.reviewed` | `run_id`, `approved`, `corrected`, `reviewer` | tenant webhooks |
| `agent.feedback.submitted` | `run_id`, `rating`, `user_id`, `chunk_ids` | knowledge (re-ranks the cited chunks for this tenant), growth (`feedback_submitted`) |

### Metering (any service → billing)

| Type | Payload | Consumed by |
| --- | --- | --- |
| `usage.recorded` | `meter`, `quantity`, `idempotency_key`, optional `resource_id` | billing (rates it into credits; the idempotency key is unique in the ledger) |

| Meter | Unit | Produced by |
| --- | --- | --- |
| `llm_input_tokens` | tokens | agents |
| `llm_output_tokens` | tokens | agents |
| `agent_runs` | runs | agents |
| `embedding_tokens` | tokens | knowledge |
| `searches` | searches | knowledge |

Prices per meter are in [architecture.md](architecture.md#monetization).

### Billing

| Type | Payload | Consumed by |
| --- | --- | --- |
| `billing.subscription.changed` | `plan`, `previous_plan`, `status`, `seats`, `recurring_cents`, `previous_recurring_cents`, `reason` | gateway (syncs plan and rate limits), growth (`subscription_changed`: conversions, MRR per account), tenant webhooks |
| `billing.quota.threshold_reached` | `threshold` (80 or 100), `plan`, `hard_limit`, `used_credits`, `included_credits`, `suggested_plan` | growth (quota pressure feeds lead scoring), tenant webhooks |
| `billing.provider.subscription_updated` | `provider`, `customer_id`, `subscription_id`, `status`, `plan`, `seats` | billing (applies the change) |
| `billing.provider.invoice_paid` | `invoice_id`, `amount_paid_cents`, `currency` | none yet (receipts, revenue recognition) |
| `billing.provider.payment_failed` | `invoice_id`, `attempt_count` | none yet (dunning emails) |

The `billing.provider.*` events come from the **stripe-webhook** Lambda, which
verifies Stripe's signature and translates `customer.subscription.*`,
`invoice.paid` and `invoice.payment_failed`.

### Growth

| Type | Payload | Consumed by |
| --- | --- | --- |
| `telemetry.batch.received` | `events`: a list of `name`, `anonymous_id`, `occurred_at` and sanitized `properties` (`path`, `referrer`, `utm_*` and similar) | growth (ingests marketing-site traffic) |
| `growth.pql.identified` | `score`, `signals`, `plan` | none yet (CRM or sales-assist hand-off) |
| `growth.experiment.concluded` | `key`, `action` (`ship`, `keep_control`, `halt_srm`), `winner`, `reason` | none yet (feature-flag rollout) |

`telemetry.batch.received` comes from the **telemetry-beacon** Lambda.

### Platform

| Type | Payload | Consumed by |
| --- | --- | --- |
| `scheduler.job.requested` | `job`: `revenue_snapshot`, `pql_scoring` or `experiment_autopilot` | billing (`revenue_snapshot`), growth (`pql_scoring`, `experiment_autopilot`) |
| `platform.task.dead_lettered` | `queue`, `task`, `task_id`, `attempts`, `error` | none yet: operators are alerted through metrics (see the [runbook](runbook.md#dead-lettered-tasks)) |

`scheduler.job.requested` comes from the **scheduler** Lambda, triggered by
EventBridge Scheduler: the revenue snapshot daily at 00:15 UTC, PQL scoring
hourly, and the experiment autopilot every 15 minutes.

## Tenant webhooks

Workspace owners and admins can subscribe HTTPS endpoints to these public
events (`POST /v1/webhooks`): `document.indexed`, `document.failed`,
`agent.run.completed`, `agent.run.reviewed`, `billing.subscription.changed`,
`billing.quota.threshold_reached` and `member.added`.

Each delivery is the CloudEvent above, signed with the endpoint's secret:

```text
x-webhook-signature: t=1790325207,v1=<hex HMAC-SHA256 of "<t>.<body>">
```

Receivers should reject timestamps older than five minutes and compare
signatures in constant time. A failed delivery is retried with exponential
backoff and jitter, up to 8 attempts (about ten minutes). After that it is
marked failed and dead-lettered on the gateway queue, where staff can redrive
it. Deliveries and their status are listed at
`GET /v1/webhooks/{endpoint_id}/deliveries`.

## Task queues

Each worker turns the events it consumes into named tasks on its own queue.
Tasks are retried with exponential backoff and dead-lettered after their last
attempt.

| Queue (service) | Tasks |
| --- | --- |
| gateway | `gateway.fanout_webhooks`, `gateway.deliver_webhook`, `gateway.sync_plan` |
| knowledge | `knowledge.index_document`, `knowledge.apply_feedback` |
| agents | `agents.execute_run` |
| billing | `billing.provision_tenant`, `billing.record_usage`, `billing.seat_added`, `billing.seat_removed`, `billing.apply_provider_update`, `billing.scheduled_job` |
| growth | `growth.project`, `growth.ingest_beacon`, `growth.scheduled_job` |

## Adding an event

1. Add the type to `Events` in `catalog.py` (past tense, `<domain>.<thing>.<verb>`).
2. Publish it through the unit of work (`uow.publish(runtime.event(...))`), so
   it commits with the state change.
3. Subscribe consumers with `worker.on_event(Events.X, task="service.handler")`
   and make the handler idempotent.
4. Document the payload here. Payloads may gain fields; renaming or removing
   one needs a new event type.
