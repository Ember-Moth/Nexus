import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings
from nexus.shared.telemetry import event

logger = logging.getLogger("nexus.worker")


class Heartbeats(Protocol):
    async def write_heartbeat(self, worker_id: str, payload: str, ttl: int) -> None: ...

    async def remove_heartbeat(self, worker_id: str, instance_id: str) -> None: ...


async def run_worker(
    readiness: Callable[[], Awaitable[dict[str, str]]],
    heartbeats: Heartbeats,
    *,
    worker_id: str,
    interval: float,
    ttl: int,
    stop: asyncio.Event | None = None,
    metrics: Callable[[], dict] | None = None,
    extra_consumers: dict[str, Callable[[], Awaitable[int]]] | None = None,
    consume: Callable[[], Awaitable[int]] | None = None,
) -> None:
    owns_signals = stop is None
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if owns_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
    instance_id = str(uuid4())
    previous = None
    consumers = dict(extra_consumers or {})
    if consume:
        consumers["billing_consumer"] = consume
    health = dict.fromkeys(consumers, "ok")

    async def consumer_loop(name, callback):
        while not stop.is_set():
            try:
                count = await callback()
                health[name] = "ok"
                if count:
                    continue
            except Exception as error:
                health[name] = "unavailable"
                event(
                    logger,
                    logging.WARNING,
                    "worker.consumer_failed",
                    consumer=name,
                    error_type=type(error).__name__,
                )
            try:
                idle = 0.1 if name == "settlement" and health[name] == "ok" else 0.5
                await asyncio.wait_for(stop.wait(), timeout=idle)
            except TimeoutError:
                pass

    tasks = [
        asyncio.create_task(consumer_loop(name, callback)) for name, callback in consumers.items()
    ]
    try:
        while not stop.is_set():
            checks = await readiness()
            checks = {**checks, **health}
            ready = bool(checks) and all(value == "ok" for value in checks.values())
            payload = json.dumps(
                {
                    "instance_id": instance_id,
                    "ready": ready,
                    "checks": checks,
                    **({"metrics": metrics()} if metrics else {}),
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
            try:
                await heartbeats.write_heartbeat(worker_id, payload, ttl)
            except Exception:
                ready = False
            if ready != previous:
                event(logger, logging.INFO, "worker.readiness", ready=ready)
                previous = ready
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await heartbeats.remove_heartbeat(worker_id, instance_id)
        except Exception:
            event(logger, logging.WARNING, "worker.heartbeat_cleanup_failed")
        if owns_signals:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)


async def serve_worker(settings: RuntimeSettings) -> None:
    application = Application(settings)
    try:
        await run_worker(
            application.readiness,
            application.cache,
            worker_id=settings.worker_id,
            interval=settings.worker_interval,
            ttl=settings.worker_heartbeat_ttl,
            metrics=application.database.metrics.snapshot,
            consume=application.process_billing,
            extra_consumers={
                "report_projection": application.reports.project,
                "identity_mail": application.identity.dispatch_email,
                "exports": application.reports.process_export,
                "account_reconciliation": application.operations.check_next,
                "settlement": application.usage.process,
                "publication_outbox": application.publication.distribute,
            },
        )
    finally:
        await application.close()
