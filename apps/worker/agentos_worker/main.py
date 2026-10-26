"""Worker entry point: ``agentos-worker [--id ID] [--concurrency N]``."""

from __future__ import annotations

import argparse
import asyncio
import signal

from agentos_core.config import get_settings
from agentos_core.db.session import dispose_engine
from agentos_core.log import configure_logging
from agentos_core.telemetry import configure_tracing
from agentos_worker.scheduler import Worker


async def _main(args: argparse.Namespace) -> None:
    worker = Worker(args.id, concurrency=args.concurrency, run_coordinator=not args.no_coordinator)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await dispose_engine()


def run() -> None:
    parser = argparse.ArgumentParser(description="AgentOS durable workflow worker")
    parser.add_argument("--id", default=None, help="stable worker id (default: host-pid-random)")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--no-coordinator", action="store_true", help="do not run recovery duties")
    args = parser.parse_args()
    settings = get_settings()
    settings.ensure_safe()
    configure_logging(settings.log_level, settings.log_json)
    configure_tracing("worker")
    asyncio.run(_main(args))


if __name__ == "__main__":
    run()
