"""Management HTTP API; authorization and transactions stay in identity services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value, error_responses, expected_version

from ..application.model_access import ModelAccessService
from ..application.schemas import (
    AccessPolicyCreate,
    AccessPolicyPatch,
    AccessPolicyView,
    Page,
)

CONFLICT = error_responses(409)


def create_policy_router(get_service):
    router = APIRouter(
        prefix="/api/v1/organizations/{organization_id}/access-policies", tags=["model access"]
    )
    Service = Annotated[ModelAccessService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def tagged(response, result):
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("", response_model=Page[AccessPolicyView])
    async def policies(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_policies(token, organization_id, limit, cursor_value(cursor))

    @router.get("/{policy_id}", response_model=AccessPolicyView)
    async def policy(
        organization_id: UUID, policy_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.get_policy(token, organization_id, policy_id))

    @router.post("", response_model=AccessPolicyView, status_code=201, responses=CONFLICT)
    async def create_policy(
        organization_id: UUID,
        body: AccessPolicyCreate,
        response: Response,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.create_policy(token, organization_id, body))

    @router.patch("/{policy_id}", response_model=AccessPolicyView, responses=CONFLICT)
    async def update_policy(
        organization_id: UUID,
        policy_id: UUID,
        body: AccessPolicyPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response, await service.update_policy(token, organization_id, policy_id, body, version)
        )

    return router
