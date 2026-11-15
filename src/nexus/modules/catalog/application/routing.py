from nexus.shared.errors import ServiceError
from nexus.shared.policy import ImportPlan, plan

from .routing_schemas import ORGANIZATION_METRICS, ResourceQuotaDefaults


class RoutingPolicies:
    def __init__(self, repository, access, administrator):
        self._repository = repository
        self._access = access
        self._administrator = administrator

    async def route(self, token, body, version):
        actor = await self._access(token, body.organization_id, operation="pricing")
        ids = [c.deployment_id for c in body.candidates]
        if len(set(ids)) != len(ids):
            raise ServiceError("duplicate_candidate", "Duplicate route candidate", 422)
        await self._repository.save_route(body, version, ids, actor)
        return {**body.model_dump(), "version": (version or 0) + 1}

    async def quota(self, token, org, body, version):
        if body.metric in ORGANIZATION_METRICS and (
            body.layer != "platform"
            or body.scope_type != "organization"
            or body.scope_id not in (None, org)
        ):
            raise ServiceError(
                "invalid_scope",
                "Resource counts and admission concurrency are platform policies of a whole "
                "organization",
                422,
            )
        actor = await self._access(
            token,
            org,
            operation="budget",
            layer=body.layer,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
        )
        if body.scope_type != "organization" and body.scope_id is None:
            raise ServiceError("invalid_scope", "Scope identifier is required", 422)
        policy_id = await self._repository.save_quota(
            org, body, version, body.scope_id or org, actor
        )
        return {**body.model_dump(), "id": policy_id, "version": (version or 0) + 1}

    async def list_routes(self, token, limit, cursor):
        await self._access(token, operation="pricing")
        items, next_cursor = await self._repository.list_routes(limit, cursor)
        return {"items": items, "next_cursor": next_cursor}

    async def list_quotas(self, token, org, limit, cursor):
        await self._access(token, org)
        items, next_cursor = await self._repository.list_quotas(org, limit, cursor)
        return {"items": items, "next_cursor": next_cursor}

    async def resource_defaults(self, token):
        await self._administrator(token)
        return await self._repository.resource_defaults()

    async def update_resource_defaults(self, token, body, version):
        actor = await self._administrator(token)
        return await self._repository.save_resource_defaults(body, version, actor)

    async def plan_import(self, mode, values, file_sha256):
        """Plan the ``catalog.quota_defaults`` group of a policy file; nothing is written yet."""
        group = "catalog.quota_defaults"
        current = await self._repository.resource_defaults()
        target, found, skipped = plan(
            group,
            mode,
            current.model_dump(mode="json", exclude={"version", "managed"}),
            current.version,
            current.managed is not None,
            values,
            ResourceQuotaDefaults,
        )
        body = ResourceQuotaDefaults.model_validate(target)

        async def write(details):
            await self._repository.save_resource_defaults(
                body,
                current.version,
                None,
                managed=file_sha256 if mode == "managed" else None,
                details={**details, **found},
            )

        return ImportPlan(group, mode, mode == "managed", found, skipped, write if found else None)
