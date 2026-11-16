from nexus.infrastructure.audit_reader import audit_reader
from nexus.shared.storage import ModuleName

from .application.service import Reports
from .persistence.repository import transaction

__all__ = ["create_service", "api"]


def create_service(database, access, source, audits, administrator, files):
    """``administrator`` resolves a session to a platform administrator or refuses it.

    ``files`` is the object store that holds export files (``put``, ``delete_prefix`` and
    ``download_url`` of ``nexus.infrastructure.object_storage.ObjectStorage``).
    """
    database.require_module(ModuleName.REPORTING)
    return Reports(
        lambda: transaction(database),
        access,
        source,
        [*audits, audit_reader(database, ModuleName.REPORTING)],
        administrator,
        files,
    )
