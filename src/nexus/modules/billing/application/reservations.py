"""Billing transaction primitives used by gateway admission and the settlement worker."""

from decimal import Decimal

from nexus.shared.errors import ServiceError

from ..domain.money import usage_amount
from .common import digest, found, view
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
            row = await repo.reserve_funds(
                admission, fingerprint, model.dimensions, await repo.now()
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
            return await repo.begin_attempt(
                org_id, request_id, attempt_id, deployment_id, deployment.model_id
            )

    async def record_cost(self, org_id, request_id, attempt_id, quantities, evidence_reference):
        if (
            not isinstance(evidence_reference, str)
            or not 1 <= len(evidence_reference.strip()) <= 255
        ):
            raise ServiceError("invalid_evidence", "Cost evidence reference is required", 422)
        async with self._transactions() as repo:
            await repo.account(org_id)
            found(await repo.get("request", request_id, org_id, lock=True))
            attempt = found(await repo.get("attempt", attempt_id, lock=True))
            if attempt.request_id != request_id:
                found(None)
            cost = found(await repo.get("price", attempt.cost_price_version_id))
            return await self.record_cost_in(
                repo, org_id, attempt, cost, quantities, evidence_reference
            )

    async def record_cost_in(self, repo, org_id, attempt, cost, quantities, evidence_reference):
        fingerprint = digest({"quantities": quantities, "evidence_reference": evidence_reference})
        if attempt.usage_digest:
            if attempt.usage_digest != fingerprint:
                raise ServiceError(
                    "cost_conflict", "Attempt cost was confirmed with different evidence", 409
                )
            return {
                "attempt_id": attempt.id,
                "cost_amount": format(attempt.cost_amount, ".12f"),
            }
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
            attempt.id,
            org_id,
            {"evidence_reference": evidence_reference, "amount": str(amount)},
        )
        return {"attempt_id": attempt.id, "cost_amount": format(amount, ".12f")}

    async def _review(self, repo, account, request, reason, facts, *, freeze=False):
        await repo.edit(request, {"state": "review", "review_reason": reason}, versioned=False)
        if freeze:
            await repo.restrict(account, "request", request.id)
        await repo.audit(
            None,
            "request.review_required",
            request.id,
            account.organization_id,
            {"reason": reason, "facts": facts.model_dump(mode="json")},
        )
        return view(RequestView, request)

    async def settle(self, org_id, request_id, facts):
        async with self._transactions() as repo:
            account = await repo.account(org_id)
            request = found(await repo.get("request", request_id, org_id, lock=True))
            attempts = (
                await repo.attempts(request_id) if request.reservation_state == "held" else ()
            )
            return await self.settle_in(repo, account, request, {a.id for a in attempts}, facts)

    async def settle_in(self, repo, account, request, attempt_ids, facts):
        fingerprint = digest(facts.model_dump(mode="json"))
        if request.reservation_state != "held":
            if request.settlement_digest != fingerprint:
                raise ServiceError(
                    "settlement_conflict",
                    "A settled request requires an adjustment to change its financial result",
                    409,
                )
            return view(RequestView, request)
        if facts.outcome == "unknown":
            return await self._review(repo, account, request, "unknown_outcome", facts)
        if facts.outcome == "not_started" and attempt_ids:
            return await self._review(repo, account, request, "attempt_may_have_executed", facts)
        amount, lines = Decimal(0), []
        if facts.outcome in {"success", "customer_cancelled"}:
            if not facts.quantities or facts.delivered_attempt_id not in attempt_ids:
                return await self._review(repo, account, request, "missing_final_usage", facts)
            price, discount = await repo.settlement_prices(request)
            if set(facts.quantities) != set(price.rates):
                return await self._review(repo, account, request, "invalid_final_usage", facts)
            input_used = sum(
                value
                for dimension, value in facts.quantities.items()
                if dimension != "output_tokens"
            )
            if (
                input_used > request.input_limit
                or facts.quantities["output_tokens"] > request.output_limit
            ):
                return await self._review(
                    repo, account, request, "authorized_bound_exceeded", facts, freeze=True
                )
            try:
                amount, lines = usage_amount(price.rates, facts.quantities, discount.factor)
            except ValueError, TypeError:
                return await self._review(repo, account, request, "invalid_final_usage", facts)
            if amount > request.reserved_amount:
                return await self._review(
                    repo, account, request, "authorized_bound_exceeded", facts, freeze=True
                )
        return await self.post_settlement(
            repo,
            account,
            request,
            amount,
            lines,
            fingerprint,
            captured=facts.outcome in {"success", "customer_cancelled"},
            canceled=facts.outcome == "not_started",
            evidence=facts.model_dump(mode="json"),
        )

    async def post_settlement(
        self,
        repo,
        account,
        request,
        amount,
        lines,
        fingerprint,
        *,
        captured,
        canceled,
        evidence=None,
    ):
        request_id = request.id
        org_id = request.organization_id
        await repo.settle_budget_holds(request_id, amount)
        account.held_amount -= request.reserved_amount
        account.version += 1
        await repo.append_entry(account, -amount, "usage_debit", "request", request_id)
        await repo.edit(
            request,
            {
                "state": "canceled" if canceled else "settled",
                "reservation_state": "captured" if captured else "released",
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
            {"amount": str(amount), "facts": evidence or {"source": "review"}},
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
