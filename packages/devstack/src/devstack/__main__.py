"""``devstack serve`` (the whole platform on one port) and ``devstack demo``."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from agents_service.anthropic_llm import AnthropicLLM
from agents_service.llm import LLMClient
from devstack.stack import STAFF_EMAIL, build_devstack
from platform_core.observability.logging import configure_logging


def _llm_from_env() -> LLMClient | None:
    if (
        not os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("AGENTS_LLM_PROVIDER", "anthropic") != "anthropic"
    ):
        return None
    model = os.environ.get("AGENTS_MODEL")
    if not model:
        raise SystemExit(
            "Set AGENTS_MODEL to the Anthropic model to use, or unset ANTHROPIC_API_KEY "
            "to run the offline model."
        )
    return AnthropicLLM(model=model, effort=os.environ.get("AGENTS_EFFORT", "high"))


async def serve(host: str, port: int, data_dir: Path) -> None:
    llm = _llm_from_env()
    stack = build_devstack(data_dir=data_dir, llm=llm)
    await stack.start()
    stack.start_workers()
    base = f"http://{host}:{port}"
    llm_label = f"Anthropic API ({llm.model})" if llm else "offline model (no API key)"
    print(f"""
  AI-native SaaS platform (devstack) is running
  ------------------------------------------------
  API docs (gateway) : {base}/docs
  Platform OpenAPI   : {base}/v1/openapi.json
  Stripe webhook     : {base}/webhooks/stripe      (Lambda emulator)
  Telemetry beacon   : {base}/t                    (Lambda emulator)
  Scheduled jobs     : {base}/_dev/scheduler/<job> (Lambda emulator)
  Staff account      : sign up as {STAFF_EMAIL} to unlock analytics
  LLM                : {llm_label}
  Data directory     : {data_dir}
""")
    server = uvicorn.Server(uvicorn.Config(stack.app, host=host, port=port, log_config=None))
    try:
        await server.serve()
    finally:
        await stack.stop()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="devstack")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_cmd = sub.add_parser("serve", help="run every service in one process")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument("--data-dir", type=Path, default=Path("data/devstack"))
    sub.add_parser("demo", help="simulate five weeks of the business end to end")
    args = parser.parse_args(argv)

    if args.command == "demo":
        configure_logging("devstack", "WARNING", json_output=False)
        from devstack.demo import main as run_demo

        run_demo()
    else:
        configure_logging("devstack", os.environ.get("LOG_LEVEL", "INFO"), json_output=False)
        asyncio.run(serve(args.host, args.port, args.data_dir))


if __name__ == "__main__":
    main()
