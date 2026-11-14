from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Scope:
    actor_id: UUID
    organization_id: UUID
    organization_status: str
    project_id: UUID | None = None
    key_id: UUID | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: UUID
    status: str
    configured: bool
    dimensions: frozenset[str]
    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class DeploymentInfo:
    id: UUID
    model_id: UUID
    configured: bool


class Access(Protocol):
    async def authorize(
        self,
        token: str,
        org_id: UUID | None = None,
        *,
        operation: str = "read",
        scope_type: str = "organization",
        scope_id: UUID | None = None,
        layer: str = "organization",
    ) -> Scope | UUID: ...
    async def visible_models(
        self, token: str, org_id: UUID, project_id: UUID | None = None, key_id: UUID | None = None
    ) -> frozenset[UUID] | None: ...


class Catalog(Protocol):
    async def model(self, model_id: UUID) -> ModelInfo | None: ...
    async def deployment(self, deployment_id: UUID) -> DeploymentInfo | None: ...


# Persistence implements its own queries; services depend only on the transaction factory.
type Transactions = Callable[[], AbstractAsyncContextManager]
