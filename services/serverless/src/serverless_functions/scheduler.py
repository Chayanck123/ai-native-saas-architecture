"""Lambda: turn EventBridge schedules into ``scheduler.job.requested`` events.

The schedule (cron) lives in infrastructure-as-code; the work lives in the
services that own the data. The function just announces "it is time", and
whichever service handles that job picks it up from the bus.
"""

from __future__ import annotations

from typing import Any

from platform_core.catalog import Events
from platform_core.events import Event
from serverless_functions.common import get_publisher

SOURCE = "/functions/scheduler"
JOBS = {"revenue_snapshot", "pql_scoring", "experiment_autopilot"}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    detail = event.get("detail") or {}
    job = detail.get("job") or event.get("job")
    if job not in JOBS:
        raise ValueError(f"unknown scheduled job {job!r}; expected one of {sorted(JOBS)}")
    # One event per schedule tick: EventBridge's id makes accidental double
    # invocations collapse into one downstream task.
    tick = event.get("id") or event.get("time")
    published = Event.create(
        Events.SCHEDULED_JOB_REQUESTED,
        source=SOURCE,
        data={"job": job},
        event_id=f"schedule:{job}:{tick}" if tick else None,
    )
    get_publisher().publish(published)
    return {"job": job, "event_id": published.id}
