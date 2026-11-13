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

from .errors import IdentityError, unauthorized
from .schemas import (
    CurrentUser,
    Login,
    MemberCreate,
    MemberPatch,
    MemberView,
    OrganizationCreate,
    OrganizationPatch,
    OrganizationView,
    Page,
    PasswordSetup,
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
