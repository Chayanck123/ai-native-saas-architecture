"""Structured JSON logging correlated with traces, tenants and requests.

Every log line carries ``trace_id``/``span_id`` (jump from a log line straight to
its trace in Jaeger), ``tenant_id`` (filter one customer's activity) and
``request_id`` (what support asks the customer for).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from platform_core.context import current_request_id, current_tenant_id
from platform_core.observability.tracing import current_trace_ids

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_trace_ids()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in (
            ("trace_id", trace_id),
            ("span_id", span_id),
            ("tenant_id", current_tenant_id()),
            ("request_id", current_request_id()),
        ):
            if value:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter(service_name))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates our structured request log.
    logging.getLogger("uvicorn.access").disabled = True
