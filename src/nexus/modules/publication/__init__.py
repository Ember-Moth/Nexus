from nexus.shared.storage import ModuleName

from .repository import transaction
from .service import PublicationService

__all__ = ["create_service", "api"]


def create_service(database, administrator, identity, catalog, prices, cache):
    database.require_module(ModuleName.PUBLICATION)
    return PublicationService(
        lambda: transaction(database), administrator, identity, catalog, prices, cache
    )
