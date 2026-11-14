from nexus.shared.errors import ServiceError


class RoutingPolicies:
    def __init__(self, repository, access):
        self._repository = repository
        self._access = access

    async def route(self, token, body, version):
        actor = await self._access(token, body.organization_id, operation="pricing")
        ids = [c.deployment_id for c in body.candidates]
        if len(set(ids)) != len(ids):
            raise ServiceError("duplicate_candidate", "Duplicate route candidate", 422)
        await self._repository.save_route(body, version, ids, actor)
        return {**body.model_dump(), "version": (version or 0) + 1}

    async def quota(self, token, org, body, version):
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

    async def list_routes(self, token):
        await self._access(token, operation="pricing")
        return await self._repository.list_routes()

    async def list_quotas(self, token, org):
        await self._access(token, org)
        return await self._repository.list_quotas(org)
