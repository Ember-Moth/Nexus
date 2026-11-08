from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID


class Passwords(Protocol):
    def hash(self, password: str) -> str: ...


class Administrators(Protocol):
    async def lock_bootstrap(self) -> None: ...

    async def first_admin(self) -> tuple[UUID, str] | None: ...

    async def login_exists(self, login: str) -> bool: ...

    async def create(self, user_id: UUID, login: str, password_hash: str) -> None: ...


class Audit(Protocol):
    async def record_bootstrap(self, user_id: UUID) -> None: ...


class IdentityTransaction(Protocol):
    administrators: Administrators
    audit: Audit

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


type TransactionFactory = Callable[[], IdentityTransaction]
