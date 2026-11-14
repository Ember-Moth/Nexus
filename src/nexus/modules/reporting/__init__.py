from nexus.infrastructure.audit_reader import audit_reader
from nexus.shared.storage import ModuleName

from .application.service import Reports
from .persistence.repository import transaction

__all__ = ["create_service", "api"]


def create_service(database, access, source, audits, settings):
    database.require_module(ModuleName.REPORTING)
    return Reports(
        lambda: transaction(database),
        access,
        source,
        [*audits, audit_reader(database, ModuleName.REPORTING)],
        ttl=settings.export_ttl_seconds,
        rate=settings.export_per_org_minute,
        max_rows=settings.export_max_rows,
    )
