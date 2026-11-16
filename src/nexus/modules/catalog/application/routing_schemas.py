from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from nexus.shared.views import ManagedSource

from .schemas import Schema

# A quota policy is identified by (organization_id, layer, scope_type, scope_id, metric).
QuotaLayer = Literal["platform", "customer"]
QuotaScope = Literal["organization", "project", "key"]
# Request rates and concurrency are enforced through Redis. Resource counts cap the live files,
# conversations, containers, batches (resources_*) and vector stores a key or an organization may
# hold in one cluster; they override the platform defaults for one organization only.
# admission_concurrency caps how many of an organization's requests may be inside the database
# admission (reserve + first attempt) at once across all nodes of a cluster; it overrides the
# Relay's NEXUS_RELAY_TENANT_ADMISSIONS for one organization.
QuotaMetric = Literal[
    "rpm",
    "tpm",
    "concurrency",
    "admission_concurrency",
    "resources_per_key",
    "resources_per_organization",
    "vector_stores_per_key",
    "vector_stores_per_organization",
]
COUNT_METRICS = frozenset(
    {
        "resources_per_key",
        "resources_per_organization",
        "vector_stores_per_key",
        "vector_stores_per_organization",
    }
)
# Metrics that only exist as platform policies of a whole organization.
ORGANIZATION_METRICS = COUNT_METRICS | {"admission_concurrency"}
QuotaValue = Annotated[int, Field(ge=1, le=2000000000, strict=True)]


class Candidate(Schema):
    deployment_id: UUID
    priority: int = Field(default=0, ge=0, le=10000, strict=True)
    weight: int = Field(default=1, ge=1, le=10000, strict=True)


Candidates = Annotated[list[Candidate], Field(min_length=1, max_length=256)]


class Route(Schema):
    """A route policy is identified by (organization_id, model_id)."""

    organization_id: UUID
    model_id: UUID
    candidates: Candidates


class RouteCandidates(Schema):
    candidates: Candidates


class Quota(Schema):
    layer: QuotaLayer
    scope_type: QuotaScope
    scope_id: UUID
    metric: QuotaMetric
    limit_value: QuotaValue


class QuotaLimit(Schema):
    limit_value: QuotaValue


class RoutePolicyView(Schema):
    organization_id: UUID
    model_id: UUID
    candidates: list[Candidate]
    version: int


class QuotaPolicyResult(Schema):
    """The written policy."""

    layer: QuotaLayer
    scope_type: QuotaScope
    scope_id: UUID
    metric: QuotaMetric
    limit_value: int
    id: UUID
    version: int


class QuotaPolicyView(Schema):
    id: UUID
    organization_id: UUID
    layer: QuotaLayer
    scope_type: QuotaScope
    scope_id: UUID
    metric: QuotaMetric
    limit_value: int
    version: int


class ResourceQuotaDefaults(Schema):
    """Platform defaults of the resource counts; organizations override them with quota policies."""

    resources_per_key: QuotaValue
    resources_per_organization: QuotaValue
    vector_stores_per_key: QuotaValue
    vector_stores_per_organization: QuotaValue


class ResourceQuotaDefaultsView(ResourceQuotaDefaults):
    managed: ManagedSource | None = Field(
        description="The policy file that locked these defaults; writes are refused while set"
    )
    version: int
