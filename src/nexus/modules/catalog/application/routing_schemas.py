from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .schemas import Schema

# A quota policy is identified by (organization_id, layer, scope_type, scope_id, metric).
QuotaLayer = Literal["platform", "customer"]
QuotaScope = Literal["organization", "project", "key"]
QuotaMetric = Literal["rpm", "tpm", "concurrency"]
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
    layer: QuotaLayer = "customer"
    scope_type: QuotaScope = "organization"
    scope_id: UUID | None = None
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
    """The written policy. `scope_id` echoes the request, so the deprecated POST may leave it
    null for organization scope."""

    layer: QuotaLayer
    scope_type: QuotaScope
    scope_id: UUID | None
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
