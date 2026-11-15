from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
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


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    user_id: UUID
    created: bool


type MemberRole = Literal["org_owner", "org_developer", "org_finance"]
type OrganizationStatus = Literal["provisioning", "active", "suspended", "archived"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChallengeInput(Schema):
    turnstile_token: SecretStr | None = Field(default=None, min_length=1, max_length=2048)


class Login(ChallengeInput):
    login: str = Field(min_length=3, max_length=254)
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


class AuthPolicy(Schema):
    registration: bool = Field(
        default=False,
        description="Self-service registration; requires email_verification. Omitted means closed.",
    )
    email_verification: bool
    turnstile: bool


class AuthPolicyView(AuthPolicy):
    registration: bool
    version: int


class PublicAuthOptions(AuthPolicy):
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
