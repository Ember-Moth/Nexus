"""Identity's public API; storage and implementation stay inside this package."""

from nexus.infrastructure.audit_reader import audit_reader
from nexus.infrastructure.passwords import Argon2Passwords
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .application.model_access import ModelAccessService
from .application.ports import AuthLimits, MailLimits
from .application.projects import ProjectService
from .application.service import IdentityService
from .domain.events import ORGANIZATION_CREATED, ORGANIZATION_CREATED_VERSION
from .persistence.organizations import create_organization_source
from .persistence.repository import IdentityUnitOfWork
from .persistence.snapshots import create_identity_source
from .providers.authentication import AuthProviders

__all__ = [
    "create_audit_reader",
    "create_identity_source",
    "create_organization_source",
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
    database: ModuleDatabase,
    auth_limits: AuthLimits,
    *,
    settings,
    mail_cipher,
    mail_limits: MailLimits,
) -> IdentityService:
    database.require_module(ModuleName.IDENTITY)
    return IdentityService(
        lambda: IdentityUnitOfWork(database),
        Argon2Passwords(),
        auth_limits,
        providers=AuthProviders(settings),
        mail_cipher=mail_cipher,
        mail_limits=mail_limits,
        login_hash_slots=settings.login_hash_concurrency,
        account_hash_slots=settings.account_hash_concurrency,
        hash_queue_timeout=settings.hash_queue_timeout_seconds,
    )


def create_project_service(database: ModuleDatabase) -> ProjectService:
    database.require_module(ModuleName.IDENTITY)
    return ProjectService(lambda: IdentityUnitOfWork(database))


def create_model_access_service(database: ModuleDatabase, models) -> ModelAccessService:
    database.require_module(ModuleName.IDENTITY)
    return ModelAccessService(lambda: IdentityUnitOfWork(database), models)


def create_audit_reader(database):
    return audit_reader(database, ModuleName.IDENTITY)
