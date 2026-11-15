from typing import Protocol, TypedDict
from uuid import UUID


class ScopedOrganization(TypedDict):
    id: UUID
    personal: bool


class OrganizationSource(Protocol):
    """Every active organization; rule-based cluster scopes select from these."""

    async def __call__(self) -> list[ScopedOrganization]: ...
