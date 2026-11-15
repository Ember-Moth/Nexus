"""The billing policy: currency and operator-tunable limits in the billing.settings row."""

from uuid import UUID

from nexus.shared.errors import ServiceError
from nexus.shared.policy import CachedPolicy, ImportPlan, PolicyRejected, changes, plan
from nexus.shared.views import managed_source

from .schemas import BillingPolicy, BillingPolicyView

GROUP = "billing.policy"
# Audit resource of changes to the singleton billing policy.
POLICY_RESOURCE = UUID(int=0)
# billing.settings columns behind each policy field.
COLUMNS = {
    ("currency",): "currency",
    ("vector_storage", "prepaid_days"): "vector_storage_prepaid_days",
    ("vector_storage", "lapse_policy"): "vector_storage_lapse_policy",
    ("payments", "requests_per_merchant_per_minute"): "payment_requests_per_merchant_per_minute",
    ("payments", "notifications_per_channel_per_minute"): "payment_notification_per_channel",
    ("payments", "notifications_per_ip_per_minute"): "payment_notification_per_ip",
}


async def stored_currency(transactions) -> str:
    """The billing currency every account, price, budget and channel must use."""
    async with transactions() as repo:
        return repo.policy.currency


def policy_view(row) -> BillingPolicyView:
    values: dict = {"version": row.version}
    for path, column in COLUMNS.items():
        node = values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = getattr(row, column)
    managed = managed_source({"managed_sha256": row.managed_sha256, "managed_at": row.managed_at})
    return BillingPolicyView.model_validate({**values, "managed": managed})


def editable(view: BillingPolicyView) -> dict:
    return view.model_dump(mode="json", exclude={"version", "managed"})


def policy_columns(policy: BillingPolicy) -> dict:
    values = policy.model_dump()
    result = {}
    for path, column in COLUMNS.items():
        node = values
        for key in path:
            node = node[key]
        result[column] = node
    return result


class BillingSettings:
    def __init__(self, transactions, access):
        self._transactions = transactions
        self._access = access
        self.cached = CachedPolicy(self._load)

    async def _load(self) -> BillingPolicyView:
        async with self._transactions() as repo:
            return policy_view(repo.policy)

    async def get(self, token) -> BillingPolicyView:
        await self._access.administrator(token)
        return await self._load()

    async def update(self, token, body: BillingPolicy, version: int) -> BillingPolicyView:
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            current = policy_view(repo.policy)
            if current.managed is not None:
                raise ServiceError(
                    "policy_managed_externally",
                    "The billing policy is managed by a policy file",
                    409,
                )
            result = await self._write(repo, current, body, version)
            await repo.audit(
                actor,
                "billing_policy.updated",
                POLICY_RESOURCE,
                details=changes(editable(current), editable(result)),
            )
        self.cached.put(result)
        return result

    @staticmethod
    async def _write(repo, current, body, version, **lock):
        if current.version != version:
            raise ServiceError("version_conflict", "Billing policy has changed", 412)
        if body.currency != current.currency and await repo.has_money_facts():
            raise ServiceError(
                "currency_locked",
                "The billing currency cannot change once accounts, prices or channels exist",
                409,
            )
        if not await repo.set_policy(policy_columns(body), version, **lock):
            raise ServiceError("version_conflict", "Billing policy has changed", 412)
        return policy_view(repo.policy)

    async def plan_import(self, mode, values, file_sha256) -> ImportPlan:
        """Plan the ``billing.policy`` group of a policy file; nothing is written yet."""
        async with self._transactions() as repo:
            current = policy_view(repo.policy)
            target, found, skipped = plan(
                GROUP,
                mode,
                editable(current),
                current.version,
                current.managed is not None,
                values,
                BillingPolicy,
            )
            if target["currency"] != current.currency and await repo.has_money_facts():
                raise PolicyRejected(GROUP, "currency cannot change once billing facts exist")
        body = BillingPolicy.model_validate(target)

        async def write(details):
            async with self._transactions() as repo:
                result = await self._write(
                    repo,
                    policy_view(repo.policy),
                    body,
                    current.version,
                    managed=file_sha256 if mode == "managed" else None,
                )
                await repo.audit(
                    None, "billing_policy.updated", POLICY_RESOURCE, details={**details, **found}
                )
            self.cached.put(result)

        return ImportPlan(GROUP, mode, mode == "managed", found, skipped, write if found else None)
