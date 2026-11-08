import asyncio
from uuid import uuid4

from nexus.shared.errors import BootstrapRejected

from .ports import Passwords, TransactionFactory
from .schemas import BootstrapResult


class IdentityService:
    def __init__(self, transactions: TransactionFactory, passwords: Passwords) -> None:
        self._transactions = transactions
        self._passwords = passwords

    async def bootstrap_admin(self, login: str, password: str) -> BootstrapResult:
        login = login.strip().lower()
        if not 3 <= len(login) <= 254 or any(char.isspace() for char in login):
            raise BootstrapRejected("login must contain 3-254 non-whitespace characters")
        if not 16 <= len(password) <= 1024:
            raise BootstrapRejected("administrator password must contain 16-1024 characters")
        # CPU work is performed outside both the event loop and the database transaction.
        password_hash = await asyncio.to_thread(self._passwords.hash, password)
        async with self._transactions() as tx:
            await tx.administrators.lock_bootstrap()
            existing = await tx.administrators.first_admin()
            if existing is not None:
                if existing[1] != login:
                    raise BootstrapRejected("an administrator already exists")
                return BootstrapResult(existing[0], created=False)
            if await tx.administrators.login_exists(login):
                raise BootstrapRejected("existing users cannot be promoted by bootstrap")
            user_id = uuid4()
            await tx.administrators.create(user_id, login, password_hash)
            await tx.audit.record_bootstrap(user_id)
            return BootstrapResult(user_id, created=True)
