"""Management HTTP API; authorization and transactions stay in identity services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

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
    MailPolicyView,
    MailSettings,
    MailTest,
    MailTestResult,
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
    SetupCompletion,
    SetupEmail,
    SetupIssued,
    SetupStatus,
    SiteInfo,
    SitePolicyView,
    TurnstilePolicyView,
    TurnstileSettings,
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
        response_model=VerificationPending,
        status_code=202,
        responses=business_errors(
            "registration_unavailable",
            "invalid_login",
            "invalid_password",
            "invalid_email",
            "mail_unavailable",
            challenge=True,
        ),
    )
    async def register(body: Registration, request: Request, service: Service):
        """Start a registration; the response does not reveal whether the login is taken."""
        return await service.register(body, peer(request))

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
            "registration_requires_email_verification",
            "version_conflict",
            "policy_managed_externally",
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
            "registration_unavailable",
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

    @router.get("/setup", response_model=SetupStatus)
    async def setup_status(token: Token, service: Service):
        """Whether the built-in administrator still has to complete the setup wizard.

        Open to every signed-in account, also while the rest of the API is locked.
        """
        return await service.setup_status(token)

    @router.post(
        "/setup/email-verification",
        status_code=202,
        response_model=RequestAccepted,
        responses=business_errors(
            "unauthorized",
            "forbidden",
            "setup_completed",
            "invalid_email",
            "email_reserved",
            "email_in_use",
            "mail_unavailable",
            "rate_limited",
            "authentication_unavailable",
        ),
    )
    async def setup_email(body: SetupEmail, token: Token, service: Service, request: Request):
        """Mail a token proving the built-in administrator's new address (wizard step 2)."""
        await service.request_setup_email(token, body.email, peer(request))
        return RequestAccepted()

    @router.post(
        "/setup",
        response_model=SetupStatus,
        responses=business_errors(
            "unauthorized",
            "forbidden",
            "setup_completed",
            "invalid_email",
            "email_reserved",
            "email_in_use",
            "invalid_password",
            "email_verification_required",
            "invalid_verification_token",
            "policy_managed_externally",
            "turnstile_in_use",
            "authentication_unavailable",
        ),
    )
    async def complete_setup(body: SetupCompletion, token: Token, service: Service):
        """Finish the wizard in one transaction; only the built-in administrator may call it.

        Stores the site details, the new address (verified when mail is configured) and the new
        password, marks setup complete and revokes every other session of the administrator.
        The calling session stays valid. A second submission answers 409 setup_completed.
        """
        return await service.complete_setup(token, body)

    @router.get("/settings/site", response_model=SitePolicyView)
    async def site_policy(service: Service, token: Token, response: Response):
        """Site name, public console address and contact email; platform administrators only."""
        result = await service.site_policy(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/site",
        response_model=SitePolicyView,
        responses=business_errors(
            "version_conflict", "policy_managed_externally", "turnstile_in_use"
        ),
    )
    async def update_site_policy(
        body: SiteInfo, service: Service, token: Token, version: Version, response: Response
    ):
        result = await service.update_site_policy(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/settings/mail", response_model=MailPolicyView)
    async def mail_policy(service: Service, token: Token, response: Response):
        """The identity mail provider without its secret; platform administrators only.

        Open while setup is pending, for the wizard's optional mail step.
        """
        result = await service.mail_policy(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/mail",
        response_model=MailPolicyView,
        responses=business_errors(
            "invalid_mail_settings",
            "mail_in_use",
            "credential_unavailable",
            "policy_managed_externally",
        ),
    )
    async def update_mail_policy(
        body: MailSettings, service: Service, token: Token, version: Version, response: Response
    ):
        """Replace the identity mail provider; omitted secrets keep their stored value.

        Removing the provider is refused with mail_in_use while email verification is on. API
        and Worker processes use the change within seconds, without a restart.
        """
        result = await service.update_mail_policy(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.post(
        "/settings/mail/test",
        response_model=MailTestResult,
        responses=business_errors(
            "invalid_email", "mail_unavailable", "rate_limited", "authentication_unavailable"
        ),
    )
    async def test_mail(body: MailTest, service: Service, token: Token, request: Request):
        """Send a test message with the stored settings and report the provider's answer.

        Counts against the identity mail budgets and is audited. A refusal or an unreachable
        provider is reported in the result, with the provider's error summarized.
        """
        return await service.send_test_mail(token, body.recipient, peer(request))

    @router.get("/settings/turnstile", response_model=TurnstilePolicyView)
    async def turnstile_policy(service: Service, token: Token, response: Response):
        """Turnstile keys without the secret; platform administrators only."""
        result = await service.turnstile_policy(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/turnstile",
        response_model=TurnstilePolicyView,
        responses=business_errors(
            "invalid_turnstile_settings",
            "invalid_turnstile_secret",
            "turnstile_in_use",
            "turnstile_unavailable",
            "credential_unavailable",
            "policy_managed_externally",
        ),
    )
    async def update_turnstile_policy(
        body: TurnstileSettings,
        service: Service,
        token: Token,
        version: Version,
        response: Response,
    ):
        """Replace the Turnstile keys; a new secret key is checked with Siteverify first.

        Removing the keys is refused with turnstile_in_use while Turnstile is on.
        """
        result = await service.update_turnstile_policy(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

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
