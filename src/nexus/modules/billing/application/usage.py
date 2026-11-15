"""Normalize allowlisted evidence and settle durable facts inside the billing transaction."""

from uuid import UUID

from nexus.shared.errors import ServiceError

from ..domain.usage import declined, failed_over, normalize
from .schemas import Settlement


class UsageProcessor:
    def __init__(self, transactions, reservations):
        self._transactions = transactions
        self._reservations = reservations

    async def renew_storage(self, limit=16):
        count = 0
        for _ in range(limit):
            async with self._transactions() as repo:
                policy = repo.policy
                if not await repo.renew_storage(
                    policy.vector_storage_prepaid_days, policy.vector_storage_lapse_policy
                ):
                    break
            count += 1
        return count

    async def stage_storage(self, limit=16):
        count = 0
        for _ in range(limit):
            async with self._transactions() as repo:
                if not await repo.stage_storage():
                    break
            count += 1
        return count

    async def review_containers(self, limit=16):
        count = 0
        for _ in range(limit):
            async with self._transactions() as repo:
                if not await repo.review_container():
                    break
            count += 1
        return count

    async def process(self, limit=64):
        """Settle up to ``limit`` due jobs, one wallet per transaction, and return how many.

        A transaction locks one wallet and up to ``batch_size`` of its due jobs, so a busy
        wallet is locked once per batch instead of once per request. Each request still gets
        the same per-request decisions and posting; if any of them fails, the batch rolls back
        and every request is settled again in its own transaction, so only the failing one is
        retried later.
        """
        count = 0
        while count < limit:
            claimed = []
            try:
                async with self._transactions() as repo:
                    claim = await repo.claim_usage(min(self.batch_size, limit - count))
                    if claim is None:
                        break
                    account, rows, now = claim
                    # Plain ids: a rolled-back session expires its rows.
                    claimed = [request.id for request, _ in rows]
                    await self._settle(repo, account, [request for request, _ in rows], now)
            except Exception:
                if not claimed:
                    raise
                isolated = await self._isolate(claimed)
                count += isolated
                if not isolated:
                    # Every request was skipped (wallet busy elsewhere): retry on the next call
                    # instead of claiming and failing the same batch again.
                    break
                continue
            count += len(claimed)
        return count

    batch_size = 32

    async def _settle(self, repo, account, requests, now):
        async with repo.usage_batch(requests, now):
            for request in requests:
                await self._process(repo, account, request)

    async def _isolate(self, request_ids):
        """Settle each request of a failed batch alone; reschedule the ones that fail again."""
        count, failure = 0, None
        for request_id in request_ids:
            generation = None
            try:
                async with self._transactions() as repo:
                    claim = await repo.claim_usage(1, request_id)
                    if claim is None:
                        # Settled by another Worker, waiting for a new fact, or its wallet is
                        # busy: it stays due and is claimed again later.
                        continue
                    account, [(request, generation)], now = claim
                    await self._settle(repo, account, [request], now)
            except Exception as error:
                if generation is None:
                    raise
                failure = failure or error
                async with self._transactions() as repo:
                    await repo.retry_usage(request_id, generation)
                continue
            count += 1
        if failure is not None:
            raise failure
        return count

    async def _process(self, repo, account, request):
        finished = {}
        closed = None
        pending_cost = False
        priced = {
            attempt.id: (attempt, price)
            for attempt, price in await repo.priced_attempts(request.id)
        }
        corrected = await repo.corrected_attempts(request.id)
        for event in await repo.usage_facts(request.id):
            if event.kind == "request.closed":
                closed = event
                continue
            attempt, price = priced[event.attempt_id]
            payload = event.envelope["payload"]
            try:
                rule, counts = normalize(
                    payload,
                    price.rates,
                    attempt.expected_usage_format,
                    search_limit=request.web_search_limit,
                    file_limit=request.file_search_limit,
                    guardrail_limit=request.guardrail_unit_limit,
                    container_unit=request.container_unit,
                    execution_mode=(
                        "none"
                        if request.billing_policy_version == "anthropic_message_batch_v1"
                        and payload["outcome"] == "failed"
                        and (payload["raw_usage"] or {}).get("code_execution_requests") == 0
                        else request.execution_mode
                    ),
                )
                reason = None
            except ValueError as error:
                rule, counts, reason = "usage-normalizer/1", None, str(error)
            if payload["outcome"] == "declined" or (
                reason == "missing_usage"
                and payload["outcome"] == "failed"
                and failed_over(attempt, priced.values())
            ):
                # Proven not executed: the node says so, or it failed over to a later attempt.
                (rule, counts), reason = declined(price.rates), None
            if counts is not None and event.attempt_id not in corrected:
                try:
                    await self._reservations.record_cost_in(
                        repo,
                        request,
                        attempt,
                        price,
                        counts,
                        str(event.id),
                    )
                except ServiceError as error:
                    if error.code != "invalid_cost_usage":
                        raise
                    reason = "invalid_cost_usage"
            if reason and event.attempt_id not in corrected:
                pending_cost = True
                await repo.cost_review(request, event.attempt_id, reason)
            await repo.save_normalization(event.id, event.attempt_id, rule, counts, reason)
            finished[str(event.attempt_id)] = (payload["outcome"], counts, rule, reason)
        if request.review_decision_id:
            for attempt, _ in priced.values():
                if attempt.state != "confirmed" and attempt.id not in corrected:
                    pending_cost = True
                    await repo.cost_review(request, attempt.id, "missing_attempt_cost")
            return await repo.finish_usage(
                request.id,
                "review" if pending_cost else "done",
                "cost_requires_review" if pending_cost else None,
            )
        if closed is None:
            return await repo.finish_usage(
                request.id,
                "review" if pending_cost else "waiting",
                "cost_requires_review" if pending_cost else "awaiting_close",
            )
        body = closed.envelope["payload"]
        outcome = body["delivery_outcome"]
        delivery = str(UUID(body["delivered_attempt_id"])) if body["delivered_attempt_id"] else None
        final = finished.get(delivery)
        if outcome in {"succeeded", "client_cancelled"} and delivery and final is None:
            return await repo.finish_usage(request.id, "waiting", "awaiting_delivered_attempt")
        normalized = {
            "succeeded": "success",
            "client_cancelled": "customer_cancelled",
            "upstream_failed": "upstream_failed",
            "unknown": "unknown",
        }[outcome]
        if final and (
            (outcome == "succeeded" and final[0] != "succeeded")
            or (outcome == "client_cancelled" and final[0] not in {"succeeded", "cancelled"})
        ):
            normalized = "unknown"
        if normalized in {"upstream_failed", "unknown"}:
            delivery = None
            final = None
        if outcome == "client_cancelled" and not body["attempt_ids"]:
            normalized = "not_started"
        facts = Settlement(
            outcome=normalized,
            delivered_attempt_id=delivery,
            quantities=final[1]
            if final and normalized in {"success", "customer_cancelled"}
            else None,
            normalization_version=final[2]
            if final and normalized in {"success", "customer_cancelled"}
            else "customer_usage_v1",
            evidence_reference=str(closed.id),
        )
        result = await self._reservations.settle_in(repo, account, request, priced.keys(), facts)
        if result.state == "review" or pending_cost:
            return await repo.finish_usage(
                request.id, "review", result.review_reason or "cost_requires_review"
            )
        missing = {UUID(i) for i in body["attempt_ids"]} - set(map(UUID, finished)) - corrected
        if missing:
            for aid in missing:
                await repo.cost_review(request, aid, "missing_attempt_cost")
            return await repo.finish_usage(request.id, "waiting", "awaiting_attempt_cost")
        await repo.finish_usage(request.id, "done", None)
