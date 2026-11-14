"""Normalize allowlisted evidence and settle durable facts inside the billing transaction."""

from uuid import UUID

from nexus.shared.errors import ServiceError

from .schemas import Settlement

RULE = "fixture.tokens.v1/1"


def normalize(payload, dimensions):
    raw = payload["raw_usage"]
    if raw is None:
        raise ValueError("missing_usage")
    if not isinstance(raw, dict) or any(
        type(v) is not int or not 0 <= v <= 1_000_000_000 for v in raw.values()
    ):
        raise ValueError("invalid_token_count")
    if payload["usage_format"] == "fixture.dimensions.v1":
        if set(raw) != set(dimensions):
            raise ValueError("dimension_mismatch")
        return "fixture.dimensions.v1/1", raw
    if payload["usage_format"] != "fixture.tokens.v1":
        raise ValueError("unknown_usage_format")
    inputs = {
        "cached_input_tokens": "input_cache_read_tokens",
        "cache_write_5m_tokens": "input_cache_write_5m_tokens",
        "cache_write_1h_tokens": "input_cache_write_1h_tokens",
    }
    if not {
        "input_total_tokens",
        "cached_input_tokens",
        "output_tokens",
    } <= raw.keys() or raw.keys() - inputs.keys() - {
        "input_total_tokens",
        "output_tokens",
        "reasoning_tokens",
    }:
        raise ValueError("unsupported_usage_fields")
    result = {
        "output_tokens": raw["output_tokens"],
        "input_uncached_tokens": raw["input_total_tokens"],
    }
    for field, dimension in inputs.items():
        value = raw.get(field, 0)
        if dimension in dimensions:
            if field not in raw:
                raise ValueError("missing_cache_counter")
            result[dimension] = value
        elif value:
            raise ValueError("dimension_mismatch")
        result["input_uncached_tokens"] -= value
    if result["input_uncached_tokens"] < 0 or raw.get("reasoning_tokens", 0) > raw["output_tokens"]:
        raise ValueError("overlapping_token_counts")
    if set(result) != set(dimensions):
        raise ValueError("dimension_mismatch")
    return RULE, result


class UsageProcessor:
    def __init__(self, transactions, reservations):
        self._transactions = transactions
        self._reservations = reservations

    async def process(self, limit=16):
        count = 0
        for _ in range(limit):
            candidate_id = None
            try:
                async with self._transactions() as repo:
                    candidate = await repo.claim_usage()
                    if candidate is None:
                        break
                    request, generation = candidate
                    candidate_id = request.id
                    await self._process(repo, request)
            except Exception:
                if candidate_id is None:
                    raise
                async with self._transactions() as repo:
                    await repo.retry_usage(candidate_id, generation)
                raise
            count += 1
        return count

    async def _process(self, repo, request):
        finished = {}
        closed = None
        pending_cost = False
        corrected = await repo.corrected_attempts(request.id)
        for event in await repo.usage_facts(request.id):
            if event.kind == "request.closed":
                closed = event
                continue
            attempt = await repo.get("attempt", event.attempt_id)
            price = await repo.get("price", attempt.cost_price_version_id)
            payload = event.envelope["payload"]
            try:
                rule, counts = normalize(payload, price.rates)
                reason = None
            except ValueError as error:
                rule, counts, reason = "usage-normalizer/1", None, str(error)
            if counts is not None and event.attempt_id not in corrected:
                try:
                    await self._reservations.record_cost_in(
                        repo,
                        request.organization_id,
                        request.id,
                        event.attempt_id,
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
            for attempt in await repo.attempts(request.id):
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
        result = await self._reservations.settle_in(
            repo, request.organization_id, request.id, facts
        )
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
