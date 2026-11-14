from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from nexus.http import credentials, error_responses, optional_version

from ..application.routing import RoutingPolicies
from ..application.routing_schemas import Quota, QuotaPolicyResult, Route, RoutePolicyView


def create_router(get_service):
    router = APIRouter(prefix="/api/v1")
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[RoutingPolicies, Depends(get_service)]
    Version = Annotated[int | None, Depends(optional_version)]

    @router.post(
        "/route-policies",
        response_model=RoutePolicyView,
        responses={201: {"model": RoutePolicyView, "description": "Created"}},
    )
    async def route(
        body: Route, token: Token, service: Service, version: Version, response: Response
    ):
        result = await service.route(token, body, version)
        response.headers["ETag"] = f'"{result["version"]}"'
        response.status_code = 201 if result["version"] == 1 else 200
        return result

    @router.get("/route-policies")
    async def routes(token: Token, service: Service):
        return {"items": await service.list_routes(token)}

    @router.post(
        "/organizations/{organization_id}/quota-policies",
        response_model=QuotaPolicyResult,
        responses={
            201: {"model": QuotaPolicyResult, "description": "Created"},
            **error_responses(409),
        },
    )
    async def quota(
        organization_id: UUID,
        body: Quota,
        token: Token,
        service: Service,
        version: Version,
        response: Response,
    ):
        result = await service.quota(token, organization_id, body, version)
        response.headers["ETag"] = f'"{result["version"]}"'
        response.status_code = 201 if result["version"] == 1 else 200
        return result

    @router.get("/organizations/{organization_id}/quota-policies")
    async def quotas(organization_id: UUID, token: Token, service: Service):
        return {"items": await service.list_quotas(token, organization_id)}

    return router
