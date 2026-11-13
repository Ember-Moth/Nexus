from .authorization import AccessControl
from .errors import IdentityError, missing
from .ports import ModelLookup, TransactionFactory
from .schemas import ModelVisibility


def effective_models(policies, *, omit=None, platform_only=False):
    baseline = next(
        (p for p in policies if p.layer == "platform" and p.scope_type == "organization"), None
    )
    if baseline is None:
        return frozenset()
    allowed = set(baseline.allowed_models)
    for policy in policies:
        if platform_only and policy.layer != "platform":
            continue
        if omit == (policy.layer, policy.scope_type, policy.scope_id):
            continue
        allowed.intersection_update(policy.allowed_models)
    return frozenset(allowed)


class ModelAccessService:
    def __init__(self, transactions: TransactionFactory, models: ModelLookup):
        self._transactions = transactions
        self._access = AccessControl(transactions)
        self._models = models

    async def _context(self, tx, actor, org_id, scope_type, scope_id, *, write=False, layer=None):
        if write and layer == "platform":
            self._access.platform_admin(actor)
        if scope_type == "organization":
            if scope_id is not None and scope_id != org_id:
                raise missing(scope_id)
            if write:
                await self._access.project_scope(tx, actor, org_id, write=True, owner=True)
            else:
                await self._access.organization(tx, actor, org_id, finance_read=True)
            return org_id, None, None
        if scope_id is None:
            raise IdentityError("invalid_scope", "Scope identifier is required", 422)
        if scope_type == "project":
            await self._access.project(tx, actor, org_id, scope_id, write=write, credentials=write)
            return scope_id, scope_id, None
        await self._access.project_scope(tx, actor, org_id, write=write, credentials=True)
        key = await tx.applications.key(org_id, scope_id)
        if key is None:
            raise missing(scope_id)
        await self._access.project(tx, actor, org_id, key.project_id, write=write, credentials=True)
        return key.id, key.project_id, key.id

    async def _validate_parent(
        self, tx, org_id, layer, scope_type, scope_id, project_id, key_id, models
    ):
        if layer == "platform" and scope_type == "organization":
            return
        policies = await tx.policies.applicable(org_id, project_id, key_id)
        allowed = effective_models(
            policies, omit=(layer, scope_type, scope_id), platform_only=layer == "platform"
        )
        if not models <= allowed:
            raise IdentityError(
                "policy_expands_access",
                "Policy must remain within its parent model permissions",
                403,
                resource_id=scope_id,
            )

    async def list_policies(self, token, org_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            member_id = await self._access.project_scope(tx, actor, org_id, credentials=True)
            return await tx.policies.list(org_id, member_id, limit, cursor)

    async def get_policy(self, token, org_id, policy_id):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project_scope(tx, actor, org_id, credentials=True)
            policy = await tx.policies.get(org_id, policy_id)
            if policy is None:
                raise missing(policy_id)
            await self._context(tx, actor, org_id, policy.scope_type, policy.scope_id)
            return policy

    async def create_policy(self, token, org_id, body):
        models = frozenset(body.allowed_models)
        # Check authorization and parent limits before using the catalog's read-only port.
        async with self._access.authenticated(token) as (tx, actor):
            scope, project_id, key_id = await self._context(
                tx, actor, org_id, body.scope_type, body.scope_id, write=True, layer=body.layer
            )
            await self._validate_parent(
                tx, org_id, body.layer, body.scope_type, scope, project_id, key_id, models
            )
        if await self._models(models) != models:
            raise IdentityError(
                "invalid_models", "All referenced models must exist and be active", 422
            )
        async with self._access.authenticated(token) as (tx, actor):
            scope, project_id, key_id = await self._context(
                tx, actor, org_id, body.scope_type, body.scope_id, write=True, layer=body.layer
            )
            await self._validate_parent(
                tx, org_id, body.layer, body.scope_type, scope, project_id, key_id, models
            )
            policy = await tx.policies.create(
                org_id, body.layer, body.scope_type, scope, project_id, key_id, models
            )
            await tx.audit.record(
                actor.user.id,
                "access_policy.created",
                policy.id,
                organization_id=org_id,
                details={"after": policy.model_dump(mode="json")},
            )
            return policy

    async def update_policy(self, token, org_id, policy_id, body, version):
        models = frozenset(body.allowed_models)
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, org_id)
            policy = await tx.policies.get(org_id, policy_id)
            if policy is None:
                raise missing(policy_id)
            scope, project_id, key_id = await self._context(
                tx,
                actor,
                org_id,
                policy.scope_type,
                policy.scope_id,
                write=True,
                layer=policy.layer,
            )
            self._access.version(policy.version, version)
            await self._validate_parent(
                tx, org_id, policy.layer, policy.scope_type, scope, project_id, key_id, models
            )
        if await self._models(models) != models:
            raise IdentityError(
                "invalid_models", "All referenced models must exist and be active", 422
            )
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, org_id, lock=True)
            policy = await tx.policies.get(org_id, policy_id)
            if policy is None:
                raise missing(policy_id)
            scope, project_id, key_id = await self._context(
                tx,
                actor,
                org_id,
                policy.scope_type,
                policy.scope_id,
                write=True,
                layer=policy.layer,
            )
            self._access.version(policy.version, version)
            await self._validate_parent(
                tx, org_id, policy.layer, policy.scope_type, scope, project_id, key_id, models
            )
            result = await tx.policies.replace(org_id, policy_id, models)
            await tx.audit.record(
                actor.user.id,
                "access_policy.updated",
                policy_id,
                organization_id=org_id,
                details={
                    "before": policy.model_dump(mode="json"),
                    "after": result.model_dump(mode="json"),
                },
            )
            return result

    async def visibility(self, token, org_id=None, project_id=None, key_id=None):
        async with self._access.authenticated(token) as (tx, actor):
            if org_id is None:
                if project_id or key_id:
                    raise IdentityError(
                        "organization_required", "Organization context is required", 422
                    )
                self._access.platform_admin(actor)
                return ModelVisibility(actor.user.id, None)
            org = await self._access.organization(tx, actor, org_id, finance_read=True)
            if key_id:
                _, actual_project_id, _ = await self._context(tx, actor, org_id, "key", key_id)
                if project_id and actual_project_id != project_id:
                    raise missing(key_id)
                project_id = actual_project_id
            elif project_id:
                await self._context(tx, actor, org_id, "project", project_id)
            if org.status not in {"active", "provisioning"}:
                return ModelVisibility(actor.user.id, frozenset())
            if project_id:
                project = await tx.applications.project(org_id, project_id)
                if project.status != "active":
                    return ModelVisibility(actor.user.id, frozenset())
            if key_id:
                key = await tx.applications.key(org_id, key_id)
                account = await tx.applications.service_account(org_id, key.service_account_id)
                if (
                    account.status != "active"
                    or key.revoked_at
                    or (key.expires_at and key.expires_at <= await tx.directory.now())
                ):
                    return ModelVisibility(actor.user.id, frozenset())
            return ModelVisibility(
                actor.user.id,
                effective_models(await tx.policies.applicable(org_id, project_id, key_id)),
            )
