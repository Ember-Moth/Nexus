"""Normalize allowlisted evidence and settle durable facts inside the billing transaction."""

from uuid import UUID

from nexus.shared.errors import ServiceError

from ..domain.usage import normalize
from .schemas import Settlement


class UsageProcessor:
    def __init__(self, transactions, reservations):
        self._transactions = transactions
        self._reservations = reservations

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

    async def process(self, limit=16):
        count = 0
        for _ in range(limit):
            candidate_id = None
            try:
                async with self._transactions() as repo:
                    candidate = await repo.claim_usage()
                    if candidate is None:
                        break
                    account, request, generation = candidate
                    candidate_id = request.id
                    await self._process(repo, account, request)
            except Exception:
                if candidate_id is None:
                    raise
                async with self._transactions() as repo:
                    await repo.retry_usage(candidate_id, generation)
                raise
            count += 1
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
