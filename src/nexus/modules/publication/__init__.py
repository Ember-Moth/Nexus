from nexus.infrastructure.audit_reader import audit_reader
from nexus.shared.storage import ModuleName

from .application.automation import AutoPublisher
from .application.ports import KeyOwner, ResourceScope
from .application.resources import UpstreamResourceService
from .application.service import PublicationService
from .persistence import resources
from .persistence.repository import transaction

__all__ = [
    "SOURCE_CHANNEL",
    "create_audit_reader",
    "create_auto_publisher",
    "create_resource_service",
    "create_service",
    "KeyOwner",
    "ResourceScope",
    "UpstreamResourceService",
    "api",
]
# PostgreSQL notification channel announcing a changed publication source revision.
SOURCE_CHANNEL = "nexus_source_revision"


def create_service(database, administrator, identity, catalog, prices, cache, organizations):
    database.require_module(ModuleName.PUBLICATION)
    return PublicationService(
        lambda: transaction(database),
        administrator,
        identity,
        catalog,
        prices,
        cache,
        organizations,
    )


def create_auto_publisher(database, service, notifications, *, poll):
    """notifications(seconds) must wait on SOURCE_CHANNEL, returning early when notified.

    Each cluster's quiet window and maximum delay come from its cluster policy.
    """
    database.require_module(ModuleName.PUBLICATION)
    return AutoPublisher(service, lambda: transaction(database), notifications, poll=poll)


def create_resource_service(database, access):
    """access implements application.ports.ResourceAccess with identity's role checks."""
    database.require_module(ModuleName.PUBLICATION)
    return UpstreamResourceService(lambda: resources.transaction(database), access)


def create_audit_reader(database):
    return audit_reader(database, ModuleName.PUBLICATION)
