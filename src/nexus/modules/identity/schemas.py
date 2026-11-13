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


class Login(Schema):
    login: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)


class UserCreate(Schema):
    login: str = Field(min_length=3, max_length=254)


class PasswordSetup(Schema):
    token: SecretStr = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=16, max_length=1024)


class UserView(Schema):
    id: UUID
    login: str
    status: str
    created_at: datetime


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


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None = None


class CurrentUser(Schema):
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
