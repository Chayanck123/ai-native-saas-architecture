"""Uniform command line for every service container::

    <service>-service api      # HTTP API (scale on CPU / request rate)
    <service>-service worker   # background worker (scale on queue depth)

The same image runs either role; Kubernetes deploys them as separate
Deployments so each can scale independently.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Callable, Sequence
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from prometheus_client import start_http_server

from platform_core.observability.logging import configure_logging
from platform_core.service import ServiceRuntime
from platform_core.worker import Worker


class BuiltService(Protocol):
    @property
    def runtime(self) -> ServiceRuntime: ...

    @property
    def app(self) -> FastAPI: ...

    @property
    def worker(self) -> Worker: ...


async def run_worker(built: BuiltService, *, metrics_port: int | None = None) -> None:
    await built.runtime.start()
    if metrics_port:
        # Workers have no HTTP API, but Prometheus still needs their task, queue and
        # LLM metrics; this endpoint doubles as the liveness probe target.
        start_http_server(metrics_port, registry=built.runtime.metrics.registry)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Graceful shutdown: finish the current batch, then exit (Kubernetes sends SIGTERM).
        loop.add_signal_handler(sig, built.worker.stop)
    try:
        await built.worker.run_forever()
    finally:
        await built.runtime.stop()
        await built.runtime.infra.close()


def main(
    service: str, build: Callable[[], BuiltService], argv: Sequence[str] | None = None
) -> None:
    parser = argparse.ArgumentParser(prog=f"{service}-service")
    sub = parser.add_subparsers(dest="role", required=True)
    api = sub.add_parser("api", help="serve the HTTP API")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104 - containers listen on all interfaces
    api.add_argument("--port", type=int, default=8000)
    worker = sub.add_parser("worker", help="run the background worker")
    worker.add_argument("--metrics-port", type=int, default=9100)
    args = parser.parse_args(argv)

    built = build()
    settings = built.runtime.settings
    configure_logging(service, settings.log_level, json_output=settings.log_json)
    if args.role == "api":
        uvicorn.run(
            built.app,
            host=args.host,
            port=args.port,
            log_config=None,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    else:
        asyncio.run(run_worker(built, metrics_port=args.metrics_port))
