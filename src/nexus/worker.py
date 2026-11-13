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
    consumer_healthy = True

    async def consumer_loop():
        nonlocal consumer_healthy
        while not stop.is_set():
            try:
                count = await consume()
                consumer_healthy = True
                if count:
                    continue
            except Exception as error:
                consumer_healthy = False
                logger.warning("billing consumer failed: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    consumer_task = asyncio.create_task(consumer_loop()) if consume else None
    try:
        while not stop.is_set():
            checks = await readiness()
            if consume:
                checks = {**checks, "billing_consumer": "ok" if consumer_healthy else "unavailable"}
            ready = bool(checks) and all(value == "ok" for value in checks.values())
            payload = json.dumps(
                {
                    "instance_id": instance_id,
                    "ready": ready,
                    "checks": checks,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
            try:
                await heartbeats.write_heartbeat(worker_id, payload, ttl)
            except Exception:
                ready = False
            if ready != previous:
                logger.info("worker readiness=%s", ready)
                previous = ready
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        if consumer_task:
            consumer_task.cancel()
            await asyncio.gather(consumer_task, return_exceptions=True)
        try:
            await heartbeats.remove_heartbeat(worker_id, instance_id)
        except Exception:
            logger.warning("worker heartbeat cleanup unavailable")
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
            consume=application.process_billing,
        )
    finally:
        await application.close()
