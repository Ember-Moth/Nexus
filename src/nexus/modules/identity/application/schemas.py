import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from nexus.shared.views import ManagedSource

from ..domain.email import mailbox
from ..domain.errors import IdentityError

type MemberRole = Literal["org_owner", "org_developer", "org_finance"]
type OrganizationStatus = Literal["provisioning", "active", "suspended", "archived"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChallengeInput(Schema):
    turnstile_token: SecretStr | None = Field(default=None, min_length=1, max_length=2048)


class Login(ChallengeInput):
    login: str = Field(
        min_length=3, max_length=254, description="Login name, or the account's email address"
    )
    password: SecretStr = Field(min_length=1, max_length=1024)


class Registration(ChallengeInput):
    email: str | None = Field(default=None, min_length=3, max_length=254)
    login: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=16, max_length=1024)


class EmailRequest(ChallengeInput):
    email: str = Field(min_length=3, max_length=254)


class EmailBinding(EmailRequest):
    password: SecretStr = Field(min_length=1, max_length=1024)


class EmailProof(ChallengeInput):
    token: SecretStr = Field(min_length=1, max_length=128)


class RequestAccepted(Schema):
    accepted: Literal[True] = True


class VerificationPending(Schema):
    email_verification_required: Literal[True] = True


class AuthSwitches(Schema):
    registration: bool = Field(
        default=False,
        description="Self-service registration; requires email_verification. Omitted means closed.",
    )
    email_verification: bool
    turnstile: bool


class SignInLimits(Schema):
    """Attempts per window for one account and for one client address (IPv4 or IPv6 /64)."""

    window_seconds: int = Field(ge=1, le=3600)
    per_account: int = Field(ge=1, le=1000)
    per_ip: int = Field(ge=1, le=10000)


class MailBudgets(Schema):
    """Identity mail per window, shared by registration, mailbox binding and recovery."""

    window_seconds: int = Field(ge=60, le=86400)
    per_recipient: int = Field(
        ge=1, le=1000, description="One recipient, per requester IPv4 address or IPv6 /64"
    )
    per_recipient_total: int = Field(ge=1, le=10000, description="One recipient, all requesters")
    per_account: int = Field(
        ge=1, le=10000, description="One registering login or one signed-in account"
    )
    per_ip: int = Field(ge=1, le=100000, description="One requester, all recipients")
    global_limit: int = Field(ge=1, le=10_000_000)
    queue_ttl_seconds: int = Field(
        ge=60,
        le=86400,
        description="Unsent mail is dropped after this; tokens last 15 minutes from sending",
    )

    @model_validator(mode="after")
    def recipient_total(self):
        if self.per_recipient > self.per_recipient_total:
            raise ValueError("per_recipient must not exceed per_recipient_total")
        return self


class CredentialLifetimes(Schema):
    session_ttl_seconds: int = Field(ge=60, le=86400)
    password_setup_ttl_seconds: int = Field(ge=60, le=86400)


def normalize_logins(values: list[str]) -> list[str]:
    """Reserved logins are compared after the same folding as new logins."""
    folded = {unicodedata.normalize("NFKC", value).strip().lower() for value in values}
    if "" in folded:
        raise ValueError("reserved logins must not be blank")
    return sorted(folded)


# Plain aliases keep these out of the named OpenAPI components.
ReservedLogin = Annotated[str, Field(min_length=1, max_length=254)]
ReservedLogins = Annotated[list[ReservedLogin], Field(max_length=1000)]


class AuthPolicy(AuthSwitches):
    """The complete authentication policy; a PUT replaces all of it."""

    sign_in: SignInLimits
    mail: MailBudgets
    sessions: CredentialLifetimes
    reserved_logins: ReservedLogins = Field(
        description="Logins that self-service registration may not claim"
    )
    platform_email_recovery: bool = Field(
        description="Whether holders of a platform role may recover access by email"
    )

    @field_validator("reserved_logins")
    @classmethod
    def fold_reserved(cls, values):
        return normalize_logins(values)


class AuthPolicyView(AuthPolicy):
    registration: bool
    managed: ManagedSource | None = Field(
        description="The policy file that locked this policy; writes are refused while set"
    )
    version: int


class PublicAuthOptions(AuthSwitches):
    registration: bool
    turnstile_site_key: str | None
    email_delivery_available: bool


class UserCreate(Schema):
    login: str = Field(min_length=3, max_length=254)


