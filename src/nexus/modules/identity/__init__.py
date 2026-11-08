"""Identity's public API; storage and implementation stay inside this package."""

from nexus.infrastructure.passwords import Argon2Passwords
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .repository import IdentityUnitOfWork
from .schemas import BootstrapResult
from .service import IdentityService

__all__ = ["BootstrapResult", "IdentityService", "create_service"]


def create_service(database: ModuleDatabase) -> IdentityService:
    database.require_module(ModuleName.IDENTITY)
    return IdentityService(lambda: IdentityUnitOfWork(database), Argon2Passwords())
