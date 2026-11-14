"""Public catalog operations and its bounded, read-only model lookup."""

from nexus.infrastructure.audit_reader import audit_reader
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .application.routing import RoutingPolicies as RoutingService
from .application.schemas import Visibility
from .application.service import CatalogService
from .persistence.repository import transaction
from .persistence.routing import RoutingRepository
from .persistence.snapshots import create_catalog_source

__all__ = [
    "create_audit_reader",
    "create_catalog_source",
    "RoutingPolicies",
    "routing_api",
    "CatalogService",
    "Visibility",
    "create_service",
    "create_model_lookup",
    "create_pricing_lookup",
    "api",
]


def __getattr__(name):
    """Load the public HTTP adapters only when the API process requests them."""
    if name == "routing_api":
        from .api import routing

        return routing
    raise AttributeError(name)


class RoutingPolicies(RoutingService):
    def __init__(self, database, access):
        super().__init__(RoutingRepository(database), access)


def create_model_lookup(database: ModuleDatabase):
    database.require_module(ModuleName.CATALOG)

    async def lookup(ids):
        async with transaction(database) as repo:
            return await repo.known_models(ids)

    return lookup


def create_service(database: ModuleDatabase, access, cipher) -> CatalogService:
    database.require_module(ModuleName.CATALOG)
    return CatalogService(lambda: transaction(database), access, cipher)


def create_pricing_lookup(database: ModuleDatabase, versions):
    database.require_module(ModuleName.CATALOG)

    async def lookup(kind, resource_id):
        async with transaction(database) as repo:
            if kind == "model":
                return await repo.model(resource_id, versions)
            return await repo.deployment(resource_id, versions)

    return lookup


def create_audit_reader(database):
    return audit_reader(database, ModuleName.CATALOG)
