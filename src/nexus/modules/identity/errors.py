from uuid import UUID

from nexus.shared.errors import ServiceError


class IdentityError(ServiceError):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        *,
        resource_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(code, message, status)
        self.resource_id = resource_id
        self.reason = reason or code


def unauthorized() -> IdentityError:
    return IdentityError("unauthorized", "Invalid or expired credentials", 401)


def forbidden(resource_id: UUID | None = None) -> IdentityError:
    return IdentityError("forbidden", "Operation is not permitted", 403, resource_id=resource_id)


def missing(resource_id: UUID) -> IdentityError:
    return IdentityError("not_found", "Resource not found", 404, resource_id=resource_id)
