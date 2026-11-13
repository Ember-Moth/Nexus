"""Identity's public API; storage and implementation stay inside this package."""

from nexus.infrastructure.passwords import Argon2Passwords
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .applications import ProjectService
from .events import ORGANIZATION_CREATED
from .ports import AuthLimits
from .repository import IdentityUnitOfWork
from .schemas import BootstrapResult
from .service import IdentityService

# The HTTP adapter submodule is loaded only by the API entry point.
__all__ = [
    "BootstrapResult",
    "IdentityService",
    "ProjectService",
    "create_service",
    "create_project_service",
    "api",
    "ORGANIZATION_CREATED",
]


def create_service(
    database: ModuleDatabase, auth_limits: AuthLimits, *, session_ttl: int, setup_ttl: int
) -> IdentityService:
    database.require_module(ModuleName.IDENTITY)
    return IdentityService(
        lambda: IdentityUnitOfWork(database),
        Argon2Passwords(),
        auth_limits,
        session_ttl=session_ttl,
        setup_ttl=setup_ttl,
    )


def create_project_service(database: ModuleDatabase) -> ProjectService:
    database.require_module(ModuleName.IDENTITY)
    return ProjectService(lambda: IdentityUnitOfWork(database))
