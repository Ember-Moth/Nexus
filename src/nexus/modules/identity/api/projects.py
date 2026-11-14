"""Management HTTP API; authorization and transactions stay in identity services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value, error_responses, expected_version, idempotency_key

from ..application.projects import ProjectService
from ..application.schemas import (
    GrantCreate,
    GrantPatch,
    GrantView,
    KeyCreate,
    KeyIssued,
    KeyRotate,
    KeyView,
    Page,
    ProjectCreate,
    ProjectPatch,
    ProjectView,
    ServiceAccountCreate,
    ServiceAccountPatch,
    ServiceAccountView,
)

CONFLICT = error_responses(409)


def create_project_router(get_service) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/organizations/{organization_id}", tags=["projects and credentials"]
    )
    Service = Annotated[ProjectService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Idempotency = Annotated[str, Depends(idempotency_key)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def tagged(response, resource):
        response.headers["ETag"] = f'"{resource.version}"'
        return resource

    def issued(response, result):
        response.status_code = 201 if result.secret_status == "available" else 200
        response.headers["ETag"] = f'"{result.key.version}"'
        return result

    @router.post("/projects", response_model=ProjectView, status_code=201, responses=CONFLICT)
    async def create_project(
        organization_id: UUID,
        body: ProjectCreate,
        response: Response,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.create_project(token, organization_id, body))

    @router.get("/projects", response_model=Page[ProjectView])
    async def projects(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_projects(token, organization_id, limit, cursor_value(cursor))

    @router.get("/projects/{project_id}", response_model=ProjectView)
    async def project(
        organization_id: UUID, project_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.get_project(token, organization_id, project_id))

    @router.patch("/projects/{project_id}", response_model=ProjectView, responses=CONFLICT)
    async def update_project(
        organization_id: UUID,
        project_id: UUID,
        body: ProjectPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response,
            await service.update_project(token, organization_id, project_id, body, version),
        )

    @router.post("/project-grants", response_model=GrantView, status_code=201, responses=CONFLICT)
    async def create_grant(
        organization_id: UUID, body: GrantCreate, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.create_grant(token, organization_id, body))

    @router.get("/project-grants", response_model=Page[GrantView])
    async def grants(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_grants(token, organization_id, limit, cursor_value(cursor))

    @router.get("/project-grants/{grant_id}", response_model=GrantView)
    async def grant(
        organization_id: UUID, grant_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.get_grant(token, organization_id, grant_id))

    @router.patch("/project-grants/{grant_id}", response_model=GrantView, responses=CONFLICT)
    async def update_grant(
        organization_id: UUID,
        grant_id: UUID,
        body: GrantPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response, await service.update_grant(token, organization_id, grant_id, body, version)
        )

    @router.delete("/project-grants/{grant_id}", status_code=204, responses=CONFLICT)
    async def revoke_grant(
        organization_id: UUID, grant_id: UUID, version: Version, token: Token, service: Service
    ):
        await service.update_grant(
            token, organization_id, grant_id, GrantPatch(status="revoked"), version
        )

    @router.post(
        "/service-accounts", response_model=ServiceAccountView, status_code=201, responses=CONFLICT
    )
    async def create_account(
        organization_id: UUID,
        body: ServiceAccountCreate,
        response: Response,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.create_service_account(token, organization_id, body))

    @router.get("/service-accounts", response_model=Page[ServiceAccountView])
    async def accounts(
        organization_id: UUID,
        token: Token,
        service: Service,
        project_id: UUID | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_service_accounts(
            token, organization_id, project_id, limit, cursor_value(cursor)
        )

    @router.get("/service-accounts/{account_id}", response_model=ServiceAccountView)
    async def account(
        organization_id: UUID, account_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(
            response, await service.get_service_account(token, organization_id, account_id)
        )

    @router.patch(
        "/service-accounts/{account_id}", response_model=ServiceAccountView, responses=CONFLICT
    )
    async def update_account(
        organization_id: UUID,
        account_id: UUID,
        body: ServiceAccountPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response,
            await service.update_service_account(token, organization_id, account_id, body, version),
        )

    @router.post("/api-keys", response_model=KeyIssued, status_code=201)
    async def create_key(
        organization_id: UUID,
        body: KeyCreate,
        response: Response,
        idem: Idempotency,
        token: Token,
        service: Service,
    ):
        return issued(response, await service.create_key(token, organization_id, body, idem))

    @router.get("/api-keys", response_model=Page[KeyView])
    async def keys(
        organization_id: UUID,
        token: Token,
        service: Service,
        project_id: UUID | None = None,
        service_account_id: UUID | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_keys(
            token, organization_id, project_id, service_account_id, limit, cursor_value(cursor)
        )

    @router.get("/api-keys/{key_id}", response_model=KeyView)
    async def key(
        organization_id: UUID, key_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.get_key(token, organization_id, key_id))

    @router.post("/api-keys/{key_id}/revoke", response_model=KeyView, responses=CONFLICT)
    async def revoke_key(
        organization_id: UUID,
        key_id: UUID,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.revoke_key(token, organization_id, key_id, version))

    @router.post("/api-keys/{key_id}/rotate", response_model=KeyIssued, status_code=201)
    async def rotate_key(
        organization_id: UUID,
        key_id: UUID,
        body: KeyRotate,
        response: Response,
        version: Version,
        idem: Idempotency,
        token: Token,
        service: Service,
    ):
        return issued(
            response, await service.rotate_key(token, organization_id, key_id, body, version, idem)
        )

    return router
