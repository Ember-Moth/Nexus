from uuid import uuid4

from nexus.shared.errors import ServiceError

from .common import found, replay, versioned, view
from .funding import post_funding_record
from .ports import Access, Transactions
from .schemas import (
    AccountView,
    AdjustmentView,
    BudgetView,
    FundingView,
    LedgerView,
    Page,
    ReceivingView,
    RequestView,
)

VIEWS = {
    "receiving": ReceivingView,
    "funding": FundingView,
    "adjustment": AdjustmentView,
    "ledger": LedgerView,
    "budget": BudgetView,
    "request": RequestView,
}


class BillingService:
    def __init__(self, transactions: Transactions, access: Access, currency: str):
        self._transactions, self._access, self.currency = transactions, access, currency

    def check_currency(self, currency):
        if currency != self.currency:
            raise ServiceError(
                "currency_mismatch", "Currency must match the deployment billing currency", 422
            )

    async def check(self):
        async with self._transactions():
            pass

    async def provision_account(self, event):
        if event.schema_version != 1:
            raise ServiceError(
                "unsupported_event_version", "Unsupported organization event version", 422
            )
        async with self._transactions() as repo:
            return await repo.provision(event, self.currency)

    async def pending_accounts(self, limit=100):
        async with self._transactions() as repo:
            return await repo.pending_accounts(limit)

    async def acknowledge_account(self, event_id):
        async with self._transactions() as repo:
            await repo.acknowledge_account(event_id)

    async def account(self, token, org_id):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            account = await repo.account(org_id, lock=False)
            return view(
                AccountView,
                account,
                available_balance=account.balance - account.held_amount,
                as_of=await repo.now(),
            )

    async def list(self, token, kind, org_id, limit, cursor):
        if kind == "receiving":
            await self._access.authorize(token, operation="finance")
        else:
            await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            rows, next_cursor = await repo.page(kind, org_id, limit, cursor)
            return Page[VIEWS[kind]](
                items=[view(VIEWS[kind], row) for row in rows], next_cursor=next_cursor
            )

    async def get(self, token, kind, org_id, resource_id):
        if kind == "receiving":
            await self._access.authorize(token, operation="finance")
        else:
            await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            return view(VIEWS[kind], found(await repo.get(kind, resource_id, org_id)))

    async def create_receiving(self, token, body, key):
        actor = await self._access.authorize(token, operation="finance")
        self.check_currency(body.currency)
        async with self._transactions() as repo:
            scope = "receiving:create"
            old, kd, rd = await replay(repo, actor, scope, key, body.model_dump(mode="json"))
            if old:
                return view(ReceivingView, found(await repo.get("receiving", old.resource_id)))
            row = await repo.create(
                "receiving", {**body.model_dump(), "receipt_namespace": "manual:" + body.reference}
            )
            await repo.remember(actor, scope, kd, rd, "receiving", row.id)
            await repo.audit(actor, "receiving_account.created", row.id)
            return view(ReceivingView, row)

    async def update_receiving(self, token, resource_id, body, version):
        actor = await self._access.authorize(token, operation="finance")
        async with self._transactions() as repo:
            row = found(await repo.get("receiving", resource_id, lock=True))
            versioned(row, version)
            await repo.edit(row, body.model_dump(exclude_unset=True))
            await repo.audit(
                actor,
                "receiving_account.updated",
                row.id,
                details=body.model_dump(exclude_unset=True),
            )
            return view(ReceivingView, row)

    async def create_funding(self, token, org_id, body, key):
        scope = await self._access.authorize(token, org_id, operation="finance")
        self.check_currency(body.currency)
        async with self._transactions() as repo:
            action = f"{org_id}:funding:create"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, body.model_dump(mode="json")
            )
            if old:
                return view(FundingView, found(await repo.get("funding", old.resource_id, org_id)))
            account = await repo.account(org_id)
            if scope.organization_status == "archived":
                raise ServiceError(
                    "organization_archived",
                    "Archived organizations cannot receive new funding",
                    409,
                )
            receiving = found(await repo.get("receiving", body.receiving_account_id, lock=True))
            if receiving.status != "active" or receiving.currency != body.currency:
                raise ServiceError(
                    "receiving_unavailable",
                    "Receiving account is disabled or has a different currency",
                    409,
                )
            if body.received_at > await repo.now():
                raise ServiceError(
                    "invalid_received_at", "Receipt time cannot be in the future", 422
                )
            row = await repo.create(
                "funding",
                {
                    **body.model_dump(),
                    "organization_id": org_id,
                    "account_id": account.id,
                    "receipt_namespace": receiving.receipt_namespace,
                    "created_by": scope.actor_id,
                },
            )
            await repo.remember(scope.actor_id, action, kd, rd, "funding", row.id)
            await repo.audit(
                scope.actor_id,
                "funding.created",
                row.id,
                org_id,
                {"amount": str(body.amount), "currency": body.currency},
            )
            return view(FundingView, row)

    async def update_funding(self, token, org_id, resource_id, body, version):
        scope = await self._access.authorize(token, org_id, operation="finance")
        self.check_currency(body.currency)
        async with self._transactions() as repo:
            await repo.account(org_id)
            row = found(await repo.get("funding", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.source_type != "manual":
                raise ServiceError(
                    "gateway_funding_immutable",
                    "Gateway receipts are managed through their payment order",
                    409,
                )
            if row.status != "draft":
                raise ServiceError("record_immutable", "Only draft funding can be edited", 409)
            receiving = found(await repo.get("receiving", body.receiving_account_id, lock=True))
            if receiving.status != "active" or receiving.currency != body.currency:
                raise ServiceError("receiving_unavailable", "Receiving account is unavailable", 409)
            if body.received_at > await repo.now():
                raise ServiceError(
                    "invalid_received_at", "Receipt time cannot be in the future", 422
                )
            before = view(FundingView, row).model_dump(mode="json")
            await repo.edit(
                row, {**body.model_dump(), "receipt_namespace": receiving.receipt_namespace}
            )
            await repo.audit(
                scope.actor_id,
                "funding.updated",
                row.id,
                org_id,
                {"before": before, "after": view(FundingView, row).model_dump(mode="json")},
            )
            return view(FundingView, row)

    async def cancel_funding(self, token, org_id, resource_id, version):
        scope = await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            await repo.account(org_id)
            row = found(await repo.get("funding", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.source_type != "manual":
                raise ServiceError(
                    "gateway_funding_immutable",
                    "Gateway receipts are managed through their payment order",
                    409,
                )
            if row.status != "draft":
                raise ServiceError("invalid_funding_state", "Only a draft can be cancelled", 409)
            await repo.edit(row, {"status": "cancelled"})
            await repo.audit(scope.actor_id, "funding.cancelled", row.id, org_id)
            return view(FundingView, row)

    async def post_funding(self, token, org_id, resource_id, version, key):
        scope = await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            action = f"{org_id}:funding:post"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, {"id": str(resource_id), "version": version}
            )
            if old:
                return view(FundingView, found(await repo.get("funding", old.resource_id, org_id)))
            account = await repo.account(org_id)
            row = found(await repo.get("funding", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.source_type != "manual":
                raise ServiceError(
                    "gateway_funding_immutable",
                    "Gateway receipts are managed through their payment order",
                    409,
                )
            if row.status != "draft":
                raise ServiceError("invalid_funding_state", "Funding is not a draft", 409)
            # A real receipt remains postable after a receiving account is disabled.
            await post_funding_record(repo, account, row, actor_id=scope.actor_id)
            await repo.remember(scope.actor_id, action, kd, rd, "funding", row.id)
            return view(FundingView, row)

    async def create_adjustment(self, token, org_id, body, key):
        scope = await self._access.authorize(token, org_id, operation="finance")
        self.check_currency(body.currency)
        async with self._transactions() as repo:
            action = f"{org_id}:adjustment:create"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, body.model_dump(mode="json")
            )
            if old:
                return view(
                    AdjustmentView, found(await repo.get("adjustment", old.resource_id, org_id))
                )
            account = await repo.account(org_id)
            source = found(await repo.get("ledger", body.source_entry_id, org_id))
            if source.kind not in {"funding_credit", "usage_debit"}:
                raise ServiceError(
                    "invalid_adjustment_source",
                    "Adjustments must refer to an original funding credit or usage debit",
                    422,
                )
            if body.amount + await repo.adjusted_amount(source.id) > abs(source.signed_amount):
                raise ServiceError(
                    "adjustment_exceeds_source",
                    "Adjustment exceeds the remaining source amount",
                    409,
                )
            row = await repo.create(
                "adjustment",
                {
                    **body.model_dump(),
                    "organization_id": org_id,
                    "account_id": account.id,
                    "created_by": scope.actor_id,
                },
            )
            await repo.remember(scope.actor_id, action, kd, rd, "adjustment", row.id)
            await repo.audit(scope.actor_id, "adjustment.created", row.id, org_id)
            return view(AdjustmentView, row)

    async def post_adjustment(self, token, org_id, resource_id, version, key):
        scope = await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            action = f"{org_id}:adjustment:post"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, {"id": str(resource_id), "version": version}
            )
            if old:
                return view(
                    AdjustmentView, found(await repo.get("adjustment", old.resource_id, org_id))
                )
            account = await repo.account(org_id)
            row = found(await repo.get("adjustment", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.status != "draft":
                raise ServiceError("invalid_adjustment_state", "Adjustment is not a draft", 409)
            source = found(await repo.get("ledger", row.source_entry_id, org_id))
            if row.amount + await repo.adjusted_amount(source.id) > abs(source.signed_amount):
                raise ServiceError(
                    "adjustment_exceeds_source",
                    "Adjustment exceeds the remaining source amount",
                    409,
                )
            refund = source.kind == "usage_debit"
            if refund:
                request = found(await repo.get("request", source.source_id, org_id, lock=True))
                await repo.refund_budgets(request, row.amount)
            await repo.append_entry(
                account,
                row.amount if refund else -row.amount,
                "usage_refund" if refund else "funding_reversal",
                "adjustment",
                row.id,
                scope.actor_id,
                source.id,
            )
            await repo.edit(
                row,
                {"status": "posted", "posted_by": scope.actor_id, "posted_at": await repo.now()},
            )
            await repo.remember(scope.actor_id, action, kd, rd, "adjustment", row.id)
            await repo.audit(
                scope.actor_id,
                "adjustment.posted",
                row.id,
                org_id,
                {"amount": str(row.amount), "source_entry_id": str(source.id)},
            )
            return view(AdjustmentView, row)

    async def reconcile(self, token, org_id):
        scope = await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            account = await repo.account(org_id)
            issues = await repo.reconcile(account)
            run_id = uuid4()
            await repo.audit(
                scope.actor_id, "account.reconciled", run_id, org_id, {"issues": issues}
            )
            return {
                "id": run_id,
                "organization_id": org_id,
                "consistent": not issues,
                "issues": issues,
                "as_of": await repo.now(),
            }
