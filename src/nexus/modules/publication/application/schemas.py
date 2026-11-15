import base64
from typing import Annotated, Literal
from uuid import UUID

from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
# listed: organization_ids; all: every active organization; personal: every active personal one.
OrganizationScope = Literal["listed", "all", "personal"]
ORGANIZATION_SCOPE_DESCRIPTION = (
    "listed publishes organization_ids; all and personal resolve every active (personal) "
    "organization at each release and require organization_ids to be empty"
)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def check_scope(scope, organization_ids):
    if scope == "listed" and not organization_ids:
        raise ValueError("A listed organization scope requires organization_ids")
    if scope != "listed" and organization_ids:
        raise ValueError("A rule-based organization scope takes no organization_ids")


class Cluster(Command):
    name: str = Field(min_length=1, max_length=160)
    region: str = Field(min_length=1, max_length=100)
    organization_scope: OrganizationScope = Field(
        default="listed", description=ORGANIZATION_SCOPE_DESCRIPTION
    )
    organization_ids: list[UUID] = Field(default_factory=list, max_length=256)
    deployment_ids: list[UUID] = Field(min_length=1, max_length=1024)
    public_key: str
    auto_publish: bool = Field(
        default=True, description="Publish source changes without a manual release"
    )

    @model_validator(mode="after")
    def valid_scope(self):
        check_scope(self.organization_scope, self.organization_ids)
        return self

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
    organization_scope: OrganizationScope | None = Field(
        default=None, description=ORGANIZATION_SCOPE_DESCRIPTION
    )
    organization_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=256)
    deployment_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=1024)
    auto_publish: bool | None = Field(
        default=None,
        description="true also resumes automatic publication paused by a rollback",
    )

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value):
        return Cluster.valid_key(value) if value is not None else None


class Page[T](View):
    items: list[T]
    next_cursor: str | None


class AutoPublishRelease(View):
    revision: Revision
    at: Timestamp
    trigger: dict[str, list[str | None]] = Field(
        description="Source vector entries that changed, as [previous, current]; "
        "list-valued entries are shown as short digests"
    )


class AutoPublishFailure(View):
    code: str
    message: str
    at: Timestamp


class AutoPublishPause(View):
    reason: Literal["rollback"]
    revision: Revision
    at: Timestamp


class AutoPublishStatus(View):
    state: Literal["disabled", "paused", "pending", "failing", "current", "unknown"] = Field(
        description="unknown until the auto-publisher first checks an enabled cluster"
    )
    pending_since: Timestamp | None = Field(
        description="First unpublished source change; the release waits for a quiet window"
    )
    checked_at: Timestamp | None
    last_published: AutoPublishRelease | None
    last_failure: AutoPublishFailure | None
    paused: AutoPublishPause | None


class ClusterView(View):
    id: UUID
    name: str
    region: str
    organization_scope: OrganizationScope
    organization_ids: list[UUID]
    deployment_ids: list[UUID]
    public_key: str = Field(description="Base64 Curve25519 public key")
    key_version: int
    active: bool
    auto_publish: bool
    auto_publish_status: AutoPublishStatus
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


class PrincipalView(NodeStatus):
    database_role: str
    capabilities: list[str]
    applied_at: Timestamp | None


class ReleaseRecord(View):
    revision: Revision
    sha256: str
    created_at: Timestamp
    actor_id: UUID | None = Field(description="Platform administrator who requested it, if any")
    initiator: Literal["operator", "auto_publisher"] = Field(
        description="operator: requested through the release endpoint; "
        "auto_publisher: published by the worker after a source change"
    )
    rollback_revision: Revision | None = Field(
        description="Historical revision whose resources this release republished"
    )
    distribution: ReleaseDelivery


class ReleaseStatus(View):
    revision: Revision
    sha256: str
    desired_revision: Revision
    distributed_revision: Revision
    distribution: ReleaseDelivery
    nodes: list[NodeStatus]


ResourceKind = Literal["response", "file", "conversation", "vector_store", "container", "batch"]
# The handle prefix Relay shows the key holder for each kind; the rest is the handle ID.
HANDLE_PREFIXES: dict[str, str] = {
    "response": "resp_nx_",
    "file": "file_nx_",
    "conversation": "conv_nx_",
    "vector_store": "vs_nx_",
    "container": "cntr_nx_",
    "batch": "msgbatch_nx_",
}
PurgeState = Literal["pending", "purged", "failed"]
ResourceState = Literal["retained", "pending", "purged", "failed", "deleted"]
RESOURCE_STATE_DESCRIPTION = (
    "retained: held upstream with no purge queued; pending: a purge is queued or retrying; "
    "purged: the purge deleted the upstream copy; failed: the purge gave up after its last "
    "attempt and the copy may remain; deleted: the key holder deleted it without a purge"
)


class ResourcePurge(View):
    id: UUID
    state: PurgeState
    reason: Literal["key_inactive", "orphan", "owner_request"]
    attempts: int
    last_error: str | None = Field(description="Error code of the last failed attempt")
    created_at: Timestamp
    next_attempt_at: Timestamp | None = Field(
        description="When a pending purge is next attempted; null once it has finished"
    )
    finished_at: Timestamp | None


class UpstreamResourceView(View):
    id: UUID = Field(
        description="Handle ID; for an upstream copy whose handle was never saved, the purge ID"
    )
    handle: str | None = Field(
        description="The ID Relay returns to the key holder; null when no handle was saved"
    )
    kind: ResourceKind
    state: ResourceState = Field(description=RESOURCE_STATE_DESCRIPTION)
    organization_id: UUID | None
    project_id: UUID | None
    api_key_id: UUID
    cluster_id: UUID
    deployment_id: UUID
    created_at: Timestamp
    purge: ResourcePurge | None


class PurgeRequestCreate(Command):
    """Delete every upstream copy the key holds, or the one given by kind and resource_id."""

    api_key_id: UUID
    kind: ResourceKind | None = None
    resource_id: UUID | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.kind is None) != (self.resource_id is None):
            raise ValueError("kind and resource_id go together")
        return self


class PurgeRequestView(View):
    id: UUID
    organization_id: UUID
    api_key_id: UUID
    kind: ResourceKind | None
    resource_id: UUID | None
    requested_by: UUID
    created_at: Timestamp
    state: PurgeState = Field(
        description="pending while any covered purge is pending; then failed if any gave up, "
        "otherwise purged"
    )
    total: int = Field(description="Purges the request covers")
    pending: int
    purged: int
    failed: int
