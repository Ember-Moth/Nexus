"""Identity's public API; storage and implementation stay inside this package."""

from nexus.infrastructure.passwords import Argon2Passwords
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .applications import ProjectService
from .events import ORGANIZATION_CREATED, ORGANIZATION_CREATED_VERSION
from .model_access import ModelAccessService
from .ports import AuthLimits
from .repository import IdentityUnitOfWork
from .schemas import BootstrapResult
from .service import IdentityService

# The HTTP adapter submodule is loaded only by the API entry point.
from .snapshot_repository import create_identity_source

__all__ = [
    "create_identity_source",
    "BootstrapResult",
    "IdentityService",
    "ProjectService",
    "ModelAccessService",
    "create_service",
    "create_project_service",
    "create_model_access_service",
    "api",
    "ORGANIZATION_CREATED",
    "ORGANIZATION_CREATED_VERSION",
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


def create_model_access_service(database: ModuleDatabase, models) -> ModelAccessService:
    database.require_module(ModuleName.IDENTITY)
    return ModelAccessService(lambda: IdentityUnitOfWork(database), models)
