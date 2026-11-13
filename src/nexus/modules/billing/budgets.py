from nexus.shared.errors import ServiceError

from .common import found, versioned, view
from .schemas import BudgetPeriodView, BudgetView, Page


class BudgetService:
    def __init__(self, transactions, access, currency):
        self._transactions, self._access, self.currency = transactions, access, currency

    async def create(self, token, org_id, body):
        scope = await self._access.authorize(
            token,
            org_id,
            operation="budget",
            scope_type=body.scope_type,
            scope_id=body.scope_id,
            layer=body.layer,
        )
        if body.currency != self.currency:
            raise ServiceError("currency_mismatch", "Currency must match the account currency", 422)
        async with self._transactions() as repo:
            await repo.account(org_id)
            row = await repo.create(
                "budget",
                {
                    **body.model_dump(),
                    "scope_id": body.scope_id or org_id,
                    "organization_id": org_id,
                    "project_id": scope.project_id,
                    "api_key_id": scope.key_id,
                },
            )
            await repo.budget_period(row, await repo.now(), backfill=True)
            await repo.audit(
                scope.actor_id,
                "budget.created",
                row.id,
                org_id,
                {"limit_amount": str(row.limit_amount), "period": row.period},
            )
            return view(BudgetView, row)

    async def update(self, token, org_id, resource_id, body, version):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            current = found(await repo.get("budget", resource_id, org_id))
            layer = current.layer
        scope = await self._access.authorize(token, org_id, operation="budget", layer=layer)
        async with self._transactions() as repo:
            await repo.account(org_id)
            row = found(await repo.get("budget", resource_id, org_id, lock=True))
            versioned(row, version)
            before = view(BudgetView, row).model_dump(mode="json")
            await repo.edit(row, body.model_dump(exclude_unset=True))
            await repo.budget_period(row, await repo.now(), backfill=row.status == "active")
            await repo.audit(
                scope.actor_id,
                "budget.updated",
                row.id,
                org_id,
                {"before": before, "after": view(BudgetView, row).model_dump(mode="json")},
            )
            return view(BudgetView, row)

    async def periods(self, token, org_id, resource_id, limit, cursor):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            found(await repo.get("budget", resource_id, org_id))
            rows, cursor = await repo.page("period", org_id, limit, cursor, policy_id=resource_id)
            return Page[BudgetPeriodView](
                items=[
                    view(
                        BudgetPeriodView,
                        row,
                        over_limit=row.spent + row.reserved > row.limit_amount,
                    )
                    for row in rows
                ],
                next_cursor=cursor,
            )
