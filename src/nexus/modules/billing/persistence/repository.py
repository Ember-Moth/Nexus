"""Billing-owned persistence. Every financial mutation stays in one scoped transaction."""

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import column, func, select, text, tuple_
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from nexus.shared.errors import ServiceError

from ..application.schemas import AccountReady
from ..domain.events import (
    ACCOUNT_READY,
    ACCOUNT_READY_VERSION,
    FUNDING_POSTED,
    FUNDING_POSTED_VERSION,
)
from . import models as m

# Sentinel: an administrator's write leaves the policy lock as it is (it is refused while set).
KEEP = object()
TABLES = {
    "account": m.Account,
    "receiving": m.ReceivingAccount,
    "funding": m.Funding,
    "adjustment": m.Adjustment,
    "ledger": m.LedgerEntry,
    "price": m.PriceVersion,
    "discount": m.DiscountVersion,
    "budget": m.BudgetPolicy,
    "period": m.BudgetPeriod,
    "request": m.RequestRun,
    "attempt": m.Attempt,
    "payment_channel": m.PaymentChannel,
    "payment_version": m.PaymentChannelVersion,
    "payment_order": m.PaymentOrder,
    "payment_operation": m.PaymentOperation,
    "payment_event": m.PaymentEvent,
    "payment_observation": m.PaymentObservationRow,
    "payment_receipt": m.PaymentReceipt,
    "payment_case": m.PaymentCase,
}


ADMISSION_ERRORS = {
    "account_frozen": (409, "Account frozen"),
    "account_not_ready": (409, "Account not ready"),
    "attempt_conflict": (409, "Attempt conflict"),
    "budget_exceeded": (409, "Budget limit exceeded"),
    "cost_unavailable": (409, "Cost unavailable"),
    "currency_config_mismatch": (503, "Currency config mismatch"),
    "deployment_unavailable": (409, "Deployment unavailable"),
    "insufficient_funds": (409, "Available balance is insufficient"),
    "invalid_attempt_sequence": (409, "Invalid attempt sequence"),
    "invalid_bounds": (422, "Invalid bounds"),
    "invalid_reserve_amount": (422, "Invalid reserve amount"),
    "not_found": (404, "Not found"),
    "price_unavailable": (409, "Price unavailable"),
    "request_conflict": (409, "Request ID was used for different authorization facts"),
    "request_not_executable": (409, "Request not executable"),
}


def period_bounds(at, period):
    start = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return start, start + timedelta(days=1)
    start = start.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


