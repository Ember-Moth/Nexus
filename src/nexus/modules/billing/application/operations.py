from decimal import Decimal
from uuid import UUID, uuid4

from nexus.shared.errors import ServiceError

from ..domain.money import usage_amount, wallet_value
from .common import digest, found, replay, versioned


def conflict(message):
    raise ServiceError("case_conflict", message, 409)


class Operations:
    def __init__(self, transactions, access, reservations):
        self.tx, self.access, self.reservations = transactions, access, reservations

    async def list(self, token, org, state, limit, cursor):
        await self.access.authorize(token, org, operation="finance")
        async with self.tx() as repo:
            return await repo.cases(org, state, limit, cursor)

    async def detail(self, token, id):
        await self.access.authorize(token, operation="finance")
        async with self.tx() as repo:
            rows = (await repo.cases(None, None, 1, None, id))["items"]
            case = found(rows[0] if rows else None)
            return await repo.case_detail(case)

    async def statement(self, token, body, key):
        scope = await self.access.authorize(token, body.organization_id, operation="finance")
        async with self.tx() as repo:
            action = f"{body.organization_id}:upstream-statement"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, body.model_dump(mode="json")
            )
            if old:
                return {"statement_id": old.resource_id}
            account = await repo.account(body.organization_id)
            if account.currency != body.currency:
                raise ServiceError("currency_mismatch", "Statement currency differs", 422)
            attempt = found(await repo.get("attempt", body.attempt_id))
            request = found(await repo.get("request", attempt.request_id, body.organization_id))
            reference = uuid4()
            compared = await repo.latest_cost(attempt)
            await repo.insert_statement(reference, body, attempt.id, compared, scope.actor_id)
            if body.amount != compared:
                await repo.cost_review(request, attempt.id, "statement_difference")
                await repo.attach_statement(attempt.id, reference)
            await repo.audit(
                scope.actor_id,
                "statement.registered",
                reference,
                body.organization_id,
                {"reason": body.reason},
            )
            await repo.remember(scope.actor_id, action, kd, rd, "statement", reference)
            return {"statement_id": reference}

    async def resolve(self, token, id, body, expected, key):
        actor = await self.access.authorize(token, operation="finance")
        async with self.tx() as repo:
            rows = (await repo.cases(None, None, 1, None, id))["items"]
            case = found(rows[0] if rows else None)
            org = case["organization_id"]
            scope = f"{org}:case:resolve"
            old, kd, rd = await replay(
                repo,
                actor,
                scope,
                key,
                {"id": str(id), "version": expected, **body.model_dump(mode="json")},
            )
            if old:
                return (await repo.decision(old.resource_id))["result"]
            account = await repo.account(org)
            if case["request_id"]:
                found(await repo.get("request", case["request_id"], org, lock=True))
            # All source transitions serialize on the account. Re-read after taking its lock.
            case = (await repo.cases(org, None, 1, None, id))["items"][0]
            if case["version"] != expected:
                raise ServiceError("version_conflict", "Case version changed", 412)
            if case["state"] != "open":
                conflict("Case is already resolved")
            decision = uuid4()
            result = {"decision_id": str(decision), "case_id": str(id), "action": body.action}
            kind = case["source_type"]
            if kind == "payment":
                await self._payment(repo, account, case, body, decision, result)
            elif kind == "account":
                if body.action != "retry":
                    conflict("Account differences require an authoritative recheck")
                issues = await repo.reconcile(account)
                if issues:
                    conflict("Account differences remain")
                await repo.close_case(case, decision)
                result["account_id"] = str(account.id)
            elif kind == "attempt":
                await self._cost(repo, case, body, decision, result)
            else:
                await self._request(repo, account, case, body, decision, result)
            await repo.decide(decision, case, actor, body, result)
            if kind == "request" and body.action != "retry":
                request = await repo.get("request", case["request_id"], org)
                await repo.edit(request, {"review_decision_id": decision}, versioned=False)
            await repo.audit(actor, "case." + body.action, id, org, {"decision_id": str(decision)})
            await repo.remember(actor, scope, kd, rd, "case_decision", decision)
            return result

    async def _request(self, repo, account, case, body, decision, result):
        r = found(await repo.get("request", case["request_id"], account.organization_id, lock=True))
        if r.reservation_state != "held":
            conflict("Reservation has already been settled")
        if body.action == "retry":
            # The inbox worker must decide from persisted facts.
            await repo.requeue_usage(r.id)
            result["processing_state"] = "queued"
            return
        if body.action not in {"confirm_no_charge", "supplement_usage", "waive"}:
            conflict("Unsupported usage resolution")
        amount = Decimal(0)
        lines = []
        if body.action != "confirm_no_charge":
            attempt = found(await repo.get("attempt", body.attempt_id))
            if attempt.request_id != r.id:
                raise ServiceError("not_found", "Attempt not found", 404)
            for event in await repo.usage_facts(r.id):
                payload = event.envelope["payload"]
                if event.kind == "request.closed" and (
                    payload["delivery_outcome"] == "upstream_failed"
                    or payload.get("delivered_attempt_id") not in {None, str(attempt.id)}
                ):
                    conflict("Evidence contradicts the recorded delivery")
                if (
                    event.kind == "attempt.finished"
                    and event.attempt_id == attempt.id
                    and payload["outcome"] == "failed"
                ):
                    conflict("Failed attempts cannot be billed to the customer")
            known = await repo.normalized_quantities(attempt.id)
            if known is not None and known != body.quantities:
                conflict("Use the retained normalized evidence for this attempt")
            price = found(await repo.get("price", r.sale_price_version_id))
            discount = found(await repo.get("discount", r.discount_version_id))
            try:
                gross, lines = usage_amount(price.rates, body.quantities, discount.factor)
            except ValueError:
                raise ServiceError("invalid_usage", "Invalid supplemental usage", 422) from None
            waiver = body.waiver if body.action == "waive" else Decimal(0)
            if waiver > gross:
                conflict("Waiver exceeds the computed charge")
            amount = gross - waiver
            result.update(gross_amount=format(gross, ".12f"), waived_amount=format(waiver, ".12f"))
        if account.balance - account.held_amount + r.reserved_amount < amount:
            conflict("Insufficient available funds for the verified charge")
        for period, hold in await repo.request_budgets(r.id):
            # Existing authorized capacity survives a later budget reduction.
            if (
                amount > hold.reserved
                and period.spent + period.reserved - hold.reserved + amount > period.limit_amount
            ):
                conflict("Additional charge exceeds the original period budget")
        await self.reservations.post_settlement(
            repo,
            account,
            r,
            amount,
            lines,
            digest(str(decision)),
            captured=body.action != "confirm_no_charge",
            canceled=False,
        )
        await repo.close_case(case, decision)
        await repo.clear_restriction(account, "request", r.id)
        await repo.wake_existing_usage(r.id)
        result.update(request_id=str(r.id), charged_amount=format(amount, ".12f"))

    async def _cost(self, repo, case, body, decision, result):
        if body.action == "retry":
            await repo.requeue_usage(case["request_id"])
            result["processing_state"] = "queued"
            return
        attempt = found(await repo.get("attempt", case["source_id"], lock=True))
        price = found(await repo.get("price", attempt.cost_price_version_id))
        if body.action == "confirm_cost":
            amount = await repo.statement_amount(
                body.statement_id, case["organization_id"], attempt.id
            )
            if amount is None or case["evidence"].get("statement_id") != str(body.statement_id):
                conflict("Statement does not match this discrepancy")
        elif body.action == "supplement_usage" and body.attempt_id == attempt.id:
            try:
                amount, _ = usage_amount(price.rates, body.quantities)
            except ValueError:
                raise ServiceError("invalid_usage", "Invalid cost dimensions", 422) from None
        else:
            conflict("Supply attempt evidence or a registered upstream statement")
        # This separate append-only record applies to procurement cost only.
        correction = uuid4()
        await repo.append_cost_correction(
            correction, attempt.id, decision, amount, body.quantities, price.id
        )
        await repo.close_case(case, decision)
        await repo.wake_existing_usage(attempt.request_id)
        result.update(correction_id=str(correction), cost_amount=format(amount, ".12f"))

    async def _payment(self, repo, account, case, body, decision, result):
        order = found(
            await repo.get("payment_order", case["source_id"], account.organization_id, lock=True)
        )
        if body.action == "retry":
            await repo.wake_query(order)
            result["processing_state"] = "queued"
            return
        if body.action != "verify_payment":
            conflict("Payments require server-side verification")
        row = found(await repo.get("payment_case", case["id"], account.organization_id, lock=True))
        obs = found(await repo.get("payment_observation", body.observation_id))
        if obs.order_id != order.id:
            raise ServiceError("not_found", "Observation not found", 404)
        f = obs.facts
        if (
            order.verified_observation_id != obs.id
            or obs.checkout_generation != order.checkout_generation
            or obs.disposition != "current"
            or f.get("lookup_result") != "found"
            or f.get("merchant_order_ref") != order.merchant_order_ref
            or f.get("merchant_id") != order.stable_merchant_id
            or f.get("environment") != order.environment
            or f.get("currency") != order.collection_currency
            or f.get("amount") is None
            or Decimal(f["amount"]) != order.collection_amount
            or f.get("state")
            not in {"succeeded", "partially_reversed", "reversed", "cancelled", "failed"}
            or f.get("dispute_state") == "open"
            or f.get("reversal_state") == "pending"
        ):
            conflict("A current conclusive payment observation is required")
        received = await repo.receipt(order.receipt_namespace, f.get("transaction_id") or "")
        if row.reason == "receipt_conflict" and (received is None or received.order_id != order.id):
            conflict("Receipt belongs to another order")
        if row.reason in {
            "additional_payment",
            "additional_payment_reversed_or_disputed",
            "payment_reference_conflict",
        }:
            if await repo.extra_receipt_total(order) > 0:
                conflict("Additional collection remains outstanding")
            target = row.evidence.get("provider_payment_ref")
            if target and target != f.get("provider_payment_ref"):
                conflict("Verify the referenced external payment")
        adjusted = (
            await repo.adjusted_amount(order.ledger_entry_id)
            if order.ledger_entry_id
            else Decimal(0)
        )
        original = (
            await repo.get("funding", order.funding_record_id) if order.funding_record_id else None
        )
        original_receipt = (
            await repo.get("payment_receipt", original.payment_receipt_id) if original else None
        )
        extra = bool(original_receipt and received and received.id != original_receipt.id)
        fully_returned = (
            received is not None
            and received.order_id == order.id
            and f.get("reversal_state") == "succeeded"
            and Decimal(f.get("reversed_amount") or 0) == received.amount
        )
        if extra and (not fully_returned or await repo.extra_receipt_total(order) > 0):
            conflict("Additional receipt has not been fully returned")
        if (
            original_receipt
            and not extra
            and (
                f.get("transaction_id") != original_receipt.transaction_id
                or f.get("provider_payment_ref") != original_receipt.provider_payment_ref
            )
        ):
            conflict("Evidence must match the credited receipt")
        if extra and row.reason in {"payment_reversed", "payment_disputed", "refund_pending"}:
            conflict("Evidence must identify the original credited receipt")
        risk = max(
            Decimal(f.get("reversed_amount") or 0),
            Decimal(f.get("disputed_amount") or 0)
            if f.get("dispute_state") == "lost"
            else Decimal(0),
        )
        if (
            original_receipt
            and not extra
            and wallet_value(order, max(order.reversed_amount, risk)) > adjusted
        ):
            conflict("Post the source funding reversal before closing this case")
        if (
            row.reason == "reported_reversal_or_dispute"
            and not risk
            and f.get("dispute_state") != "won"
            and f.get("reversal_state") != "failed"
        ):
            conflict("Reported reversal or dispute is still unconfirmed")
        if original is None and risk and not fully_returned:
            conflict("Uncredited reversed receipts require external reconciliation")
        if (
            f.get("dispute_state") == "none"
            and order.disputed_amount
            and row.reason == "payment_disputed"
        ):
            conflict("Dispute outcome is still unknown")
        if row.reason == "refund_pending" and f.get("reversal_state") not in {
            "failed",
            "succeeded",
        }:
            conflict("Refund outcome is still unknown")
        basis_observation = row.evidence.get("observation_id")
        origin_event = row.evidence.get("event_id")
        if origin_event:
            origin = await repo.get("payment_event", UUID(origin_event))
            if origin and (
                obs.created_at < origin.created_at
                or any(
                    origin.verified_fields.get(k) not in (None, f.get(k))
                    for k in (
                        "provider_payment_ref",
                        "transaction_id",
                        "reversal_reference",
                        "dispute_reference",
                    )
                )
            ):
                conflict("Observation does not verify the reported event")
        if obs.created_at < row.created_at and basis_observation != str(obs.id):
            conflict("Observation predates the case")
        basis = {
            "observation_id": str(obs.id),
            "provider_payment_ref": f.get("provider_payment_ref"),
            "transaction_id": f.get("transaction_id"),
            "reversed_amount": f.get("reversed_amount") or "0",
            "disputed_amount": f.get("disputed_amount") or "0",
            "reversal_reference": f.get("reversal_reference"),
            "dispute_reference": f.get("dispute_reference"),
            "dispute_state": f.get("dispute_state"),
            "reversal_state": f.get("reversal_state"),
            "state": f.get("state"),
        }
        await repo.close_payment_case(row.id, decision, basis)
        remaining = await repo.open_payment_reasons(order.id)
        order.review_required = bool(remaining)
        order.review_reason = remaining[-1] if remaining else None
        if not remaining:
            order.block_credit = bool(risk)
            await repo.clear_restriction(account, "payment", order.id)
            if order.credit_state == "uncredited" and not risk:
                await repo.wake_query(order)
        order.version += 1
        result.update(
            order_id=str(order.id),
            observation_id=str(obs.id),
            adjusted_amount=format(adjusted, ".12f"),
        )

    async def unfreeze(self, token, org, reason, evidence, expected, key):
        scope = await self.access.authorize(token, org, operation="finance")
        async with self.tx() as repo:
            action = f"{org}:account:unfreeze"
            old, kd, rd = await replay(
                repo,
                scope.actor_id,
                action,
                key,
                {"reason": reason, "evidence": evidence, "version": expected},
            )
            if old:
                return {"account_id": str(old.resource_id), "status": "active"}
            account = await repo.account(org)
            versioned(account, expected)
            if account.status != "frozen":
                conflict("Account is already active")
            await repo.release_cleared_restrictions(account.id, org)
            if await repo.unresolved(account) or await repo.reconcile(account):
                conflict("Account still has unresolved restrictions or differences")
            await repo.edit(account, {"status": "active"})
            await repo.audit(
                scope.actor_id,
                "account.unfrozen",
                account.id,
                org,
                {"reason": reason, "evidence": evidence},
            )
            await repo.remember(scope.actor_id, action, kd, rd, "account", account.id)
            return {"account_id": str(account.id), "status": "active"}

    async def check_next(self):
        async with self.tx() as repo:
            return await repo.check_account()
