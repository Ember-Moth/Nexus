from nexus.infrastructure.audit_reader import audit_reader
from nexus.shared.storage import ModuleName

from .repository import transaction
from .service import PublicationService

__all__ = ["create_audit_reader", "create_service", "api"]


def create_service(database, administrator, identity, catalog, prices, cache):
    database.require_module(ModuleName.PUBLICATION)
    return PublicationService(
        lambda: transaction(database), administrator, identity, catalog, prices, cache
    )


def create_audit_reader(database):
    return audit_reader(database, ModuleName.PUBLICATION)
