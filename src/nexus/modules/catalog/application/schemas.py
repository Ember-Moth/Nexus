from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

type ProtocolFamily = Literal["openai_compatible", "anthropic", "gemini"]
type Status = Literal["active", "disabled", "archived"]
type Dimension = Literal[
    "input_uncached_tokens",
    "input_cache_read_tokens",
    "input_cache_write_5m_tokens",
    "input_cache_write_1h_tokens",
    "output_tokens",
]


@dataclass(frozen=True)
class Visibility:
    actor_id: UUID
    allowed_models: frozenset[UUID] | None


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Capabilities(Schema):
    streaming: bool = False
    tools: bool = False


class ModelCreate(Schema):
    public_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
    display_name: str = Field(min_length=1, max_length=200)
    protocols: list[ProtocolFamily] = Field(min_length=1, max_length=3)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    usage_schema: Literal["tokens.v1"] = "tokens.v1"
    token_dimensions: list[Dimension] = Field(
        default_factory=lambda: ["input_uncached_tokens", "output_tokens"],
        min_length=2,
        max_length=5,
    )
    max_input_tokens: int = Field(ge=1, le=1_000_000_000, strict=True)
    max_output_tokens: int = Field(ge=1, le=1_000_000_000, strict=True)

    @field_validator("display_name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @field_validator("protocols", "token_dimensions")
    @classmethod
    def unique_values(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate entries are not allowed")
        return sorted(values)

    @field_validator("token_dimensions")
    @classmethod
    def required_dimensions(cls, values):
        if not {"input_uncached_tokens", "output_tokens"} <= set(values):
            raise ValueError("input and output token dimensions are required")
        return values


class ModelPatch(Schema):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    capabilities: Capabilities | None = None
    protocols: list[ProtocolFamily] | None = Field(default=None, min_length=1, max_length=3)
    token_dimensions: list[Dimension] | None = Field(default=None, min_length=2, max_length=5)
    max_input_tokens: int | None = Field(default=None, ge=1, le=1_000_000_000, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000_000, strict=True)
    status: Status | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("supply non-null changes")
        return self


class ModelView(ModelCreate):
    id: UUID
    status: Status
    version: int
    created_at: datetime
    configured: bool


def endpoint(value: str) -> str:
    url = HttpUrl(value)
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
        raise ValueError("HTTPS endpoint without credentials, query or fragment is required")
    return str(url).rstrip("/")


class UpstreamCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    protocol_family: ProtocolFamily

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        return ModelCreate.nonblank(value)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        return endpoint(value)


class UpstreamPatch(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    status: Status | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("supply non-null changes")
        return self


class UpstreamView(UpstreamCreate):
    id: UUID
    status: Status
    version: int
    created_at: datetime


class CredentialCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    secret: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        return ModelCreate.nonblank(value)

    @field_validator("secret")
    @classmethod
    def valid_secret(cls, value):
        if any(not 33 <= ord(char) <= 126 for char in value.get_secret_value()):
            raise ValueError("secret must be a non-whitespace ASCII API key")
        return value


class CredentialView(Schema):
    id: UUID
    upstream_id: UUID
    name: str
    key_version: int
    status: Literal["active", "revoked"]
    revoked_at: datetime | None
    version: int
    created_at: datetime


class RelayProfile(Schema):
    convert_from: Literal["anthropic_messages", "openai_responses"] | None = None
    endpoint: Literal["anthropic_messages", "openai_responses"]
    transports: list[Literal["http", "websocket"]] = Field(min_length=1, max_length=2)
    streaming: bool = False
    tools: bool = False
    continuation: bool = False
    cancellation: bool = False
    count_rule: Literal["o200k_base.v1", "cl100k_base.v1", "deployment_ceiling.v1"]
    input_ceiling: int = Field(ge=1, le=1_000_000_000, strict=True)
    output_ceiling: int = Field(ge=1, le=1_000_000_000, strict=True)
    # Operator reference to evidence for these limits on this upstream model version.
    bound_reference: str = Field(min_length=1, max_length=200, pattern=r"\S")

    @model_validator(mode="after")
    def supported(self):
        ws = "websocket" in self.transports
        if self.convert_from is not None and (
            self.convert_from == self.endpoint or "http" not in self.transports
        ):
            raise ValueError("Conversion requires HTTP and the other native endpoint")
        if len(set(self.transports)) != len(self.transports):
            raise ValueError("duplicate transports")
        if self.endpoint == "anthropic_messages" and (
            ws or self.count_rule != "deployment_ceiling.v1"
        ):
            raise ValueError("unsupported Messages transport or counting rule")
        if not ws and (self.continuation or self.cancellation):
            raise ValueError("WebSocket controls require WebSocket transport")
        return self


class DeploymentCreate(Schema):
    relay_profile: RelayProfile | None = None
    name: str = Field(min_length=1, max_length=200)
    model_id: UUID
    upstream_id: UUID
    credential_id: UUID
    upstream_model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
    region: str = Field(default="global", min_length=1, max_length=100)
    status: Literal["active", "disabled"] = "active"

    @field_validator("name", "region")
    @classmethod
    def nonblank(cls, value):
        return ModelCreate.nonblank(value)


class DeploymentPatch(Schema):
    relay_profile: RelayProfile | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    credential_id: UUID | None = None
    upstream_model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$"
    )
    region: str | None = Field(default=None, min_length=1, max_length=100)
    status: Status | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set - {"relay_profile"}
        ):
            raise ValueError("supply non-null changes")
        return self


class DeploymentView(Schema):
    relay_profile: RelayProfile | None = None
    id: UUID
    name: str
    model_id: UUID
    upstream_id: UUID
    credential_id: UUID
    upstream_model: str
    region: str
    status: Status
    configured: bool
    version: int
    created_at: datetime


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None = None
