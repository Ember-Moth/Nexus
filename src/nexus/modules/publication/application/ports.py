from dataclasses import dataclass
from typing import Protocol, TypedDict
from uuid import UUID


class ScopedOrganization(TypedDict):
    id: UUID
    personal: bool


class OrganizationSource(Protocol):
    """Every active organization; rule-based cluster scopes select from these."""

    async def __call__(self) -> list[ScopedOrganization]: ...


@dataclass(frozen=True)
class KeyOwner:
    organization_id: UUID
    project_id: UUID


@dataclass(frozen=True)
class ResourceScope:
    """The caller and the API keys whose upstream resources it may see or delete.

    keys is None for a platform administrator listing across organizations.
    """

    actor_id: UUID
    keys: dict[UUID, KeyOwner] | None


class ResourceAccess(Protocol):
    """Identity's organization role checks for upstream resources held by API keys."""

    async def scope(
        self,
        token: str,
        organization_id: UUID | None,
        *,
        project_id: UUID | None = None,
        api_key_id: UUID | None = None,
        manage: bool = False,
    ) -> ResourceScope: ...

    async def owners(self, key_ids: set[UUID]) -> dict[UUID, KeyOwner]: ...
