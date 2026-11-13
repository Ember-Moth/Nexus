"""Management HTTP API; authorization and transactions stay in identity services."""

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .applications import ProjectService
from .errors import IdentityError, unauthorized
from .schemas import (
    CurrentUser,
    GrantCreate,
    GrantPatch,
    GrantView,
    KeyCreate,
    KeyIssued,
    KeyRotate,
    KeyView,
    Login,
    MemberCreate,
    MemberPatch,
    MemberView,
    OrganizationCreate,
    OrganizationPatch,
    OrganizationView,
    Page,
    PasswordSetup,
    ProjectCreate,
    ProjectPatch,
    ProjectView,
    ServiceAccountCreate,
    ServiceAccountPatch,
    ServiceAccountView,
    SessionCreated,
    SetupIssued,
    UserCreate,
    UserView,
)
from .service import IdentityService

bearer = HTTPBearer(auto_error=False)


def credentials(
    request: Request, value: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    if value is None or len(request.headers.getlist("authorization")) != 1:
        raise unauthorized()
    return value.credentials


def expected_version(if_match: Annotated[str | None, Header()] = None) -> int:
    if if_match is None:
        raise IdentityError("precondition_required", "If-Match is required", 428)
    if not re.fullmatch(r'"[1-9][0-9]{0,9}"', if_match):
        raise IdentityError(
            "invalid_version", "If-Match must contain a quoted resource version", 422
        )
    return int(if_match[1:-1])


def cursor_value(cursor: str | None):
    if cursor is None:
        return None
    try:
        values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(isinstance(value, str) for value in values)
        ):
            raise ValueError
        at = datetime.fromisoformat(values[0])
        if at.tzinfo is None:
            raise ValueError
        return at, UUID(values[1])
    except ValueError, TypeError, binascii.Error:
        raise IdentityError("invalid_cursor", "Invalid pagination cursor", 422) from None


def create_router(get_service) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["identity"])
    Service = Annotated[IdentityService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    @router.post("/auth/sessions", response_model=SessionCreated, status_code=201)
    async def login(body: Login, request: Request, service: Service):
        return await service.login(
            body.login,
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
        )

    @router.delete("/auth/sessions/current", status_code=204)
    async def logout(token: Token, service: Service):
        await service.logout(token)

    @router.post("/auth/password", status_code=204)
    async def set_password(body: PasswordSetup, request: Request, service: Service):
        await service.set_password(
            body.token.get_secret_value(),
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
        )

    @router.get("/me", response_model=CurrentUser)
    async def me(token: Token, service: Service):
        return await service.me(token)

    @router.post("/users", response_model=SetupIssued, status_code=201)
    async def create_user(body: UserCreate, token: Token, service: Service):
        return await service.create_user(token, body.login)

    @router.get("/users", response_model=Page[UserView])
    async def users(token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None):
        return await service.list_users(token, limit, cursor_value(cursor))

    @router.post(
        "/users/{user_id}/password-reset-tokens", response_model=SetupIssued, status_code=201
    )
    async def setup(user_id: UUID, token: Token, service: Service):
        return await service.issue_password_setup(token, user_id)

    @router.post("/organizations", response_model=OrganizationView, status_code=201)
    async def create_organization(
        body: OrganizationCreate, response: Response, token: Token, service: Service
    ):
        result = await service.create_organization(token, body)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/organizations", response_model=Page[OrganizationView])
    async def organizations(
        token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None
    ):
        return await service.list_organizations(token, limit, cursor_value(cursor))

    @router.get("/organizations/{organization_id}", response_model=OrganizationView)
    async def organization(
        organization_id: UUID, response: Response, token: Token, service: Service
    ):
        result = await service.get_organization(token, organization_id)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.patch("/organizations/{organization_id}", response_model=OrganizationView)
    async def update_organization(
        organization_id: UUID,
        body: OrganizationPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        result = await service.update_organization(token, organization_id, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.post(
        "/organizations/{organization_id}/members", response_model=MemberView, status_code=201
    )
    async def add_member(
        organization_id: UUID,
        body: MemberCreate,
        response: Response,
        token: Token,
        service: Service,
    ):
        result = await service.add_member(token, organization_id, body)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/organizations/{organization_id}/members", response_model=Page[MemberView])
    async def members(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_members(token, organization_id, limit, cursor_value(cursor))

    @router.get("/organizations/{organization_id}/members/{member_id}", response_model=MemberView)
    async def member(
        organization_id: UUID, member_id: UUID, response: Response, token: Token, service: Service
    ):
        result = await service.get_member(token, organization_id, member_id)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.patch("/organizations/{organization_id}/members/{member_id}", response_model=MemberView)
    async def update_member(
        organization_id: UUID,
        member_id: UUID,
        body: MemberPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        result = await service.update_member(token, organization_id, member_id, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.delete("/organizations/{organization_id}/members/{member_id}", status_code=204)
    async def remove_member(
        organization_id: UUID, member_id: UUID, version: Version, token: Token, service: Service
    ):
        await service.update_member(
            token, organization_id, member_id, MemberPatch(status="disabled"), version
        )

    return router


def idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if value is None:
        raise IdentityError("precondition_required", "Idempotency-Key is required", 428)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise IdentityError("invalid_idempotency_key", "Invalid Idempotency-Key", 422)
    return value


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

    @router.post("/projects", response_model=ProjectView, status_code=201)
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

    @router.patch("/projects/{project_id}", response_model=ProjectView)
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

    @router.post("/project-grants", response_model=GrantView, status_code=201)
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

    @router.patch("/project-grants/{grant_id}", response_model=GrantView)
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

    @router.delete("/project-grants/{grant_id}", status_code=204)
    async def revoke_grant(
        organization_id: UUID, grant_id: UUID, version: Version, token: Token, service: Service
    ):
        await service.update_grant(
            token, organization_id, grant_id, GrantPatch(status="revoked"), version
        )

    @router.post("/service-accounts", response_model=ServiceAccountView, status_code=201)
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

    @router.patch("/service-accounts/{account_id}", response_model=ServiceAccountView)
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

    @router.post("/api-keys/{key_id}/revoke", response_model=KeyView)
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
