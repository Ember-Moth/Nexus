from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from nexus.http import credentials, optional_version

from .schemas import Schema


class Candidate(Schema):
    deployment_id: UUID
    priority: int = Field(default=0, ge=0, le=10000, strict=True)
    weight: int = Field(default=1, ge=1, le=10000, strict=True)


class Route(Schema):
    organization_id: UUID
    model_id: UUID
    candidates: list[Candidate] = Field(min_length=1, max_length=256)


class Quota(Schema):
    layer: Literal["platform", "customer"] = "customer"
    scope_type: Literal["organization", "project", "key"] = "organization"
    scope_id: UUID | None = None
    metric: Literal["rpm", "tpm", "concurrency"]
    limit_value: int = Field(ge=1, le=2000000000, strict=True)


def create_router(get_service):
    router = APIRouter(prefix="/api/v1")
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[object, Depends(get_service)]
    Version = Annotated[int | None, Depends(optional_version)]

    @router.post("/route-policies")
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

    @router.post("/organizations/{org}/quota-policies")
    async def quota(
        org: UUID, body: Quota, token: Token, service: Service, version: Version, response: Response
    ):
        result = await service.quota(token, org, body, version)
        response.headers["ETag"] = f'"{result["version"]}"'
        response.status_code = 201 if result["version"] == 1 else 200
        return result

    @router.get("/organizations/{org}/quota-policies")
    async def quotas(org: UUID, token: Token, service: Service):
        return {"items": await service.list_quotas(token, org)}

    return router
