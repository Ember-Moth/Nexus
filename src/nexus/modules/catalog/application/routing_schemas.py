from typing import Literal
from uuid import UUID

from pydantic import Field

from .schemas import Schema


class Candidate(Schema):
    deployment_id: UUID
    priority: int = Field(default=0, ge=0, le=10000, strict=True)
    weight: int = Field(default=1, ge=1, le=10000, strict=True)


class Route(Schema):
    organization_id: UUID
    model_id: UUID
    candidates: list[Candidate] = Field(min_length=1, max_length=256)


class Quota(Schema):
    layer: Literal["platform", "customer"] = "customer"
    scope_type: Literal["organization", "project", "key"] = "organization"
    scope_id: UUID | None = None
    metric: Literal["rpm", "tpm", "concurrency"]
    limit_value: int = Field(ge=1, le=2000000000, strict=True)


class RoutePolicyView(Schema):
    organization_id: UUID
    model_id: UUID
    candidates: list[Candidate]
    version: int


class QuotaPolicyResult(Schema):
    """The written policy; `scope_id` echoes the request and is null for organization scope."""

    layer: Literal["platform", "customer"]
    scope_type: Literal["organization", "project", "key"]
    scope_id: UUID | None
    metric: Literal["rpm", "tpm", "concurrency"]
    limit_value: int
    id: UUID
    version: int
