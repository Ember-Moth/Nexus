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
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code, message, status, retry_after=retry_after)
        self.resource_id = resource_id
        self.reason = reason or code


def unauthorized() -> IdentityError:
    return IdentityError("unauthorized", "Invalid or expired credentials", 401)


def forbidden(resource_id: UUID | None = None) -> IdentityError:
    return IdentityError("forbidden", "Operation is not permitted", 403, resource_id=resource_id)


def missing(resource_id: UUID) -> IdentityError:
    return IdentityError("not_found", "Resource not found", 404, resource_id=resource_id)


class MailRejected(Exception):
    """The provider refused this message for good; sending it again cannot succeed."""

    def __init__(self, status: int | str | None, detail: str | None = None) -> None:
        super().__init__(detail or "message rejected by the mail provider")
        self.status = status


def registration_unavailable() -> IdentityError:
    return IdentityError(
        "registration_unavailable", "Self-service registration is not available", 403
    )
