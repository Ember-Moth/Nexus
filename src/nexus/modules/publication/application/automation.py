"""Publish a cluster after its sources change, once they have been quiet for a short window.

A cluster is pending while its probe vector differs from the vector its desired snapshot
represents (``settled``). The release waits until the probe has not moved for ``quiet``
seconds, but never longer than ``max_delay`` after the first unpublished change. A release
that fails validation is not retried until the sources or the cluster configuration change.
"""

import asyncio
import logging
from datetime import timedelta
from time import monotonic

from nexus.shared.errors import ServiceError
from nexus.shared.telemetry import event

logger = logging.getLogger("nexus.publication")
# Races with an administrator, moving sources and storage outages resolve on their own.
TRANSIENT = frozenset({"cluster_changed", "source_changed", "storage_unavailable"})
MIN_SPACING = 0.2


class AutoPublisher:
    def __init__(self, service, transactions, wakeups, *, quiet, max_delay, poll, lease=120):
        self.service = service
        self.tx = transactions
        # wakeups(timeout) returns once a source may have changed or the timeout elapsed.
        self.wakeups = wakeups
        self.quiet = timedelta(seconds=quiet)
        self.max_delay = timedelta(seconds=max_delay)
        self.retry = timedelta(seconds=max(quiet, 1.0))
        self.poll = poll
        self.lease = lease
        self._wake_at = monotonic()
        self._ticked_at = None

    async def step(self):
        """One worker iteration: wait for a change or a due cluster, then advance all."""
        wait = min(max(self._wake_at - monotonic(), 0.0), self.poll)
        if wait:
            await self.wakeups(wait)
        if self._ticked_at is not None:
            # Any database login can NOTIFY; a flood must not turn into a busy loop.
            await asyncio.sleep(max(self._ticked_at + MIN_SPACING - monotonic(), 0.0))
        self._ticked_at = monotonic()
        published, delay = await self.tick()
        self._wake_at = monotonic() + (self.poll if delay is None else delay)
        return published

    async def tick(self):
        """Advance every enabled cluster; returns releases made and seconds to the next due."""
        async with self.tx() as repo:
            clusters = await repo.automated_clusters()
        published, delay, revisions = 0, None, None
        for cid in clusters:
            try:
                async with self.tx() as repo:
                    claimed = await repo.claim_automation(cid, self.lease)
                if claimed is None:
                    continue
                cluster, state = claimed
                if revisions is None:
                    revisions = await self.service.revisions(cluster)
                made, due = await self._advance(cluster, state, revisions)
            except Exception as error:
                # One cluster's storage or source failure does not hold back the others.
                code = getattr(error, "code", "internal_error")
                event(
                    logger,
                    logging.WARNING,
                    "publication.auto_publish_error",
                    cluster_id=cid,
                    error_code=code,
                    error_type=type(error).__name__,
                )
                await self._defer(cid, code, type(error).__name__)
                continue
            published += made
            if due is not None:
                seconds = max((due - state["now"]).total_seconds(), 0.0)
                delay = seconds if delay is None else min(delay, seconds)
        return published, delay

    async def _defer(self, cid, code, detail):
        """Record an unexpected failure and retry after the backoff, keeping any pending change."""
        try:
            async with self.tx() as repo:
                await repo.defer(cid, code, f"Unexpected {detail}", self.retry.total_seconds())
        except Exception:
            # Storage is unavailable; the lease expires on its own.
            pass

    async def _advance(self, cluster, state, revisions):
        cid, now = cluster["id"], state["now"]
        probe = await self.service.probe(cluster, revisions, state["price_models"])
        failure = {"sources": probe, "version": cluster["version"]}
        if probe == state["settled"]:
            await self._track(
                cid,
                observed=None,
                changed_at=None,
                pending_since=None,
                retry_after=None,
                failed=None,
                checked_at=now,
            )
            return 0, None
        if failure == state["failed"]:
            await self._track(cid, checked_at=now)
            return 0, None
        observed, changed_at = probe, state["changed_at"]
        if probe != state["observed"] or changed_at is None:
            changed_at = now
        pending_since = state["pending_since"] or now
        due = min(changed_at + self.quiet, pending_since + self.max_delay)
        if state["retry_after"]:
            due = max(due, state["retry_after"])
        if now < due:
            await self._track(
                cid,
                observed=observed,
                changed_at=changed_at,
                pending_since=pending_since,
                failed=None,
                checked_at=now,
            )
            return 0, due
        try:
            release = await self.service.publish_automatically(cluster, state["settled"])
        except ServiceError as error:
            if error.code in TRANSIENT:
                retry = now + self.retry
                await self._track(
                    cid,
                    observed=observed,
                    changed_at=changed_at,
                    pending_since=pending_since,
                    retry_after=retry,
                    checked_at=now,
                    **(
                        {}
                        if error.code == "cluster_changed"
                        else self._failure(error.code, str(error), now)
                    ),
                )
                event(
                    logger,
                    logging.INFO,
                    "publication.auto_publish_deferred",
                    cluster_id=cid,
                    error_code=error.code,
                )
                return 0, retry
            await self._track(
                cid,
                observed=None,
                changed_at=None,
                pending_since=None,
                retry_after=None,
                failed=failure,
                checked_at=now,
                **self._failure(error.code, str(error), now),
            )
            event(
                logger,
                logging.WARNING,
                "publication.auto_publish_failed",
                cluster_id=cid,
                error_code=error.code,
            )
            return 0, None
        # The commit settled the cluster and cleared its pending change.
        await self._track(cid, checked_at=now)
        event(
            logger,
            logging.INFO,
            "publication.auto_published",
            cluster_id=cid,
            revision=release and release["revision"],
            outcome="published" if release else "unchanged",
        )
        return int(release is not None), None

    @staticmethod
    def _failure(code, message, now):
        return {"failure_code": code, "failure_message": message[:500], "failure_at": now}

    async def _track(self, cid, **values):
        async with self.tx() as repo:
            await repo.track(cid, **values)
