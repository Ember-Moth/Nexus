"""Internal prepaid primitives, not an externally callable gateway authorization API.

The future gateway boundary supplies authenticated identity and normalized evidence.
Each method here holds locks only for the short billing transaction.
"""

from datetime import timedelta
from decimal import Decimal

from nexus.shared.errors import ServiceError

from .common import digest, found, view
from .money import reserve_amount, usage_amount
from .schemas import RequestView


class ReservationService:
    def __init__(self, transactions, catalog):
        self._transactions, self._catalog = transactions, catalog

    async def reserve(self, admission):
        fingerprint = digest(admission.model_dump(mode="json"))
        # Cross-module reads finish before acquiring any billing connection or lock.
        model = await self._catalog.model(admission.model_id)
        async with self._transactions() as repo:
            account = await repo.account(admission.organization_id)
            previous = await repo.get("request", admission.request_id)
            if previous:
                if previous.request_digest != fingerprint or previous.account_id != account.id:
                    raise ServiceError(
                        "request_conflict",
                        "Request ID was used for different authorization facts",
                        409,
                    )
                return view(RequestView, previous)
            if account.status != "active":
                raise ServiceError("account_frozen", "Account requires financial review", 409)
            model = found(model)
            if model.status != "active" or not model.configured:
                raise ServiceError(
                    "model_unavailable", "Model is not configured for admission", 409
                )
            if (
                admission.input_limit > model.max_input_tokens
                or admission.output_limit > model.max_output_tokens
            ):
                raise ServiceError(
                    "invalid_bounds", "Token bounds exceed the public model limits", 422
                )
            now = await repo.now()
            price = await repo.current_price("sale", admission.model_id, now)
            if price is None or not price.enabled or set(price.rates) != model.dimensions:
                raise ServiceError(
                    "price_unavailable", "No complete active sale price is available", 409
                )
            discount = await repo.current_discount(admission.organization_id, now)
            try:
                amount = reserve_amount(
                    price.rates, admission.input_limit, admission.output_limit, discount.factor
                )
            except ValueError:
                raise ServiceError(
                    "invalid_reserve_amount",
                    "The calculated reservation is outside supported bounds",
                    422,
                ) from None
            if account.balance - account.held_amount < amount:
                raise ServiceError("insufficient_funds", "Available balance is insufficient", 409)
            periods = []
            for policy in await repo.applicable_budgets(admission):
                period = await repo.budget_period(policy, now)
                if period.spent + period.reserved + amount > period.limit_amount:
                    raise ServiceError(
                        "budget_exceeded", "An applicable amount budget is exhausted", 409
                    )
                periods.append(period)
            row = await repo.create(
                "request",
                {
                    "id": admission.request_id,
                    "organization_id": admission.organization_id,
                    "account_id": account.id,
                    "project_id": admission.project_id,
                    "service_account_id": admission.service_account_id,
                    "api_key_id": admission.api_key_id,
                    "model_id": admission.model_id,
                    "request_digest": fingerprint,
                    "sale_price_version_id": price.id,
                    "discount_version_id": discount.id,
                    "billing_policy_version": "customer_usage_v1",
                    "input_limit": admission.input_limit,
                    "output_limit": admission.output_limit,
                    "reserved_amount": amount,
                    "expires_at": now + timedelta(seconds=admission.ttl_seconds),
                    "created_at": now,
                },
            )
            for period in periods:
                await repo.add_budget_hold(period, row.id, amount)
            await repo.edit(account, {"held_amount": account.held_amount + amount})
            await repo.audit(
                None,
                "request.reserved",
                row.id,
                account.organization_id,
                {
                    "amount": str(amount),
                    "sale_price_version_id": str(price.id),
                    "discount_version_id": str(discount.id),
                },
            )
            return view(RequestView, row)

    async def start_attempt(self, org_id, request_id, attempt_id, deployment_id):
        deployment = await self._catalog.deployment(deployment_id)
        async with self._transactions() as repo:
            account = await repo.account(org_id)
            request = found(await repo.get("request", request_id, org_id, lock=True))
            previous = await repo.get("attempt", attempt_id)
            if previous:
                if previous.request_id != request_id or previous.deployment_id != deployment_id:
                    raise ServiceError(
                        "attempt_conflict", "Attempt ID was used for a different attempt", 409
                    )
                return {
                    "attempt_id": previous.id,
                    "cost_price_version_id": previous.cost_price_version_id,
                    "state": previous.state,
                }
            if account.status != "active":
                raise ServiceError("account_frozen", "Account requires financial review", 409)
            if (
                request.reservation_state != "held"
                or request.state == "review"
                or request.expires_at <= await repo.now()
            ):
                raise ServiceError(
                    "request_not_executable", "Request cannot start another attempt", 409
                )
            deployment = found(deployment)
            if deployment.model_id != request.model_id or not deployment.configured:
                raise ServiceError(
                    "deployment_unavailable",
                    "Deployment is not available for the authorized model",
                    409,
                )
            cost = await repo.current_price("cost", deployment_id, await repo.now())
            sale = found(await repo.get("price", request.sale_price_version_id))
            if cost is None or not cost.enabled or set(cost.rates) != set(sale.rates):
                raise ServiceError(
                    "cost_unavailable", "No complete active cost price is available", 409
                )
            await repo.create(
                "attempt",
                {
                    "id": attempt_id,
                    "request_id": request_id,
                    "deployment_id": deployment_id,
                    "cost_price_version_id": cost.id,
                },
            )
            await repo.edit(request, {"state": "running"}, versioned=False)
            await repo.audit(
                None,
                "attempt.started",
                attempt_id,
                org_id,
                {"request_id": str(request_id), "cost_price_version_id": str(cost.id)},
            )
            return {"attempt_id": attempt_id, "cost_price_version_id": cost.id, "state": "started"}

    async def record_cost(self, org_id, request_id, attempt_id, quantities, evidence_reference):
        if (
            not isinstance(evidence_reference, str)
            or not 1 <= len(evidence_reference.strip()) <= 255
        ):
            raise ServiceError("invalid_evidence", "Cost evidence reference is required", 422)
        fingerprint = digest({"quantities": quantities, "evidence_reference": evidence_reference})
        async with self._transactions() as repo:
            await repo.account(org_id)
            found(await repo.get("request", request_id, org_id, lock=True))
            attempt = found(await repo.get("attempt", attempt_id, lock=True))
            if attempt.request_id != request_id:
                found(None)
            if attempt.usage_digest:
                if attempt.usage_digest != fingerprint:
                    raise ServiceError(
                        "cost_conflict", "Attempt cost was confirmed with different evidence", 409
                    )
                return {
                    "attempt_id": attempt_id,
                    "cost_amount": format(attempt.cost_amount, ".12f"),
                }
            cost = found(await repo.get("price", attempt.cost_price_version_id))
            try:
                amount, lines = usage_amount(cost.rates, quantities)
            except ValueError, TypeError:
                raise ServiceError(
                    "invalid_cost_usage", "Cost usage does not match its frozen price", 422
                ) from None
            await repo.edit(
                attempt,
                {
                    "state": "confirmed",
                    "cost_amount": amount,
                    "cost_details": lines,
                    "usage_digest": fingerprint,
                },
                versioned=False,
            )
            await repo.audit(
                None,
                "attempt.cost_confirmed",
                attempt_id,
                org_id,
                {"evidence_reference": evidence_reference, "amount": str(amount)},
            )
            return {"attempt_id": attempt_id, "cost_amount": format(amount, ".12f")}

    async def _review(self, repo, account, request, reason, facts, *, freeze=False):
        await repo.edit(request, {"state": "review", "review_reason": reason}, versioned=False)
        if freeze:
            await repo.edit(account, {"status": "frozen"})
        await repo.audit(
            None,
            "request.review_required",
            request.id,
            account.organization_id,
            {"reason": reason, "facts": facts.model_dump(mode="json")},
        )
        return view(RequestView, request)

    async def settle(self, org_id, request_id, facts):
        fingerprint = digest(facts.model_dump(mode="json"))
        async with self._transactions() as repo:
            account = await repo.account(org_id)
            request = found(await repo.get("request", request_id, org_id, lock=True))
            if request.reservation_state != "held":
                if request.settlement_digest != fingerprint:
                    raise ServiceError(
                        "settlement_conflict",
                        "A settled request requires an adjustment to change its financial result",
                        409,
                    )
                return view(RequestView, request)
            attempts = await repo.attempts(request_id)
            if facts.outcome == "unknown":
                return await self._review(repo, account, request, "unknown_outcome", facts)
            if facts.outcome == "not_started" and attempts:
                return await self._review(
                    repo, account, request, "attempt_may_have_executed", facts
                )
            amount, lines = Decimal(0), []
            if facts.outcome in {"success", "customer_cancelled"}:
                if not facts.quantities or not any(
                    item.id == facts.delivered_attempt_id for item in attempts
                ):
                    return await self._review(repo, account, request, "missing_final_usage", facts)
                price = found(await repo.get("price", request.sale_price_version_id))
                discount = found(await repo.get("discount", request.discount_version_id))
                try:
                    amount, lines = usage_amount(price.rates, facts.quantities, discount.factor)
                except ValueError, TypeError:
                    return await self._review(repo, account, request, "invalid_final_usage", facts)
                input_used = sum(
                    value
                    for dimension, value in facts.quantities.items()
                    if dimension != "output_tokens"
                )
                if (
                    input_used > request.input_limit
                    or facts.quantities["output_tokens"] > request.output_limit
                    or amount > request.reserved_amount
                ):
                    return await self._review(
                        repo, account, request, "authorized_bound_exceeded", facts, freeze=True
                    )
            account.held_amount -= request.reserved_amount
            account.version += 1
            await repo.settle_budget_holds(request_id, amount)
            await repo.append_entry(account, -amount, "usage_debit", "request", request_id)
            await repo.edit(
                request,
                {
                    "state": "canceled" if facts.outcome == "not_started" else "settled",
                    "reservation_state": "captured"
                    if facts.outcome in {"success", "customer_cancelled"}
                    else "released",
                    "charged_amount": amount,
                    "charge_details": lines,
                    "settlement_digest": fingerprint,
                    "settled_at": await repo.now(),
                    "review_reason": None,
                },
                versioned=False,
            )
            await repo.audit(
                None,
                "request.settled",
                request.id,
                org_id,
                {"amount": str(amount), "facts": facts.model_dump(mode="json")},
            )
            return view(RequestView, request)

    async def mark_expired(self, limit=100):
        async with self._transactions() as repo:
            candidates = await repo.expired_requests(limit)
        changed = 0
        for org_id, request_id in candidates:
            async with self._transactions() as repo:
                await repo.account(org_id)
                request = found(await repo.get("request", request_id, org_id, lock=True))
                if (
                    request.reservation_state == "held"
                    and request.review_reason is None
                    and request.expires_at <= await repo.now()
                ):
                    await repo.edit(
                        request,
                        {"state": "review", "review_reason": "reservation_expired"},
                        versioned=False,
                    )
                    await repo.audit(
                        None,
                        "request.review_required",
                        request.id,
                        org_id,
                        {"reason": "reservation_expired"},
                    )
                    changed += 1
        return changed