class BillingRepository:
    def __init__(self, session):
        self._session = session

    async def review_container(self):
        return (
            await self._session.execute(text("SELECT billing.v1_review_container()"))
        ).scalar_one()

    async def renew_storage(self, days, policy):
        return (
            await self._session.execute(
                text("SELECT billing.v1_renew_storage(:days,:policy)"),
                {"days": days, "policy": policy},
            )
        ).scalar_one()

    async def stage_storage(self):
        return (await self._session.execute(text("SELECT billing.v1_stage_storage()"))).scalar_one()

    async def claim_usage(self):
        # Account, request, then task. An inbox writer takes request then task.
        candidate_sql = text("""
            SELECT a.*, r.id AS claim_request_id FROM billing.usage_jobs j
            JOIN billing.request_runs r ON r.id=j.request_id
            JOIN billing.accounts a ON a.id=r.account_id
            WHERE j.state='ready' AND j.next_attempt_at <= statement_timestamp()
            ORDER BY j.next_attempt_at,j.request_id
            LIMIT 1 FOR UPDATE OF a SKIP LOCKED
        """)
        candidates = await self._session.execute(
            select(m.Account, column("claim_request_id")).from_statement(candidate_sql)
        )
        candidate = candidates.one_or_none()
        if candidate is None:
            return None
        account, request_id = candidate
        request = await self.get("request", request_id, lock=True)
        # All job creation/requeue paths serialize with the request. Still lock and
        # recheck the job after taking the account and request locks.
        task = (
            await self._session.execute(
                text(
                    "SELECT generation FROM billing.usage_jobs WHERE request_id=:id "
                    "AND state='ready' FOR UPDATE"
                ),
                {"id": request_id},
            )
        ).one_or_none()
        return (account, request, task.generation) if task else None

    async def retry_usage(self, rid, generation):
        await self._session.execute(
            text("""
            UPDATE billing.usage_jobs SET failures=failures+1,
            next_attempt_at=clock_timestamp()+make_interval(secs=>least(30,failures+1)),
            reason='processing_retry' WHERE request_id=:rid AND generation=:generation
            AND state='ready'
        """),
            {"rid": rid, "generation": generation},
        )

    async def usage_facts(self, rid):
        return (
            await self._session.execute(
                text(
                    "SELECT * FROM billing.usage_events WHERE request_id=:id ORDER "
                    "BY received_at,id"
                ),
                {"id": rid},
            )
        ).all()

    async def save_normalization(self, eid, aid, rule, counts, reason):
        await self._session.execute(
            text("""
            INSERT INTO billing.normalized_usage(event_id, attempt_id, rule_version, quantities,
            review_reason)
            VALUES(:eid,:aid,:rule,cast(:counts AS jsonb),:reason) ON CONFLICT(event_id) DO NOTHING
        """),
            {
                "eid": eid,
                "aid": aid,
                "rule": rule,
                "counts": json.dumps(counts) if counts is not None else None,
                "reason": reason,
            },
        )

    async def restrict(self, account, kind, source_id):
        await self._session.execute(
            text("""INSERT INTO billing.account_restrictions(account_id,source_type,source_id)
            VALUES(:acct, :kind, :id) ON CONFLICT(account_id, source_type, source_id) DO UPDATE
            SET released_at=NULL"""),
            {"acct": account.id, "kind": kind, "id": source_id},
        )
        if account.status != "frozen":
            await self.edit(account, {"status": "frozen"})

    async def requeue_usage(self, rid):
        await self._session.execute(
            text("""INSERT INTO billing.usage_jobs(request_id,state) VALUES(:rid,'ready')
          ON CONFLICT(request_id) DO UPDATE SET state='ready',
          generation=billing.usage_jobs.generation+1,
          failures=0,reason=NULL,next_attempt_at=clock_timestamp()"""),
            {"rid": rid},
        )

    async def cost_review(self, request, attempt_id, reason):
        await self._session.execute(
            text("""INSERT INTO billing.review_cases(organization_id,source_type,
            source_id,request_id,reason) VALUES(:org,'attempt',:id,:rid,:reason)
            ON CONFLICT(source_type,source_id) DO UPDATE SET reason=excluded.reason,state='open',
            resolved_at=NULL,resolution_id=NULL,version=billing.review_cases.version+1
            WHERE billing.review_cases.state<>'open' OR
             (billing.review_cases.reason<>excluded.reason AND
             billing.review_cases.reason<>'statement_difference')"""),
            {"org": request.organization_id, "id": attempt_id, "rid": request.id, "reason": reason},
        )

    async def corrected_attempts(self, rid):
        return set(
            (
                await self._session.execute(
                    text("""SELECT DISTINCT c.attempt_id FROM billing.cost_corrections c
            JOIN billing.request_attempts a ON a.id=c.attempt_id WHERE a.request_id=:rid"""),
                    {"rid": rid},
                )
            ).scalars()
        )

    async def priced_attempts(self, request_id):
        return (
            await self._session.execute(
                select(m.Attempt, m.PriceVersion)
                .join(m.PriceVersion, m.Attempt.cost_price_version_id == m.PriceVersion.id)
                .where(m.Attempt.request_id == request_id)
                .order_by(m.Attempt.id)
                .with_for_update(of=m.Attempt)
            )
        ).all()

    async def settlement_prices(self, request):
        return (
            await self._session.execute(
                select(m.PriceVersion, m.DiscountVersion)
                .select_from(m.PriceVersion)
                .join(m.DiscountVersion, m.DiscountVersion.id == request.discount_version_id)
                .where(m.PriceVersion.id == request.sale_price_version_id)
            )
        ).one()

    async def finish_usage(self, rid, state, reason):
        await self._session.execute(
            text("UPDATE billing.usage_jobs SET state=:state,reason=:reason WHERE request_id=:rid"),
            {"rid": rid, "state": state, "reason": reason},
        )

    async def admission_function(self, statement, params):
        try:
            return (await self._session.execute(text(statement), params)).scalar_one()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "P0001":
                raise
            code = error.orig.diag.message_primary
            spec = ADMISSION_ERRORS.get(code)
            if spec is None:
                raise ServiceError("storage_unavailable", "Admission failed", 503) from None
            status, message = spec
            raise ServiceError(code, message, status) from None

    async def reserve_funds(self, admission, fingerprint, dimensions, at):
        params = admission.model_dump()
        await self.admission_function(
            "SELECT billing.v1_reserve(:request_id,:organization_id,:project_id,"
            ":service_account_id,"
            ":api_key_id,:model_id,:input_limit,:output_limit,:ttl_seconds,:fingerprint,"
            "CAST(:dimensions AS jsonb),:at,NULL,NULL,NULL)",
            {
                **params,
                "fingerprint": fingerprint,
                "dimensions": json.dumps(sorted(dimensions)),
                "at": at,
            },
        )
        return await self.get("request", admission.request_id, admission.organization_id)

    async def begin_attempt(self, org_id, request_id, attempt_id, deployment_id, model_id):
        result = await self.admission_function(
            "SELECT billing.v1_attempt(:org,:rid,:aid,:dep,:model,NULL,:at,NULL)",
            {
                "org": org_id,
                "rid": request_id,
                "aid": attempt_id,
                "dep": deployment_id,
                "model": model_id,
                "at": await self.now(),
            },
        )
        return {
            "attempt_id": attempt_id,
            "cost_price_version_id": UUID(result["cost_price_version_id"]),
            "state": result["state"],
        }

    async def now(self):
        return (await self._session.execute(select(func.clock_timestamp()))).scalar_one()

    async def lock(self, key):
        number = int.from_bytes(
            hashlib.sha256(("nexus:billing:" + key).encode()).digest()[:8], signed=True
        )
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})

    async def settings(self):
        """The billing policy row; every billing transaction reads it first."""
        self.policy = (
            await self._session.execute(
                select(m.Settings)
                .where(m.Settings.id == 1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return self.policy

    async def set_policy(self, values, version, *, managed=KEEP):
        """Replace the policy columns if the row is still at ``version``.

        ``managed`` is the SHA-256 of the policy file that locks the row, or None to unlock it.
        """
        assignments = [f"{name}=:{name}" for name in values]
        params = {**values, "version": version}
        if managed is not KEEP:
            assignments.append(
                "managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
                "THEN NULL ELSE clock_timestamp() END"
            )
            params["managed"] = managed
        result = await self._session.execute(
            text(
                f"UPDATE billing.settings SET {','.join(assignments)},version=version+1 "
                "WHERE id=1 AND version=:version RETURNING version"
            ),
            params,
        )
        updated = result.scalar_one_or_none()
        if updated is not None:
            await self._session.refresh(self.policy)
        return updated

    async def has_money_facts(self):
        """Whether anything already carries the billing currency."""
        return (
            await self._session.execute(
                text(
                    "SELECT EXISTS(SELECT FROM billing.accounts) "
                    "OR EXISTS(SELECT FROM billing.price_versions) "
                    "OR EXISTS(SELECT FROM billing.receiving_accounts) "
                    "OR EXISTS(SELECT FROM billing.payment_channels)"
                )
            )
        ).scalar_one()

    async def get(self, kind, resource_id, org_id=None, *, lock=False):
        model = TABLES[kind]
        query = (
            select(model).where(model.id == resource_id).execution_options(populate_existing=True)
        )
        if org_id is not None:
            query = query.where(model.organization_id == org_id)
        if lock:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def account(self, org_id, *, lock=True):
        query = (
            select(m.Account)
            .where(m.Account.organization_id == org_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalar_one_or_none()
        if row is None:
            raise ServiceError("account_not_ready", "Billing account is not ready", 409)
        return row

    async def create(self, kind, values):
        row = TABLES[kind](id=values.pop("id", uuid4()), **values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def edit(self, row, values, *, versioned=True):
        for name, value in values.items():
            setattr(row, name, value)
        if versioned:
            row.version += 1
        await self._session.flush()
        return row

    async def flush(self):
        await self._session.flush()

    async def page(self, kind, org_id, limit, cursor, /, **filters):
        # Positional-only: a column filter may share a name with a parameter, e.g. price `kind`.
        model = TABLES[kind]
        query = select(model)
        if org_id is not None:
            query = query.where(model.organization_id == org_id)
        for name, value in filters.items():
            query = query.where(getattr(model, name) == value)
        at = model.posted_at if kind == "ledger" else model.created_at
        if cursor:
            query = query.where(tuple_(at, model.id) > cursor)
        rows = (
            (await self._session.execute(query.order_by(at, model.id).limit(limit + 1)))
            .scalars()
            .all()
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            timestamp = last.posted_at if kind == "ledger" else last.created_at
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([timestamp.isoformat(), str(last.id)]).encode()
            ).decode()
        return rows[:limit], next_cursor

    async def audit(self, actor_id, action, resource_id, org_id=None, details=None):
        self._session.add(
            m.Audit(
                id=uuid4(),
                actor_id=actor_id,
                organization_id=org_id,
                action=action,
                resource_id=resource_id,
                details=details or {},
            )
        )

    async def replay(self, actor, scope, key_digest):
        await self.lock(f"idem:{actor}:{scope}:{key_digest}")
        return (
            await self._session.execute(
                select(m.Idempotency).where(
                    m.Idempotency.actor_id == actor,
                    m.Idempotency.scope == scope,
                    m.Idempotency.key_digest == key_digest,
                )
            )
        ).scalar_one_or_none()

    async def remember(self, actor, scope, key_digest, request_digest, kind, resource_id):
        self._session.add(
            m.Idempotency(
                id=uuid4(),
                actor_id=actor,
                scope=scope,
                key_digest=key_digest,
                request_digest=request_digest,
                resource_kind=kind,
                resource_id=resource_id,
            )
        )

    async def provision(self, event, currency):
        await self.lock("provision:" + str(event.organization_id))
        receipt = await self._session.get(
            m.Inbox, ("identity.organization_created", event.event_id)
        )
        if receipt:
            if receipt.organization_id != event.organization_id:
                raise ServiceError("event_conflict", "Conflicting organization event", 409)
            return receipt.result_id
        account = (
            await self._session.execute(
                select(m.Account).where(m.Account.organization_id == event.organization_id)
            )
        ).scalar_one_or_none()
        if account is None:
            account = await self.create(
                "account", {"organization_id": event.organization_id, "currency": currency}
            )
            self._session.add(
                m.Outbox(
                    id=uuid4(),
                    topic=ACCOUNT_READY,
                    schema_version=ACCOUNT_READY_VERSION,
                    organization_id=event.organization_id,
                    aggregate_id=account.id,
                    payload={"account_id": str(account.id)},
                )
            )
            await self.audit(None, "account.created", account.id, event.organization_id)
        self._session.add(
            m.Inbox(
                consumer="identity.organization_created",
                event_id=event.event_id,
                organization_id=event.organization_id,
                result_id=account.id,
            )
        )
        return account.id

    async def pending_accounts(self, limit):
        rows = (
            await self._session.execute(
                select(m.Outbox)
                .where(m.Outbox.topic == ACCOUNT_READY, m.Outbox.delivered_at.is_(None))
                .order_by(m.Outbox.created_at, m.Outbox.id)
                .limit(limit)
            )
        ).scalars()
        return [
            AccountReady(row.id, row.organization_id, row.aggregate_id, row.schema_version)
            for row in rows
        ]

    async def acknowledge_account(self, event_id):
        row = (
            await self._session.execute(
                select(m.Outbox).where(m.Outbox.id == event_id).with_for_update()
            )
        ).scalar_one()
        if row.delivered_at is None:
            row.delivered_at = await self.now()

    async def funding_posted(self, record, entry_id):
        self._session.add(
            m.Outbox(
                id=uuid4(),
                topic=FUNDING_POSTED,
                schema_version=FUNDING_POSTED_VERSION,
                organization_id=record.organization_id,
                aggregate_id=record.id,
                payload={
                    "funding_record_id": str(record.id),
                    "ledger_entry_id": str(entry_id),
                    "amount": str(record.amount),
                    "currency": record.currency,
                    "source_type": record.source_type,
                    "payment_order_id": str(record.payment_order_id)
                    if record.payment_order_id
                    else None,
                },
            )
        )

    async def append_entry(
        self, account, amount, kind, source_type, source_id, actor=None, reverses=None
    ):
        if amount == 0:
            return None
        balance = account.balance + amount
        if balance < account.held_amount:
            raise ServiceError("insufficient_funds", "Available balance is insufficient", 409)
        from ..domain.money import MAX_AMOUNT

        if balance > MAX_AMOUNT:
            raise ServiceError(
                "amount_overflow", "Account balance exceeds supported precision", 409
            )
        account.balance = balance
        account.entry_sequence += 1
        account.version += 1
        row = m.LedgerEntry(
            id=uuid4(),
            organization_id=account.organization_id,
            account_id=account.id,
            sequence=account.entry_sequence,
            currency=account.currency,
            signed_amount=amount,
            balance_after=balance,
            kind=kind,
            source_type=source_type,
            source_id=source_id,
            actor_id=actor,
            reverses_entry_id=reverses,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def adjusted_amount(self, source_id):
        return (
            await self._session.execute(
                select(func.coalesce(func.sum(m.Adjustment.amount), 0)).where(
                    m.Adjustment.source_entry_id == source_id, m.Adjustment.status == "posted"
                )
            )
        ).scalar_one()

    async def current_price(self, kind, scope_id, at):
        return (
            await self._session.execute(
                select(m.PriceVersion)
                .where(
                    m.PriceVersion.kind == kind,
                    m.PriceVersion.scope_id == scope_id,
                    m.PriceVersion.state == "published",
                    m.PriceVersion.effective_at <= at,
                )
                .order_by(m.PriceVersion.effective_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def current_discount(self, org_id, at):
        row = (
            await self._session.execute(
                select(m.DiscountVersion)
                .where(
                    m.DiscountVersion.organization_id == org_id,
                    m.DiscountVersion.state == "published",
                    m.DiscountVersion.effective_at <= at,
                )
                .order_by(m.DiscountVersion.effective_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = await self._session.get(
                m.DiscountVersion, UUID("00000000-0000-0000-0000-000000000001")
            )
        return row

    async def last_effective(self, kind, scope_id):
        model = m.DiscountVersion if kind == "discount" else m.PriceVersion
        query = select(func.max(model.effective_at)).where(model.state == "published")
        if kind == "discount":
            query = query.where(model.organization_id == scope_id)
        else:
            query = query.where(model.kind == kind, model.scope_id == scope_id)
        return (await self._session.execute(query)).scalar_one()

    async def budget_period(self, policy, at, *, backfill=False):
        start, end = period_bounds(at, policy.period)
        row = (
            await self._session.execute(
                select(m.BudgetPeriod)
                .where(m.BudgetPeriod.policy_id == policy.id, m.BudgetPeriod.period_start == start)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = await self.create(
                "period",
                {
                    "organization_id": policy.organization_id,
                    "policy_id": policy.id,
                    "policy_version": policy.version,
                    "period_start": start,
                    "period_end": end,
                    "limit_amount": policy.limit_amount,
                },
            )
        row.policy_version = policy.version
        row.limit_amount = policy.limit_amount
        if backfill:
            query = select(m.RequestRun).where(
                m.RequestRun.organization_id == policy.organization_id,
                m.RequestRun.created_at >= start,
                m.RequestRun.created_at < end,
            )
            if policy.scope_type == "project":
                query = query.where(m.RequestRun.project_id == policy.project_id)
            elif policy.scope_type == "key":
                query = query.where(m.RequestRun.api_key_id == policy.api_key_id)
            for request in (await self._session.execute(query)).scalars():
                hold = await self._session.get(m.BudgetHold, (request.id, row.id))
                if hold is None:
                    reserved = (
                        request.reserved_amount
                        if request.reservation_state == "held"
                        else Decimal(0)
                    )
                    spent = (request.charged_amount or Decimal(0)) - request.refunded_amount
                    self._session.add(
                        m.BudgetHold(
                            request_id=request.id,
                            period_id=row.id,
                            organization_id=request.organization_id,
                            reserved=reserved,
                            spent=spent,
                        )
                    )
                    row.reserved += reserved
                    row.spent += spent
        await self._session.flush()
        return row

    async def request_budgets(self, request_id):
        return (
            await self._session.execute(
                select(m.BudgetPeriod, m.BudgetHold)
                .join(m.BudgetHold, m.BudgetHold.period_id == m.BudgetPeriod.id)
                .where(m.BudgetHold.request_id == request_id)
                .order_by(m.BudgetPeriod.id)
                .with_for_update()
            )
        ).all()

    async def settle_budget_holds(self, request_id, amount):
        for period, hold in await self.request_budgets(request_id):
            period.reserved -= hold.reserved
            period.spent += amount
            hold.reserved = Decimal(0)
            hold.spent = amount

    async def refund_budgets(self, request, amount):
        for period, hold in await self.request_budgets(request.id):
            period.spent -= amount
            hold.spent -= amount
        request.refunded_amount += amount

    async def attempts(self, request_id):
        return (
            (
                await self._session.execute(
                    select(m.Attempt)
                    .where(m.Attempt.request_id == request_id)
                    .order_by(m.Attempt.created_at, m.Attempt.id)
                )
            )
            .scalars()
            .all()
        )

    async def expired_requests(self, limit):
        return (
            await self._session.execute(
                select(m.RequestRun.organization_id, m.RequestRun.id)
                .where(
                    m.RequestRun.reservation_state == "held",
                    m.RequestRun.expires_at <= func.clock_timestamp(),
                    m.RequestRun.review_reason.is_(None),
                )
                .order_by(m.RequestRun.expires_at)
                .limit(limit)
            )
        ).all()

    async def reconcile(self, account):
        """Every difference between the account's projections and its facts, over all history."""
        return (await self.examine(account))[0]

    async def examine(self, account, verified=0):
        """Compare the account's projections and records with its ledger and reservation facts.

        Returns ``(issues, through)``. Ledger entries can be neither updated nor deleted
        (``ledger_immutable``) and their sequence is unique per account, so the per-entry checks
        (chain, reversals, orphans) only read entries after ``verified``, a sequence whose prefix
        passed them before; ``through`` is the prefix that passes them now. The other comparisons
        cover the whole account, each in one indexed query that returns only the differences.
        Run it under the account lock or in a REPEATABLE READ snapshot: every financial writer
        changes the projections and their facts in one transaction.
        """
        q = self._session.execute
        ids = {"account": account.id, "org": account.organization_id}
        base = Decimal(0)
        if verified:
            # An entry numbered below 1 would sit before the verified prefix; scan everything.
            row = (
                await q(
                    text("""
            SELECT balance_after, EXISTS(SELECT FROM billing.ledger_entries
              WHERE account_id=:account AND sequence<1) AS stray
            FROM billing.ledger_entries WHERE account_id=:account AND sequence=:verified
        """),
                    {"account": account.id, "verified": verified},
                )
            ).one_or_none()
            if row is None or row.stray:
                verified = 0
            else:
                base = row.balance_after
        after = " AND e.sequence>:verified" if verified else ""
        scope = {"account": account.id, "verified": verified}
        totals = (
            await q(
                text(f"""
            SELECT coalesce(sum(e.signed_amount),0) AS amount,count(*) AS entries,
              max(e.sequence) AS last
            FROM billing.ledger_entries e WHERE e.account_id=:account{after}
        """),
                scope,
            )
        ).one()
        ledger_sum = base + totals.amount
        hold_sum = (
            await q(
                text("""
            SELECT coalesce(sum(reserved_amount),0) FROM billing.request_runs
            WHERE organization_id=:org AND account_id=:account AND reservation_state='held'
        """),
                ids,
            )
        ).scalar_one()
        issues = []
        if account.balance != ledger_sum:
            issues.append(
                {"kind": "balance", "projection": str(account.balance), "facts": str(ledger_sum)}
            )
        if account.held_amount != hold_sum:
            issues.append(
                {
                    "kind": "held_amount",
                    "projection": str(account.held_amount),
                    "facts": str(hold_sum),
                }
            )
        # Sequences of entries that fail a per-entry check; the verified prefix ends before them.
        failed = []
        for entry_id, sequence in await q(
            text(f"""
            SELECT id,sequence FROM (
              SELECT e.id,e.sequence,e.balance_after,e.currency,
                cast(:verified AS bigint)+row_number() OVER w AS position,
                cast(:base AS numeric)+sum(e.signed_amount) OVER w AS running
              FROM billing.ledger_entries e WHERE e.account_id=:account{after}
              WINDOW w AS (ORDER BY e.sequence ROWS UNBOUNDED PRECEDING)) entries
            WHERE sequence<>position OR balance_after<>running OR currency<>:currency
            ORDER BY sequence
        """),
            {**scope, "base": base, "currency": account.currency},
        ):
            failed.append(sequence)
            issues.append({"kind": "ledger_chain", "entry_id": str(entry_id)})
        if account.entry_sequence != verified + totals.entries:
            issues.append({"kind": "entry_sequence"})
        # A reversal must point at an entry of the kind it reverses, and the reversals of one
        # source, counted in ledger order, must not exceed it.
        for row in await q(
            text(f"""
            SELECT e.id,e.sequence,s.id AS source_id,
              s.id IS NULL OR s.kind<>CASE WHEN e.kind='usage_refund' THEN 'usage_debit'
                ELSE 'funding_credit' END AS unmatched,
              coalesce((SELECT sum(abs(r.signed_amount)) FROM billing.ledger_entries r
                WHERE r.reverses_entry_id=e.reverses_entry_id AND r.account_id=e.account_id
                AND r.sequence<=e.sequence AND s.kind=CASE WHEN r.kind='usage_refund'
                THEN 'usage_debit' ELSE 'funding_credit' END)>abs(s.signed_amount),false) AS excess
            FROM billing.ledger_entries e LEFT JOIN billing.ledger_entries s
              ON s.id=e.reverses_entry_id AND s.account_id=e.account_id
            WHERE e.account_id=:account AND e.reverses_entry_id IS NOT NULL{after}
            ORDER BY e.sequence
        """),
            scope,
        ):
            if row.unmatched:
                failed.append(row.sequence)
                issues.append({"kind": "adjustment_source", "entry_id": str(row.id)})
            elif row.excess:
                failed.append(row.sequence)
                issues.append({"kind": "adjustment_total", "entry_id": str(row.source_id)})
        # Holds reference their request and period within one organization, so the holds of
        # the organization's requests are exactly the holds of its periods.
        for (period_id,) in await q(
            text("""
            SELECT p.id FROM billing.budget_periods p LEFT JOIN (
              SELECT h.period_id,sum(h.reserved) AS reserved,sum(h.spent) AS spent
              FROM billing.request_runs r JOIN billing.budget_holds h ON h.request_id=r.id
              WHERE r.organization_id=:org GROUP BY h.period_id) t ON t.period_id=p.id
            WHERE p.organization_id=:org
              AND (p.reserved<>coalesce(t.reserved,0) OR p.spent<>coalesce(t.spent,0))
            ORDER BY p.id
        """),
            ids,
        ):
            issues.append({"kind": "budget_totals", "period_id": str(period_id)})
        for kind, table in (("funding", "funding_records"), ("adjustment", "adjustments")):
            for (record_id,) in await q(
                text(f"""
                SELECT x.id FROM billing.{table} x LEFT JOIN billing.ledger_entries e
                  ON e.source_type=:kind AND e.source_id=x.id
                WHERE x.organization_id=:org AND ((x.status='posted')<>(e.id IS NOT NULL)
                  OR abs(e.signed_amount)<>x.amount OR e.account_id<>:account)
                ORDER BY x.created_at,x.id
            """),
                {**ids, "kind": kind},
            ):
                issues.append({"kind": kind + "_ledger", "record_id": str(record_id)})
        for row in await q(
            text("""
            SELECT id,refunds,charge FROM (
              SELECT r.id,r.created_at,
                r.refunded_amount<>coalesce((SELECT sum(abs(x.signed_amount))
                  FROM billing.ledger_entries x
                  WHERE x.reverses_entry_id=e.id AND x.kind='usage_refund'),0) AS refunds,
                (coalesce(r.charged_amount,0)>0)<>(e.id IS NOT NULL)
                  OR coalesce(e.signed_amount<>-coalesce(r.charged_amount,0),false) AS charge
              FROM billing.request_runs r LEFT JOIN billing.ledger_entries e
                ON e.source_type='request' AND e.source_id=r.id
              WHERE r.organization_id=:org AND r.account_id=:account) requests
            WHERE refunds OR charge ORDER BY created_at,id
        """),
            ids,
        ):
            if row.refunds:
                issues.append({"kind": "request_refunds", "request_id": str(row.id)})
            if row.charge:
                issues.append({"kind": "request_ledger", "request_id": str(row.id)})
        for request_id, period_id in await q(
            text("""
            SELECT r.id AS request_id,h.period_id
            FROM billing.request_runs r JOIN billing.budget_holds h ON h.request_id=r.id
            WHERE r.organization_id=:org AND r.account_id=:account AND (
                h.reserved<>CASE WHEN r.reservation_state='held' THEN r.reserved_amount ELSE 0 END
                OR h.spent<>coalesce(r.charged_amount,0)-r.refunded_amount)
            ORDER BY r.id,h.period_id
        """),
            ids,
        ):
            issues.append(
                {
                    "kind": "request_budget",
                    "request_id": str(request_id),
                    "period_id": str(period_id),
                }
            )
        for entry_id, sequence in await q(
            text(f"""
            SELECT e.id,e.sequence FROM billing.ledger_entries e
            WHERE e.account_id=:account{after} AND NOT CASE e.source_type
              WHEN 'funding' THEN EXISTS(SELECT FROM billing.funding_records x
                WHERE x.id=e.source_id AND x.organization_id=:org)
              WHEN 'adjustment' THEN EXISTS(SELECT FROM billing.adjustments x
                WHERE x.id=e.source_id AND x.organization_id=:org)
              WHEN 'request' THEN EXISTS(SELECT FROM billing.request_runs x
                WHERE x.id=e.source_id AND x.account_id=:account)
              ELSE false END
            ORDER BY e.sequence
        """),
            {**ids, "verified": verified},
        ):
            failed.append(sequence)
            issues.append({"kind": "orphan_ledger_entry", "entry_id": str(entry_id)})
        if not failed:
            return issues, verified if totals.last is None else totals.last
        through = (
            await q(
                text(f"""
            SELECT greatest(cast(:verified AS bigint),max(e.sequence))
            FROM billing.ledger_entries e WHERE e.account_id=:account AND e.sequence<:first{after}
        """),
                {**scope, "first": min(failed)},
            )
        ).scalar_one()
        return issues, through


@asynccontextmanager
async def transaction(database, repository_type=BillingRepository, *, snapshot=False):
    """A billing transaction; ``snapshot`` makes it REPEATABLE READ and read-only.

    A snapshot reads every table as of its first query and takes no row locks, so long reads
    such as the account check see a consistent state without making writers wait.
    """
    try:
        async with database.transaction() as session:
            if snapshot:
                # SET LOCAL ROLE and search_path take no snapshot; settings() below is the first.
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
            repo = repository_type(session)
            await repo.settings()
            yield repo
    except IntegrityError:
        raise ServiceError(
            "billing_conflict", "Billing fact conflicts with an existing record or constraint", 409
        ) from None
    except SQLAlchemyError:
        raise ServiceError(
            "storage_unavailable", "Storage is temporarily unavailable", 503
        ) from None
