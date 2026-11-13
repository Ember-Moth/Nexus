"""Public catalog operations and its bounded, read-only model lookup."""

from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .repository import transaction
from .schemas import Visibility
from .service import CatalogService

__all__ = ["CatalogService", "Visibility", "create_service", "create_model_lookup", "api"]


def create_model_lookup(database: ModuleDatabase):
    database.require_module(ModuleName.CATALOG)

    async def lookup(ids):
        async with transaction(database) as repo:
            return await repo.known_models(ids)

    return lookup


def create_service(database: ModuleDatabase, access, cipher) -> CatalogService:
    database.require_module(ModuleName.CATALOG)
    return CatalogService(lambda: transaction(database), access, cipher)