class PasswordSetup(ChallengeInput):
    token: SecretStr = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=16, max_length=1024)


class UserView(Schema):
    id: UUID
    login: str
    status: str
    created_at: datetime
    version: int


class SessionCreated(Schema):
    access_token: str = Field(repr=False)
    token_type: str = "bearer"
    expires_at: datetime


class SetupIssued(Schema):
    user: UserView
    setup_token: str = Field(repr=False)
    expires_at: datetime


class OrganizationCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    owner_user_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()


class OrganizationPatch(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: OrganizationStatus | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("supply at least one non-null change")
        if self.name is not None and not self.name.strip():
            raise ValueError("name cannot be blank")
        return self


class OrganizationView(Schema):
    personal_owner_id: UUID | None
    id: UUID
    name: str
    status: OrganizationStatus
    version: int
    created_at: datetime


class MemberCreate(Schema):
    user_id: UUID
    role: MemberRole


class MemberPatch(Schema):
    role: MemberRole | None = None
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("supply at least one non-null change")
        return self


class MemberView(Schema):
    id: UUID
    organization_id: UUID
    user_id: UUID
    role: MemberRole
    status: str
    version: int
    created_at: datetime


class MemberListItem(MemberView):
    login: str


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None = None


class InvitationCreate(Schema):
    login: str = Field(min_length=3, max_length=254)
    role: MemberRole


class InvitationAccept(Schema):
    token: SecretStr = Field(min_length=1, max_length=128)


class InvitationView(Schema):
    id: UUID
    organization_id: UUID
    login: str
    role: MemberRole
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    accepted_at: datetime | None


class InvitationIssued(InvitationView):
    token: str = Field(repr=False)


class RegistrationCreated(SessionCreated):
    personal_organization_id: UUID


class CurrentUser(Schema):
    verified_email: str | None = None
    unverified_email: str | None = Field(
        default=None,
        description="An address the account signs in with but has not proven; "
        "it cannot recover the account",
    )
    personal_organization_id: UUID
    user: UserView
    platform_roles: list[str]
    memberships: list[MemberView]


class NamedResource(Schema):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()


class ProjectCreate(NamedResource):
    pass


class ProjectPatch(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled", "archived"] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("supply at least one non-null change")
        if self.name is not None and not self.name.strip():
            raise ValueError("name cannot be blank")
        return self


class ProjectView(Schema):
    id: UUID
    organization_id: UUID
    name: str
    status: str
    version: int
    created_at: datetime


class GrantCreate(Schema):
    project_id: UUID
    membership_id: UUID


class GrantPatch(Schema):
    status: Literal["active", "revoked"]


class GrantView(Schema):
    id: UUID
    organization_id: UUID
    project_id: UUID
    membership_id: UUID
    status: str
    version: int
    created_at: datetime


class ServiceAccountCreate(NamedResource):
    project_id: UUID


class ServiceAccountPatch(ProjectPatch):
    status: Literal["active", "disabled"] | None = None


class ServiceAccountView(ProjectView):
    project_id: UUID


class KeyCreate(NamedResource):
    service_account_id: UUID
    expires_at: AwareDatetime | None = None

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        try:
            return value.astimezone(UTC) if value else None
        except OverflowError:
            raise ValueError("expiry is outside the supported range") from None


class KeyRotate(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: AwareDatetime | None = None
    grace_seconds: int = Field(default=0, ge=0, le=3600, strict=True)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip() if value else None

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        try:
            return value.astimezone(UTC) if value else None
        except OverflowError:
            raise ValueError("expiry is outside the supported range") from None


class KeyView(Schema):
    id: UUID
    organization_id: UUID
    project_id: UUID
    service_account_id: UUID
    name: str
    prefix: str
    expires_at: datetime | None
    revoked_at: datetime | None
    rotated_at: datetime | None
    rotated_from_id: UUID | None
    version: int
    created_at: datetime


class KeyIssued(Schema):
    key: KeyView
    secret: str | None = Field(default=None, repr=False)
    secret_status: Literal["available", "secret_unavailable"]


class ApplicationIdentity(Schema):
    organization_id: UUID
    project_id: UUID
    service_account_id: UUID
    key_id: UUID


class AccessPolicyCreate(Schema):
    layer: Literal["platform", "customer"]
    scope_type: Literal["organization", "project", "key"]
    scope_id: UUID | None = None
    allowed_models: list[UUID] = Field(max_length=1000)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.scope_type != "organization" and self.scope_id is None:
            raise ValueError("scope_id is required")
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("duplicate models are not allowed")
        return self


class AccessPolicyPatch(Schema):
    allowed_models: list[UUID] = Field(max_length=1000)

    @field_validator("allowed_models")
    @classmethod
    def unique_models(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate models are not allowed")
        return values


class AccessPolicyView(Schema):
    id: UUID
    organization_id: UUID
    layer: str
    scope_type: str
    scope_id: UUID
    project_id: UUID | None
    api_key_id: UUID | None
    allowed_models: list[UUID]
    version: int
    created_at: datetime


@dataclass(frozen=True)
class ModelVisibility:
    actor_id: UUID
    allowed_models: frozenset[UUID] | None


class PlatformRolesPatch(Schema):
    roles: list[Literal["platform_admin", "platform_finance"]] = Field(max_length=2)

    @field_validator("roles")
    @classmethod
    def distinct(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate roles are not allowed")
        return sorted(values)


class PlatformRolesView(Schema):
    user_id: UUID
    version: int
    roles: list[str]


@dataclass(frozen=True)
class OrganizationCreated:
    event_id: UUID
    organization_id: UUID
    schema_version: int


@dataclass(frozen=True)
class FinancialAccess:
    actor_id: UUID
    organization_id: UUID
    organization_status: str
    project_id: UUID | None = None
    key_id: UUID | None = None


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def console_address(value: str) -> str:
    """The console's public address: HTTPS, or plain HTTP on a loopback host."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"https", "http"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or not char.isprintable() for char in value)
        ):
            raise ValueError
        _ = parsed.port  # raises ValueError on an invalid port
    except ValueError:
        raise ValueError(
            "console URL must be an HTTP(S) URL without credentials, query or fragment"
        ) from None
    # Session cookies are Secure, which browsers accept on HTTPS and on loopback hosts only.
    if parsed.scheme == "http" and not (host in LOOPBACK_HOSTS or host.endswith(".localhost")):
        raise ValueError("console URL must use HTTPS outside loopback hosts")
    return value.rstrip("/")


class SiteInfo(Schema):
    """How the deployment names itself and where people reach it."""

    site_name: str = Field(
        min_length=1, max_length=100, description="Shown in the console and in identity mail"
    )
    console_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Public address of the management console; HTTPS except on loopback hosts. "
        "Payment checkouts return to it.",
    )
    contact_email: str | None = Field(
        default=None, max_length=254, description="Where users reach the operators"
    )

    @field_validator("site_name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("site name cannot be blank")
        if any(unicodedata.category(char)[0] == "C" for char in value):
            raise ValueError("site name cannot contain control characters")
        return value

    @field_validator("console_url")
    @classmethod
    def console(cls, value: str | None) -> str | None:
        return console_address(value) if value is not None else None

    @field_validator("contact_email")
    @classmethod
    def contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return mailbox(value)
        except IdentityError:
            raise ValueError("contact email must be a valid address") from None


class SitePolicyView(SiteInfo):
    console_url: str | None
    contact_email: str | None
    setup_completed_at: datetime | None = Field(
        description="When the built-in administrator completed the setup wizard; "
        "until then the management API is locked"
    )
    managed: ManagedSource | None = Field(
        description="The policy file that locked these values; writes are refused while set"
    )
    version: int


class SetupStatus(Schema):
    required: bool = Field(
        description="The setup wizard has not been completed; other management calls "
        "answer 409 setup_required"
    )
    builtin_admin: bool = Field(
        description="The caller is the built-in administrator, the only account that can "
        "complete the wizard"
    )
    email_delivery_available: bool = Field(
        description="Mail is configured: the new administrator address must be verified"
    )
    site: SitePolicyView


class SetupEmail(Schema):
    email: str = Field(min_length=3, max_length=254)


class SetupCompletion(Schema):
    site: SiteInfo = Field(
        description="Site details; while the site policy is managed by a policy file they must "
        "equal the managed values"
    )
    email: str = Field(
        min_length=3,
        max_length=254,
        description="The administrator's new address; not the default or an example domain",
    )
    email_token: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Token mailed by POST /setup/email-verification; required when email "
        "delivery is available, ignored otherwise (the address is then stored unverified)",
    )
    password: SecretStr = Field(min_length=16, max_length=1024)
