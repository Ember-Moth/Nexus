from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value, error_responses, idempotency_key

from ..application.resources import UpstreamResourceService
from ..application.schemas import (
    Page,
    PurgeRequestCreate,
    PurgeRequestView,
    ResourceKind,
    ResourceState,
    UpstreamResourceView,
)


def create_resource_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["upstream resources"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[UpstreamResourceService, Depends(get_service)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    @router.get(
        "/organizations/{organization_id}/upstream-resources",
        response_model=Page[UpstreamResourceView],
    )
    async def resources(
        organization_id: UUID,
        token: Token,
        service: Service,
        project_id: UUID | None = None,
        api_key_id: UUID | None = None,
        kind: ResourceKind | None = None,
        state: ResourceState | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        """Upstream copies held for the organization's API keys, revoked keys included,
        newest first. Developers see keys of their granted projects."""
        return await service.list(
            token,
            organization_id,
            project_id=project_id,
            api_key_id=api_key_id,
            kind=kind,
            state=state,
            limit=limit,
            cursor=cursor_value(cursor),
        )

    @router.get("/upstream-resources", response_model=Page[UpstreamResourceView])
    async def all_resources(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
        api_key_id: UUID | None = None,
        kind: ResourceKind | None = None,
        state: ResourceState | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        """Upstream copies across organizations, for platform administrators."""
        return await service.list(
            token,
            organization_id,
            project_id=project_id,
            api_key_id=api_key_id,
            kind=kind,
            state=state,
            limit=limit,
            cursor=cursor_value(cursor),
        )

    @router.post(
        "/organizations/{organization_id}/upstream-purges",
        status_code=202,
        response_model=PurgeRequestView,
        responses=error_responses(409),
    )
    async def purge(
        organization_id: UUID,
        body: PurgeRequestCreate,
        response: Response,
        token: Token,
        key: Annotated[str, Depends(idempotency_key)],
        service: Service,
    ):
        """Delete the upstream copies a key holds, or one of them, through the purge queue.

        Organization owners and platform administrators only. Poll the Location for progress.
        """
        result = await service.purge(token, organization_id, body, key)
        response.headers["Location"] = (
            f"/api/v1/organizations/{organization_id}/upstream-purges/{result['id']}"
        )
        return result

    @router.get(
        "/organizations/{organization_id}/upstream-purges/{purge_request_id}",
        response_model=PurgeRequestView,
    )
    async def purge_request(
        organization_id: UUID, purge_request_id: UUID, token: Token, service: Service
    ):
        return await service.purge_request(token, organization_id, purge_request_id)

    return router
