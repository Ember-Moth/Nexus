"""Payment persistence and task fencing inside billing's scoped transaction."""

import math
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from .common import digest, found
from .payment_schemas import PaymentClaim
from .repository import BillingRepository


class PaymentRepository(BillingRepository):
    async def current_configuration(self, channel_id):
        channel = found(await self.get("payment_channel", channel_id))
        config = found(await self.get("payment_version", channel.active_config_id))
        return channel, config

    async def verification_versions(self, channel_id):
        channel = found(await self.get("payment_channel", channel_id))
        now = await self.now()
        versions = (
            (
                await self._session.execute(
                    select(m.PaymentChannelVersion)
                    .where(
                        m.PaymentChannelVersion.channel_id == channel_id,
                        m.PaymentChannelVersion.revoked_at.is_(None),
                        or_(
                            m.PaymentChannelVersion.id == channel.active_config_id,
                            m.PaymentChannelVersion.verification_valid_until > now,
                        ),
                    )
                    .order_by(m.PaymentChannelVersion.version.desc())
                )
            )
            .scalars()
            .all()
        )
        return channel, versions

    async def configuration_usable(self, channel, version_id, *, verification=False):
        channel = found(await self.get("payment_channel", channel.id))
        config = await self.get("payment_version", version_id)
        if config is None or config.channel_id != channel.id or config.revoked_at is not None:
            return False
        return (
            not verification
            or config.id == channel.active_config_id
            or (
                config.verification_valid_until is not None
                and config.verification_valid_until > await self.now()
            )
        )

    async def payment_lock(self, order_id):
        row = found(await self.get("payment_order", order_id))
        account = await self.account(row.organization_id)
        row = found(await self.get("payment_order", order_id, account.organization_id, lock=True))
        return account, row

    async def order_by_reference(self, namespace, reference):
        return (
            await self._session.execute(
                select(m.PaymentOrder).where(
                    m.PaymentOrder.receipt_namespace == namespace,
                    m.PaymentOrder.merchant_order_ref == reference,
                )
            )
        ).scalar_one_or_none()

    async def operation(self, order_id, kind, operation_key="main", *, lock=False):
        query = (
            select(m.PaymentOperation)
            .where(
                m.PaymentOperation.order_id == order_id,
                m.PaymentOperation.kind == kind,
                m.PaymentOperation.operation_key == operation_key,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def add_operations(self, order):
        now = await self.now()
        for kind, delay in [("create", 0), ("query", 30)]:
            await self.create(
                "payment_operation",
                {
                    "order_id": order.id,
                    "kind": kind,
                    "operation_key": "main",
                    "idempotency_key": "nexus-payment:" + str(order.id),
                    "next_attempt_at": now + timedelta(seconds=delay),
                },
            )

    async def wake_query(self, order, *, actor_id=None, target_ref=None):
        # Different signed provider-object hints get separate durable queries; none are overwritten.
        operation_key = (
            digest(target_ref)
            if target_ref and target_ref != order.provider_payment_ref
            else "main"
        )
        row = await self.operation(order.id, "query", operation_key, lock=True)
        if row is None:
            return await self.create(
                "payment_operation",
                {
                    "order_id": order.id,
                    "kind": "query",
                    "operation_key": operation_key,
                    "target_provider_ref": target_ref,
                    "idempotency_key": "nexus-query:" + str(uuid4()),
                    "next_attempt_at": await self.now(),
                    "requested_by": actor_id,
                },
            )
        row.wake_version += 1
        row.requested_by = actor_id
        row.attempts = 0
        row.next_attempt_at = await self.now()
        if row.state != "running":
            row.state = "pending"
        return row

    async def take_rate_slot(self, namespace, limit):
        now = await self.now()
        await self._session.execute(
            insert(m.PaymentRateBucket)
            .values(receipt_namespace=namespace, window_start=now, request_count=0)
            .on_conflict_do_nothing(index_elements=[m.PaymentRateBucket.receipt_namespace])
        )
        row = (
            await self._session.execute(
                select(m.PaymentRateBucket)
                .where(m.PaymentRateBucket.receipt_namespace == namespace)
                .with_for_update()
            )
        ).scalar_one()
        end = row.window_start + timedelta(seconds=60)
        if now >= end:
            row.window_start = now
            row.request_count = 0
            end = now + timedelta(seconds=60)
        if row.request_count >= limit:
            return max(1, math.ceil((end - now).total_seconds()))
        row.request_count += 1
        return 0

    async def claim(self, lease_seconds=30):
        now = await self.now()
        row = (
            await self._session.execute(
                select(m.PaymentOperation)
                .where(
                    or_(
                        and_(
                            m.PaymentOperation.state == "pending",
                            m.PaymentOperation.next_attempt_at <= now,
                        ),
                        and_(
                            m.PaymentOperation.state == "running",
                            m.PaymentOperation.lease_until <= now,
                        ),
                    )
                )
                .order_by(m.PaymentOperation.next_attempt_at, m.PaymentOperation.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.state = "running"
        row.fencing_token += 1
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.attempts += 1
        row.claimed_wake_version = row.wake_version
        await self.flush()
        return PaymentClaim(row.id, row.order_id, row.fencing_token)

    async def claimed_operation(self, claim):
        row = found(await self.get("payment_operation", claim.id, lock=True))
        if (
            row.state != "running"
            or row.fencing_token != claim.fencing_token
            or row.lease_until is None
            or row.lease_until <= await self.now()
        ):
            return None
        return row

    async def finish(
        self,
        operation,
        *,
        delay=None,
        error=None,
        reset_attempts=False,
        review=False,
        handled=False,
    ):
        operation.last_error = error
        operation.lease_until = None
        if reset_attempts:
            operation.attempts = 0
        if operation.wake_version > operation.claimed_wake_version:
            operation.state = "pending"
            operation.next_attempt_at = await self.now()
        elif review:
            operation.state = "review"
        elif delay is None:
            operation.state = "done"
        else:
            operation.state = "pending"
            operation.next_attempt_at = await self.now() + timedelta(seconds=delay)
        if handled or review:
            await self._session.execute(
                update(m.PaymentEvent)
                .where(
                    m.PaymentEvent.operation_id == operation.id,
                    m.PaymentEvent.wake_version <= operation.claimed_wake_version,
                    m.PaymentEvent.processing_state.in_(["received", "conflict", "review"]),
                )
                .values(processing_state="processed" if handled else "review")
            )
        await self.flush()

    async def count_duplicate(self):
        await self._session.execute(
            text("""INSERT INTO billing.ingest_counters(name,total)
           VALUES('payment', 1) ON CONFLICT(name) DO UPDATE SET
           total=billing.ingest_counters.total+1""")
        )

    async def notice(self, namespace, event_key, fingerprint):
        rows = (
            (
                await self._session.execute(
                    select(m.PaymentEvent).where(
                        m.PaymentEvent.receipt_namespace == namespace,
                        m.PaymentEvent.event_key == event_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        same = next((row for row in rows if row.event_fingerprint == fingerprint), None)
        return same, bool(rows and same is None)

    async def receipt(self, namespace, transaction_id):
        await self.lock("payment-receipt:" + digest([namespace, transaction_id]))
        return (
            await self._session.execute(
                select(m.PaymentReceipt).where(
                    m.PaymentReceipt.receipt_namespace == namespace,
                    m.PaymentReceipt.transaction_id == transaction_id,
                )
            )
        ).scalar_one_or_none()

    async def verified_risk_decision(self, order, fact):
        rows = (
            (
                await self._session.execute(
                    select(m.PaymentCase).where(
                        m.PaymentCase.order_id == order.id,
                        m.PaymentCase.status == "resolved",
                        m.PaymentCase.decision_id.is_not(None),
                        m.PaymentCase.decision_basis.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            basis = row.decision_basis
            if (
                basis["transaction_id"] == fact.transaction_id
                and basis["provider_payment_ref"] == fact.provider_payment_ref
                and Decimal(basis["reversed_amount"]) == fact.reversed_amount
                and Decimal(basis["disputed_amount"]) == fact.disputed_amount
                and basis["reversal_state"] == fact.reversal_state
                and basis["dispute_state"] == fact.dispute_state
                and basis.get("reversal_reference") == fact.reversal_reference
                and basis.get("dispute_reference") == fact.dispute_reference
                and basis["state"] == fact.state
            ):
                return True
        return False

    async def open_case(self, order, reason, evidence, amount=Decimal(0)):
        row = (
            await self._session.execute(
                select(m.PaymentCase).where(
                    m.PaymentCase.order_id == order.id, m.PaymentCase.reason == reason
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = await self.create(
                "payment_case",
                {
                    "organization_id": order.organization_id,
                    "order_id": order.id,
                    "reason": reason,
                    "amount_at_risk": amount,
                    "evidence": evidence,
                },
            )
        else:
            row.version += 1
            row.decision_id = None
            row.decision_basis = None
            row.status = "open"
            row.amount_at_risk = amount
            row.evidence = evidence
        order.review_required = True
        order.review_reason = reason
        return row

    async def resolve_transient_cases(self, order, *, allow_refund_resolution=True):
        transient = {
            "provider_unconfirmed",
            "provider_unavailable",
            "retry_exhausted",
            "create_unknown",
            "refund_pending",
            "payment_deadline_elapsed",
            "wallet_capacity_unavailable",
        }
        if not allow_refund_resolution:
            transient.discard("refund_pending")
        rows = (
            (
                await self._session.execute(
                    select(m.PaymentCase).where(
                        m.PaymentCase.order_id == order.id, m.PaymentCase.status == "open"
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.reason in transient:
                row.status = "resolved"
                row.version += 1
        remaining = [row for row in rows if row.status == "open"]
        order.review_required = bool(remaining)
        order.review_reason = remaining[-1].reason if remaining else None

    async def extra_receipt_total(self, order):
        credited_receipt_id = None
        if order.funding_record_id:
            credited_receipt_id = (
                await self.get("funding", order.funding_record_id)
            ).payment_receipt_id
        receipts = (
            (
                await self._session.execute(
                    select(m.PaymentReceipt).where(m.PaymentReceipt.order_id == order.id)
                )
            )
            .scalars()
            .all()
        )
        observations = (
            (
                await self._session.execute(
                    select(m.PaymentObservationRow.facts).where(
                        m.PaymentObservationRow.order_id == order.id,
                        m.PaymentObservationRow.disposition.in_(["current", "stale"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        total = Decimal(0)
        for receipt in receipts:
            if receipt.id == credited_receipt_id:
                continue
            reversed_amount = max(
                (
                    Decimal(fact["reversed_amount"])
                    for fact in observations
                    if fact.get("transaction_id") == receipt.transaction_id
                    and fact.get("reversal_state") == "succeeded"
                ),
                default=Decimal(0),
            )
            total += receipt.amount - min(receipt.amount, reversed_amount)
        return total

    async def reconcile(self, account):
        issues = await super().reconcile(account)
        orders = (
            (
                await self._session.execute(
                    select(m.PaymentOrder).where(m.PaymentOrder.account_id == account.id)
                )
            )
            .scalars()
            .all()
        )
        for order in orders:
            if order.credit_state != "credited":
                if order.funding_record_id or order.ledger_entry_id:
                    issues.append(
                        {"kind": "uncredited_payment_has_funding", "order_id": str(order.id)}
                    )
                continue
            funding = await self.get("funding", order.funding_record_id, order.organization_id)
            entry = await self.get("ledger", order.ledger_entry_id, order.organization_id)
            if (
                funding is None
                or entry is None
                or funding.payment_order_id != order.id
                or funding.amount != order.credit_amount
                or funding.status != "posted"
                or entry.source_id != funding.id
                or entry.signed_amount != order.credit_amount
            ):
                issues.append({"kind": "payment_funding", "order_id": str(order.id)})
                continue
            receipt = await self.get("payment_receipt", funding.payment_receipt_id)
            if (
                receipt is None
                or receipt.order_id != order.id
                or receipt.transaction_id != funding.external_reference
                or receipt.receipt_namespace != funding.receipt_namespace
                or receipt.amount != funding.amount
            ):
                issues.append({"kind": "payment_receipt", "order_id": str(order.id)})
        return issues
