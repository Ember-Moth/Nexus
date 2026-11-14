"""Durable payment execution: no database transaction spans a provider call."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from nexus.shared.errors import ServiceError

from .common import digest, found
from .funding import post_funding_record
from .gateways import GatewayFailure, validate_checkout
from .money import MAX_AMOUNT
from .payment_schemas import PaymentExecution
from .payment_support import expectation, open_gateway


class PaymentProcessor:
    def __init__(self, transactions, cipher, registry, *, requests_per_minute=120):
        self._transactions, self._cipher, self._registry = transactions, cipher, registry
        self._requests_per_minute = requests_per_minute

    async def process(self, limit=8):
        count = 0
        for _ in range(limit):
            async with self._transactions() as repo:
                claim = await repo.claim()
            if claim is None:
                break
            count += 1
            try:
                prepared = await self._prepare(claim)
                if prepared is None:
                    continue
                execution, gateway = prepared
                if execution.retry_observation_id is not None:
                    prepared = await self._prepare_retry(execution, execution.retry_observation_id)
                    if prepared is None:
                        continue
                    execution, gateway = prepared
                if execution.operation_kind == "create" and not execution.recovery:
                    result = await gateway.create_payment(
                        execution.expectation, execution.idempotency_key
                    )
                    await self._checkout(execution, gateway, result)
                else:
                    fact = await gateway.query_payment(
                        execution.expectation, execution.provider_payment_ref
                    )
                    retry = await self._observation(execution, gateway, fact)
                    if retry is not None:
                        prepared = await self._prepare_retry(execution, retry)
                        if prepared:
                            execution, gateway = prepared
                            result = await gateway.create_payment(
                                execution.expectation, execution.idempotency_key
                            )
                            await self._checkout(execution, gateway, result)
            except GatewayFailure as error:
                await self._failed(claim, error.code)
            except ServiceError as error:
                if error.code in {"storage_unavailable", "billing_conflict"}:
                    raise
                await self._failed(claim, "retryable_failure")
            except TimeoutError, ConnectionError:
                await self._failed(claim, "unconfirmed")
        return count

    async def _prepare(self, claim):
        async with self._transactions() as repo:
            account, order = await repo.payment_lock(claim.order_id)
            operation = await repo.claimed_operation(claim)
            if operation is None:
                return None
            if operation.attempts > 8:
                await repo.open_case(order, "retry_exhausted", {"operation_id": str(operation.id)})
                await repo.finish(operation, review=True, error="retry_exhausted")
                return None
            if operation.kind == "create" and (
                order.credit_state == "credited" or order.checkout_state == "ready"
            ):
                await repo.finish(operation)
                return None
            channel, config = await repo.current_configuration(order.channel_id)
            gateway = open_gateway(self._registry, self._cipher, channel, config)
            recovery = operation.kind == "create" and operation.remote_started_at is not None
            delay = (
                0
                if operation.retry_observation_id
                else await repo.take_rate_slot(order.receipt_namespace, self._requests_per_minute)
            )
            if delay:
                operation.attempts -= 1
                await repo.finish(operation, delay=delay, error="rate_limited")
                return None
            if operation.kind == "create" and not recovery:
                if channel.status != "active" or order.expires_at <= await repo.now():
                    await repo.edit(order, {"checkout_state": "closed"})
                    await repo.finish(operation)
                    return None
                operation.remote_started_at = await repo.now()
                operation.idempotency_mode = gateway.capabilities.idempotency
                operation.idempotency_expires_at = operation.remote_started_at + timedelta(
                    seconds=gateway.capabilities.idempotency_seconds
                )
                order.checkout_generation += 1
                order.version += 1
                await repo.audit(
                    None,
                    "payment_create.started",
                    order.id,
                    order.organization_id,
                    {
                        "generation": order.checkout_generation,
                        "fencing_token": operation.fencing_token,
                        "system_actor": "payment-worker",
                    },
                )
            await repo.flush()
            return PaymentExecution(
                claim,
                expectation(order),
                channel.adapter_type,
                config.id,
                config.key_version,
                config.ciphertext,
                operation.kind,
                operation.idempotency_key,
                operation.target_provider_ref or order.provider_payment_ref,
                order.version,
                order.checkout_generation,
                operation.remote_started_at,
                recovery,
                operation.retry_observation_id,
            ), gateway

    async def _prepare_retry(self, execution, observation_id):
        async with self._transactions() as repo:
            account, order = await repo.payment_lock(execution.claim.order_id)
            operation = await repo.claimed_operation(execution.claim)
            if operation is None:
                return None
            observation = found(await repo.get("payment_observation", observation_id))
            if (
                order.version != execution.order_version
                or order.checkout_generation != execution.checkout_generation
                or observation.order_id != order.id
                or observation.order_version != order.version
                or observation.checkout_generation != order.checkout_generation
                or observation.facts.get("lookup_result") != "not_found"
                or observation.disposition != "current"
            ):
                operation.retry_observation_id = None
                await repo.finish(operation, delay=0, reset_attempts=True)
                return None
            channel, config = await repo.current_configuration(order.channel_id)
            gateway = open_gateway(self._registry, self._cipher, channel, config)
            now = await repo.now()
            capability = gateway.capabilities
            safe = (
                capability.idempotency != "none"
                and capability.idempotency == operation.idempotency_mode
                and operation.idempotency_expires_at is not None
                and now < operation.idempotency_expires_at
                and operation.remote_started_at is not None
                and (now - operation.remote_started_at).total_seconds()
                < capability.idempotency_seconds
                and order.expires_at > now
                and channel.status == "active"
                and not order.block_credit
                and order.credit_state == "uncredited"
                and order.provider_payment_ref is None
            )
            if not safe:
                await repo.open_case(
                    order,
                    "create_unknown",
                    {"operation_id": str(operation.id), "observation_id": str(observation_id)},
                )
                await repo.edit(order, {"checkout_state": "unknown"})
                await repo.finish(operation, review=True, error="unconfirmed")
                return None
            delay = await repo.take_rate_slot(order.receipt_namespace, self._requests_per_minute)
            if delay:
                operation.retry_observation_id = observation_id
                if execution.retry_observation_id is not None:
                    operation.attempts -= 1
                await repo.finish(operation, delay=delay, error="rate_limited")
                return None
            operation.retry_observation_id = None
            order.checkout_generation += 1
            order.version += 1
            await repo.audit(
                None,
                "payment_create.retried",
                order.id,
                order.organization_id,
                {
                    "generation": order.checkout_generation,
                    "fencing_token": operation.fencing_token,
                    "observation_id": str(observation_id),
                },
            )
            await repo.flush()
            return replace(
                execution,
                expectation=expectation(order),
                credential_version_id=config.id,
                credential_key_version=config.key_version,
                credential_ciphertext=config.ciphertext,
                order_version=order.version,
                checkout_generation=order.checkout_generation,
                recovery=False,
                retry_observation_id=None,
            ), gateway

    async def _checkout(self, execution, gateway, result):
        async with self._transactions() as repo:
            account, order = await repo.payment_lock(execution.claim.order_id)
            operation = await repo.claimed_operation(execution.claim)
            if operation is None:
                order.version += 1
                await repo.wake_query(order, target_ref=result.provider_payment_ref)
                await repo.audit(
                    None,
                    "payment_create.late_result",
                    order.id,
                    order.organization_id,
                    {
                        "provider_payment_ref": result.provider_payment_ref,
                        "generation": execution.checkout_generation,
                    },
                )
                return
            channel = found(await repo.get("payment_channel", order.channel_id))
            if not await repo.configuration_usable(channel, execution.credential_version_id):
                await repo.finish(operation, delay=1, error="credentials_changed")
                return
            if (
                order.provider_payment_ref
                and order.provider_payment_ref != result.provider_payment_ref
            ):
                await repo.open_case(
                    order,
                    "payment_reference_conflict",
                    {"provider_payment_ref": result.provider_payment_ref},
                )
                order.block_credit = True
                if order.credit_state == "credited":
                    await repo.edit(account, {"status": "frozen"})
                await repo.wake_query(order, target_ref=result.provider_payment_ref)
            elif (
                order.version == execution.order_version
                and order.checkout_generation == execution.checkout_generation
            ):
                order.provider_payment_ref = result.provider_payment_ref
                checkout = validate_checkout(
                    result.checkout, gateway.capabilities, await repo.now()
                )
                if (
                    checkout
                    and result.state in {"pending", "processing"}
                    and order.credit_state == "uncredited"
                    and not order.review_required
                    and order.expires_at > await repo.now()
                ):
                    order.checkout_result = checkout.model_dump(mode="json")
                    order.checkout_state = "ready"
                elif order.credit_state == "uncredited":
                    order.checkout_state = "unknown"
                order.version += 1
                await repo.wake_query(order)
            else:
                # The query recovers the object without regressing a newer notification or credit.
                await repo.wake_query(order, target_ref=result.provider_payment_ref)
            await repo.finish(operation)
            await repo.audit(
                None,
                "payment_create.completed",
                order.id,
                order.organization_id,
                {
                    "provider_payment_ref": result.provider_payment_ref,
                    "generation": execution.checkout_generation,
                },
            )

    @staticmethod
    def _query_identity(expect, fact):
        return (
            fact.merchant_order_ref == expect.merchant_order_ref
            and fact.merchant_id == expect.stable_merchant_id
            and fact.environment == expect.environment
        )

    @staticmethod
    def _binding(expect, fact, gateway, provider_ref=None):
        try:
            gateway.check_binding(expect, fact)
            if (
                fact.merchant_order_ref != expect.merchant_order_ref
                or fact.merchant_id != expect.stable_merchant_id
                or fact.environment != expect.environment
                or fact.amount != expect.amount
                or fact.currency != expect.currency
                or not fact.provider_payment_ref
                or (provider_ref is not None and fact.provider_payment_ref != provider_ref)
                or not fact.relevant
            ):
                return False
            if any(
                getattr(fact, name) is not None and getattr(fact, name) != getattr(expect, name)
                for name in ("order_id", "organization_id", "account_id")
            ):
                return False
            if fact.reversed_amount > fact.amount or fact.disputed_amount > fact.amount:
                return False
            if (fact.state == "reversed" and fact.reversed_amount != fact.amount) or (
                fact.state == "partially_reversed" and not 0 < fact.reversed_amount < fact.amount
            ):
                return False
            return True
        except GatewayFailure:
            return False

    async def _record_receipt(self, repo, order, fact, observation, *, review_conflict=True):
        if not fact.transaction_id or not fact.provider_payment_ref:
            return None
        existing = await repo.receipt(order.receipt_namespace, fact.transaction_id)
        if existing:
            if (
                existing.order_id != order.id
                or existing.amount != fact.amount
                or existing.currency != fact.currency
                or existing.provider_payment_ref != fact.provider_payment_ref
            ):
                if review_conflict:
                    await repo.open_case(
                        order,
                        "receipt_conflict",
                        {"observation_id": str(observation.id), "receipt_id": str(existing.id)},
                        fact.amount,
                    )
                    order.block_credit = True
                return None
            return existing
        return await repo.create(
            "payment_receipt",
            {
                "organization_id": order.organization_id,
                "account_id": order.account_id,
                "order_id": order.id,
                "receipt_namespace": order.receipt_namespace,
                "transaction_id": fact.transaction_id,
                "provider_payment_ref": fact.provider_payment_ref,
                "amount": fact.amount,
                "currency": fact.currency,
                "observation_id": observation.id,
                "confirmed_at": fact.received_at or fact.observed_at,
            },
        )

    async def _risk(self, repo, account, order, fact, observation):
        reversal = (
            fact.reversal_state == "succeeded" and fact.reversed_amount > 0
        ) or fact.state in {"partially_reversed", "reversed"}
        dispute = fact.dispute_state in {"open", "lost"} or fact.disputed_amount > 0
        if not reversal and not dispute:
            return False
        order.block_credit = True
        order.reversed_amount = max(order.reversed_amount, fact.reversed_amount)
        order.disputed_amount = max(order.disputed_amount, fact.disputed_amount)
        if reversal:
            order.payment_state = (
                "reversed" if order.reversed_amount >= order.amount else "partially_reversed"
            )
        adjusted = (
            await repo.adjusted_amount(order.ledger_entry_id)
            if order.ledger_entry_id
            else Decimal(0)
        )
        evidence = {
            "observation_id": str(observation.id),
            "reversal_reference": fact.reversal_reference,
            "dispute_reference": fact.dispute_reference,
        }
        if reversal:
            await repo.open_case(
                order,
                "payment_reversed",
                evidence,
                max(order.reversed_amount - adjusted, Decimal(0)),
            )
        if dispute:
            await repo.open_case(
                order,
                "payment_disputed",
                evidence,
                max(order.disputed_amount - adjusted, Decimal(0)),
            )
        if order.credit_state == "credited":
            await repo.edit(account, {"status": "frozen"})
        return True

    async def _observation(self, execution, gateway, fact):
        async with self._transactions() as repo:
            account, order = await repo.payment_lock(execution.claim.order_id)
            operation = await repo.claimed_operation(execution.claim)
            channel = found(await repo.get("payment_channel", order.channel_id))
            usable = await repo.configuration_usable(channel, execution.credential_version_id)
            fresh = (
                operation is not None
                and usable
                and order.version == execution.order_version
                and order.checkout_generation == execution.checkout_generation
            )
            facts = fact.model_dump(mode="json")
            binding_valid = (
                self._binding(execution.expectation, fact, gateway, execution.provider_payment_ref)
                if fact.lookup_result == "found"
                else self._query_identity(execution.expectation, fact)
            )
            observation = await repo.create(
                "payment_observation",
                {
                    "order_id": order.id,
                    "config_version_id": execution.credential_version_id,
                    "source": "create_recovery"
                    if execution.operation_kind == "create"
                    else "query",
                    "order_version": execution.order_version,
                    "checkout_generation": execution.checkout_generation,
                    "observed_at": fact.observed_at,
                    "evidence_digest": digest(facts),
                    "facts": facts,
                    "disposition": "credentials_revoked"
                    if not usable
                    else "invalid_binding"
                    if not binding_valid
                    else "current"
                    if fresh
                    else "stale",
                },
            )
            if not usable:
                if operation:
                    await repo.finish(operation, delay=1, error="credentials_changed")
                return None
            if not fresh:
                if (
                    binding_valid
                    and fact.lookup_result == "found"
                    and fact.state in {"succeeded", "partially_reversed", "reversed"}
                ):
                    await self._record_receipt(
                        repo, order, fact, observation, review_conflict=False
                    )
                if operation:
                    await repo.finish(operation, delay=0, reset_attempts=True)
                elif fact.lookup_result == "found":
                    await repo.wake_query(
                        order, target_ref=fact.provider_payment_ref if binding_valid else None
                    )
                return None
            if not binding_valid:
                await repo.open_case(
                    order, "payment_binding_conflict", {"observation_id": str(observation.id)}
                )
                order.block_credit = True
                if order.credit_state == "credited" and (
                    fact.state in {"partially_reversed", "reversed"}
                    or fact.reversal_state in {"pending", "succeeded"}
                    or fact.dispute_state in {"open", "lost"}
                ):
                    await repo.edit(account, {"status": "frozen"})
                order.version += 1
                if operation:
                    await repo.finish(operation, review=True, error="manual_review")
                return None
            if fact.lookup_result == "not_found":
                if execution.operation_kind == "create":
                    return observation.id
                else:
                    await self._defer(repo, order, operation, "provider_unconfirmed")
                return None
            if fact.lookup_result == "unconfirmed":
                if operation:
                    await self._defer(repo, order, operation, "provider_unconfirmed")
                return None
            receipt = None
            if fact.state in {"succeeded", "partially_reversed", "reversed"}:
                receipt = await self._record_receipt(repo, order, fact, observation)
            order.last_observed_at = fact.observed_at
            if fact.state == "unknown":
                await self._defer(repo, order, operation, "provider_unconfirmed")
                return None
            extra = bool(
                order.provider_payment_ref
                and order.provider_payment_ref != fact.provider_payment_ref
            )
            if order.credit_state == "credited" and receipt:
                original = found(await repo.get("funding", order.funding_record_id))
                extra = extra or original.payment_receipt_id != receipt.id
            if extra:
                evidence = {
                    "observation_id": str(observation.id),
                    "receipt_id": str(receipt.id) if receipt else None,
                    "provider_payment_ref": fact.provider_payment_ref,
                    "reversed_amount": str(fact.reversed_amount),
                    "disputed_amount": str(fact.disputed_amount),
                }
                await repo.open_case(
                    order, "additional_payment", evidence, await repo.extra_receipt_total(order)
                )
                if fact.reversal_state == "succeeded" or fact.dispute_state in {"open", "lost"}:
                    await repo.open_case(order, "additional_payment_reversed_or_disputed", evidence)
                order.block_credit = True
                if order.credit_state == "credited":
                    await repo.edit(account, {"status": "frozen"})
                order.version += 1
                await repo.finish(operation, delay=300, reset_attempts=True, handled=True)
                return None
            if await self._risk(repo, account, order, fact, observation):
                order.version += 1
                await repo.finish(operation, delay=300, reset_attempts=True, handled=True)
                return None
            if fact.reversal_state == "pending":
                await repo.open_case(
                    order, "refund_pending", {"observation_id": str(observation.id)}
                )
                if order.credit_state == "credited":
                    await repo.edit(account, {"status": "frozen"})
                order.version += 1
                await repo.finish(operation, delay=30, reset_attempts=True, handled=True)
                return None
            # A verified existing object resolves creation/query uncertainty even before it is paid.
            await repo.resolve_transient_cases(
                order, allow_refund_resolution=account.status != "frozen"
            )
            order.provider_payment_ref = fact.provider_payment_ref
            if order.payment_state not in {"succeeded", "partially_reversed", "reversed"}:
                order.payment_state = fact.state
            if fact.state == "succeeded":
                if receipt is None and not order.block_credit:
                    await repo.open_case(
                        order, "missing_receipt_identity", {"observation_id": str(observation.id)}
                    )
                elif order.credit_state != "credited" and not order.block_credit:
                    if account.balance + order.credit_amount > MAX_AMOUNT:
                        await repo.open_case(
                            order,
                            "wallet_capacity_unavailable",
                            {"observation_id": str(observation.id)},
                            order.amount,
                        )
                        order.version += 1
                        await repo.finish(operation, delay=300, reset_attempts=True, handled=True)
                        return None
                    await self._credit(repo, account, order, receipt, observation)
            elif fact.state in {"pending", "processing"} and order.credit_state != "credited":
                if order.expires_at <= await repo.now():
                    await repo.open_case(
                        order, "payment_deadline_elapsed", {"observation_id": str(observation.id)}
                    )
                elif not order.review_required:
                    checkout = validate_checkout(
                        fact.checkout, gateway.capabilities, await repo.now()
                    )
                    if checkout:
                        order.checkout_result = checkout.model_dump(mode="json")
                        order.checkout_state = "ready"
            elif (
                fact.state in {"failed", "cancelled"}
                and order.credit_state != "credited"
                and order.payment_state != "succeeded"
            ):
                order.checkout_state = "closed"
            order.version += 1
            if execution.operation_kind == "create":
                await repo.finish(operation)
                await repo.wake_query(order)
            else:
                await repo.finish(
                    operation,
                    delay=300
                    if order.credit_state == "credited" or order.expires_at <= await repo.now()
                    else 30,
                    reset_attempts=True,
                    handled=True,
                )
            return None

    async def _credit(self, repo, account, order, receipt, observation):
        funding = await repo.create(
            "funding",
            {
                "organization_id": order.organization_id,
                "account_id": account.id,
                "receiving_account_id": None,
                "receipt_namespace": receipt.receipt_namespace,
                "external_reference": receipt.transaction_id,
                "amount": order.credit_amount,
                "currency": order.currency,
                "received_at": receipt.confirmed_at,
                "evidence": "verified-query:" + str(observation.id),
                "source_type": "gateway",
                "system_actor": "payment-worker",
                "payment_order_id": order.id,
                "payment_receipt_id": receipt.id,
                "created_by": None,
            },
        )
        entry = await post_funding_record(repo, account, funding, system_actor="payment-worker")
        credited_at = await repo.now()
        order.credit_state = "credited"
        order.funding_record_id = funding.id
        order.ledger_entry_id = entry.id
        order.credited_at = credited_at
        order.checkout_state = "closed"
        order.payment_state = "succeeded"
        await repo.audit(
            None,
            "payment_order.credited",
            order.id,
            order.organization_id,
            {
                "system_actor": "payment-worker",
                "funding_record_id": str(funding.id),
                "receipt_id": str(receipt.id),
                "observation_id": str(observation.id),
                "config_version_id": str(observation.config_version_id),
            },
        )

    async def _defer(self, repo, order, operation, reason):
        await repo.open_case(order, reason, {"operation_id": str(operation.id)})
        if operation.attempts >= 8:
            await repo.open_case(order, "retry_exhausted", {"operation_id": str(operation.id)})
            await repo.finish(operation, review=True, error=reason)
        else:
            await repo.finish(
                operation, delay=min(300, 2 ** min(operation.attempts, 8)), error=reason
            )

    async def _failed(self, claim, code):
        async with self._transactions() as repo:
            account, order = await repo.payment_lock(claim.order_id)
            operation = await repo.claimed_operation(claim)
            if operation is None:
                return
            if (
                operation.kind == "create"
                and order.credit_state != "credited"
                and order.checkout_state != "ready"
            ):
                await repo.edit(order, {"checkout_state": "unknown"})
                await repo.wake_query(order)
            if code == "manual_review":
                await repo.open_case(
                    order, "provider_result_invalid", {"operation_id": str(operation.id)}
                )
                await repo.finish(operation, review=True, error=code)
            else:
                await self._defer(repo, order, operation, "provider_unavailable")
