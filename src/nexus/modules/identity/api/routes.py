"""Management HTTP API; authorization and transactions stay in identity services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from nexus.http import (
    auth_limit_peer,
    business_errors,
    credentials,
    cursor_value,
    error_responses,
    expected_version,
)

from ..application.schemas import (
    AuthPolicy,
    AuthPolicyView,
    CurrentUser,
    EmailBinding,
    EmailProof,
    EmailRequest,
    InvitationAccept,
    InvitationCreate,
    InvitationIssued,
    InvitationView,
    Login,
    MemberCreate,
    MemberListItem,
    MemberPatch,
    MemberView,
    OrganizationCreate,
    OrganizationPatch,
    OrganizationView,
    Page,
    PasswordSetup,
    PlatformRolesPatch,
    PlatformRolesView,
    PublicAuthOptions,
    Registration,
    RegistrationCreated,
    RequestAccepted,
    SessionCreated,
    SetupIssued,
    UserCreate,
    UserView,
    VerificationPending,
)
from ..application.service import IdentityService

CONFLICT = error_responses(409)


def create_router(
    get_service, *, auth_proxy_peers=frozenset(), auth_proxy_secret=None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["identity"])

    def peer(request: Request) -> str:
        return auth_limit_peer(request, auth_proxy_peers, auth_proxy_secret)

    Service = Annotated[IdentityService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    @router.post(
        "/auth/registrations",
        response_model=RegistrationCreated,
        status_code=201,
        responses={
            **business_errors(
                "login_in_use",
                "invalid_login",
                "invalid_password",
                "invalid_email",
                "mail_unavailable",
                "conflict",
                challenge=True,
            ),
            202: {"model": VerificationPending},
        },
    )
    async def register(body: Registration, request: Request, service: Service):
        result = await service.register(body, peer(request))
        if isinstance(result, VerificationPending):
            return JSONResponse(status_code=202, content=result.model_dump())
        return result

    @router.post(
        "/auth/sessions",
        response_model=SessionCreated,
        status_code=201,
        responses=business_errors("unauthorized", "invalid_login", challenge=True),
    )
    async def login(body: Login, request: Request, service: Service):
        return await service.login(
            body.login,
            body.password.get_secret_value(),
            peer(request),
            body.turnstile_token,
        )

    @router.delete("/auth/sessions/current", status_code=204)
    async def logout(token: Token, service: Service):
        await service.logout(token)

    @router.post(
        "/auth/password",
        status_code=204,
        responses=business_errors("unauthorized", "invalid_password", challenge=True),
    )
    async def set_password(body: PasswordSetup, request: Request, service: Service):
        await service.set_password(
            body.token.get_secret_value(),
            body.password.get_secret_value(),
            peer(request),
            body.turnstile_token,
        )

    @router.get("/auth/options", response_model=PublicAuthOptions)
    async def auth_options(service: Service):
        return await service.public_options()

    @router.get("/settings/authentication", response_model=AuthPolicyView)
    async def auth_policy(service: Service, token: Token, response: Response):
        result = await service.policy(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/authentication",
        response_model=AuthPolicyView,
        responses=business_errors(
            "unauthorized",
            "forbidden",
            "turnstile_not_configured",
            "version_conflict",
            "mail_unavailable",
            "precondition_required",
            "invalid_version",
        ),
    )
    async def update_policy(
        body: AuthPolicy, service: Service, token: Token, version: Version, response: Response
    ):
        result = await service.update_policy(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.post(
        "/auth/registrations/verify",
        response_model=RegistrationCreated,
        status_code=201,
        responses=business_errors(
            "unauthorized",
            "invalid_password",
            "login_in_use",
            "email_in_use",
            "conflict",
            challenge=True,
        ),
    )
    async def verify_registration(body: PasswordSetup, service: Service, request: Request):
        return await service.confirm_registration(body, peer(request))

    @router.post(
        "/me/email",
        status_code=202,
        response_model=RequestAccepted,
        responses=business_errors(
            "unauthorized", "invalid_email", "mail_unavailable", challenge=True
        ),
    )
    async def bind_email(body: EmailBinding, token: Token, service: Service, request: Request):
        await service.request_binding(token, body, peer(request))
        return RequestAccepted()

    @router.post(
        "/me/email/verify",
        status_code=204,
        responses=business_errors("unauthorized", "email_in_use", "conflict", challenge=True),
    )
    async def verify_email(body: EmailProof, token: Token, service: Service, request: Request):
        await service.confirm_binding(token, body, peer(request))
        return Response(status_code=204)

    @router.post(
        "/auth/password/forgot",
        status_code=202,
        response_model=RequestAccepted,
        responses=business_errors("invalid_email", "mail_unavailable", challenge=True),
    )
    async def forgot(body: EmailRequest, service: Service, request: Request):
        await service.forgot(body, peer(request))
        return RequestAccepted()

    @router.post(
        "/auth/password/reset",
        status_code=204,
        responses=business_errors("unauthorized", "invalid_password", challenge=True),
    )
    async def reset(body: PasswordSetup, service: Service, request: Request):
        await service.reset(body, peer(request))
        return Response(status_code=204)

    @router.get("/me", response_model=CurrentUser)
    async def me(token: Token, service: Service):
        return await service.me(token)

    @router.post("/users", response_model=SetupIssued, status_code=201, responses=CONFLICT)
    async def create_user(body: UserCreate, token: Token, service: Service):
        return await service.create_user(token, body.login)

    @router.get("/users", response_model=Page[UserView])
    async def users(token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None):
        return await service.list_users(token, limit, cursor_value(cursor))

    @router.post(
        "/users/{user_id}/password-reset-tokens",
        response_model=SetupIssued,
        status_code=201,
        responses=CONFLICT,
    )
    async def setup(user_id: UUID, token: Token, service: Service):
        return await service.issue_password_setup(token, user_id)

    @router.post(
        "/organizations", response_model=OrganizationView, status_code=201, responses=CONFLICT
    )
    async def create_organization(
        body: OrganizationCreate, response: Response, token: Token, service: Service
    ):
        result = await service.create_organization(token, body)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/users/{user_id}/platform-roles", response_model=PlatformRolesView)
    async def platform_roles(user_id: UUID, response: Response, token: Token, service: Service):
        result = await service.platform_roles(token, user_id)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.patch(
        "/users/{user_id}/platform-roles", response_model=PlatformRolesView, responses=CONFLICT
    )
    async def update_platform_roles(
        user_id: UUID,
        body: PlatformRolesPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        result = await service.update_platform_roles(token, user_id, body, version)
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

    @router.patch(
        "/organizations/{organization_id}",
        response_model=OrganizationView,
        responses=business_errors(
            "organization_archived", "personal_organization_required", "invalid_transition"
        ),
    )
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
        "/organizations/{organization_id}/invitations",
        response_model=InvitationIssued,
        status_code=201,
        responses=CONFLICT,
    )
    async def invite(organization_id: UUID, token: Token, body: InvitationCreate, service: Service):
        return await service.invite(token, organization_id, body)

    @router.get("/organizations/{organization_id}/invitations", response_model=Page[InvitationView])
    async def invitations(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_invitations(token, organization_id, limit, cursor_value(cursor))

    @router.delete("/organizations/{organization_id}/invitations/{invitation_id}", status_code=204)
    async def revoke_invitation(
        organization_id: UUID, invitation_id: UUID, token: Token, service: Service
    ):
        await service.revoke_invitation(token, organization_id, invitation_id)
        return Response(status_code=204)

    @router.post(
        "/invitations/accept",
        response_model=MemberView,
        responses=business_errors(
            "invitation_unavailable",
            "invitation_expired",
            "invitation_consumed",
            "organization_inactive",
            "conflict",
        ),
    )
    async def accept_invitation(token: Token, body: InvitationAccept, service: Service):
        return await service.accept_invitation(token, body)

    @router.post(
        "/organizations/{organization_id}/members",
        response_model=MemberView,
        status_code=201,
        responses=CONFLICT,
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

    @router.get("/organizations/{organization_id}/members", response_model=Page[MemberListItem])
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

    @router.patch(
        "/organizations/{organization_id}/members/{member_id}",
        response_model=MemberView,
        responses=CONFLICT,
    )
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

    @router.delete(
        "/organizations/{organization_id}/members/{member_id}", status_code=204, responses=CONFLICT
    )
    async def remove_member(
        organization_id: UUID, member_id: UUID, version: Version, token: Token, service: Service
    ):
        await service.update_member(
            token, organization_id, member_id, MemberPatch(status="disabled"), version
        )

    return router
