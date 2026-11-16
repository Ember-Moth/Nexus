from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import (
    business_errors,
    credentials,
    error_responses,
    expected_version,
    key_cursor_value,
    optional_version,
)

from ..application.routing import RoutingPolicies
from ..application.routing_schemas import (
    Quota,
    QuotaLayer,
    QuotaLimit,
    QuotaMetric,
    QuotaPolicyResult,
    QuotaPolicyView,
    QuotaScope,
    ResourceQuotaDefaults,
    ResourceQuotaDefaultsView,
    Route,
    RouteCandidates,
    RoutePolicyView,
)
from ..application.schemas import Page


def created(model):
    return {201: {"model": model, "description": "Created; the policy did not exist"}}


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["gateway routing"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[RoutingPolicies, Depends(get_service)]
    Version = Annotated[int | None, Depends(optional_version)]
    Required = Annotated[int, Depends(expected_version)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def written(response, result):
        response.headers["ETag"] = f'"{result["version"]}"'
        response.status_code = 201 if result["version"] == 1 else 200
        return result

    @router.put(
        "/route-policies/{organization_id}/{model_id}",
        response_model=RoutePolicyView,
        responses=created(RoutePolicyView),
    )
    async def put_route(
        organization_id: UUID,
        model_id: UUID,
        body: RouteCandidates,
        token: Token,
        service: Service,
        version: Version,
        response: Response,
    ):
        """Create the policy without If-Match, or replace the version If-Match names.

        Without If-Match an existing policy is not overwritten (428); a version that is not
        current, including one for a policy that does not exist, returns 412.
        """
        route = Route(
            organization_id=organization_id, model_id=model_id, candidates=body.candidates
        )
        return written(response, await service.route(token, route, version))

    @router.get("/route-policies", response_model=Page[RoutePolicyView])
    async def routes(token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None):
        """Route policies ordered by organization_id, then model_id."""
        return await service.list_routes(token, limit, key_cursor_value(cursor, 2))

    @router.put(
        "/organizations/{organization_id}/quota-policies/{layer}/{scope_type}/{scope_id}/{metric}",
        response_model=QuotaPolicyResult,
        responses={**created(QuotaPolicyResult), **error_responses(409)},
    )
    async def put_quota(
        organization_id: UUID,
        layer: QuotaLayer,
        scope_type: QuotaScope,
        scope_id: UUID,
        metric: QuotaMetric,
        body: QuotaLimit,
        token: Token,
        service: Service,
        version: Version,
        response: Response,
    ):
        """Create the policy without If-Match, or replace the version If-Match names.

        For organization scope, `scope_id` is the organization itself. Without If-Match an
        existing policy is not overwritten (428); a version that is not current, including one
        for a policy that does not exist, returns 412.
        """
        quota = Quota(
            layer=layer,
            scope_type=scope_type,
            scope_id=scope_id,
            metric=metric,
            limit_value=body.limit_value,
        )
        return written(response, await service.quota(token, organization_id, quota, version))

    @router.get(
        "/organizations/{organization_id}/quota-policies", response_model=Page[QuotaPolicyView]
    )
    async def quotas(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        """Quota policies of the organization ordered by id."""
        return await service.list_quotas(token, organization_id, limit, key_cursor_value(cursor, 1))

    @router.get("/settings/resource-quotas", response_model=ResourceQuotaDefaultsView)
    async def resource_quota_defaults(token: Token, service: Service, response: Response):
        """Platform defaults of the tenant resource counts; platform administrators only."""
        result = await service.resource_defaults(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/resource-quotas",
        response_model=ResourceQuotaDefaultsView,
        responses=business_errors("version_conflict", "policy_managed_externally"),
    )
    async def update_resource_quota_defaults(
        body: ResourceQuotaDefaults,
        token: Token,
        service: Service,
        version: Required,
        response: Response,
    ):
        """Replace the defaults. Nodes enforce them once the cluster publishes its next release."""
        result = await service.update_resource_defaults(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    return router
