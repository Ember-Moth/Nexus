from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from nexus.http import credentials, error_responses, optional_version

from ..application.routing import RoutingPolicies
from ..application.routing_schemas import (
    Quota,
    QuotaLayer,
    QuotaLimit,
    QuotaMetric,
    QuotaPolicyResult,
    QuotaScope,
    Route,
    RouteCandidates,
    RoutePolicyView,
)


def created(model):
    return {201: {"model": model, "description": "Created; the policy did not exist"}}


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["gateway routing"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[RoutingPolicies, Depends(get_service)]
    Version = Annotated[int | None, Depends(optional_version)]

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

    @router.post(
        "/route-policies",
        response_model=RoutePolicyView,
        responses=created(RoutePolicyView),
        deprecated=True,
    )
    async def route(
        body: Route, token: Token, service: Service, version: Version, response: Response
    ):
        """Deprecated alias of `PUT /route-policies/{organization_id}/{model_id}`."""
        return written(response, await service.route(token, body, version))

    @router.get("/route-policies")
    async def routes(token: Token, service: Service):
        return {"items": await service.list_routes(token)}

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

    @router.post(
        "/organizations/{organization_id}/quota-policies",
        response_model=QuotaPolicyResult,
        responses={**created(QuotaPolicyResult), **error_responses(409)},
        deprecated=True,
    )
    async def quota(
        organization_id: UUID,
        body: Quota,
        token: Token,
        service: Service,
        version: Version,
        response: Response,
    ):
        """Deprecated alias of the quota policy PUT on its natural key."""
        return written(response, await service.quota(token, organization_id, body, version))

    @router.get("/organizations/{organization_id}/quota-policies")
    async def quotas(organization_id: UUID, token: Token, service: Service):
        return {"items": await service.list_quotas(token, organization_id)}

    return router
