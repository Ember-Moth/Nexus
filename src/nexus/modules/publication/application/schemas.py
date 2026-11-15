import base64
from typing import Annotated
from uuid import UUID

from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexus.shared.views import Timestamp, View

CAPABILITIES = ("reservation.v1", "usage-events.v1")
# Node capability a release requires once any deployment enables the provider feature.
FEATURE_CAPABILITIES = {
    "stored_responses": "relay.stored-responses.v1",
    "background": "relay.background-responses.v1",
    "conversations": "relay.conversations.v1",
    "files": "relay.files.v1",
    "vector_stores": "relay.vector-stores.v1",
    "batches": "relay.message-batches.v1",
    "code_execution": "relay.code-execution.v1",
    "remote_mcp": "relay.remote-mcp.v1",
    "web_fetch": "relay.web-fetch.v1",
    "web_search": "relay.web-search.v1",
}
# Revisions are bigint in SQL and decimal strings in JSON to survive any client's integers.
Revision = Annotated[str, Field(pattern=r"^[0-9]+$")]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cluster(Command):
    name: str = Field(min_length=1, max_length=160)
    region: str = Field(min_length=1, max_length=100)
    organization_ids: list[UUID] = Field(min_length=1, max_length=256)
    deployment_ids: list[UUID] = Field(min_length=1, max_length=1024)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value):
        try:
            key = PublicKey(base64.b64decode(value, validate=True))
            SealedBox(key).encrypt(b"")
        except ValueError, CryptoError:
            raise ValueError("Expected a valid base64 Curve25519 public key") from None
        return value


class Principal(Command):
    database_role: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    node_id: UUID
    capabilities: list[str] = Field(default_factory=lambda: list(CAPABILITIES), max_length=32)
    active: bool = True


class Release(Command):
    rollback_revision: int | None = Field(default=None, gt=0)


class ClusterUpdate(Command):
    active: bool | None = None
    public_key: str | None = None
    key_grace_seconds: int = Field(default=3600, ge=0, le=86400)
    organization_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=256)
    deployment_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value):
        return Cluster.valid_key(value) if value is not None else None


class Page[T](View):
    items: list[T]
    next_cursor: str | None


class ClusterView(View):
    id: UUID
    name: str
    region: str
    organization_ids: list[UUID]
    deployment_ids: list[UUID]
    public_key: str = Field(description="Base64 Curve25519 public key")
    key_version: int
    active: bool
    desired_revision: Revision
    distributed_revision: Revision
    version: int
    scope_generation: int


class ReleaseAccepted(View):
    revision: Revision
    sha256: str


class ReleaseDelivery(View):
    attempts: int
    delivered_at: Timestamp | None


class NodeAck(View):
    revision: Revision
    applied: bool
    error_code: str | None


class NodeStatus(View):
    node_id: UUID
    active: bool
    applied_revision: Revision
    last_ack: NodeAck | None


class ReleaseStatus(View):
    revision: Revision
    sha256: str
    desired_revision: Revision
    distributed_revision: Revision
    distribution: ReleaseDelivery
    nodes: list[NodeStatus]
