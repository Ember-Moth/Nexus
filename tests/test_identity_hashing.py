import asyncio
import threading

import pytest

from nexus.modules.identity.application.service import IdentityService
from nexus.modules.identity.domain.errors import IdentityError


class BlockingPasswords:
    """Argon2 stand-in whose hashing blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Semaphore(0)

    def hash(self, password):
        self.started.release()
        self.release.wait(5)
        return "hash"

    def verify(self, password_hash, password):
        return password == "correct"


def service(passwords, **options):
    return IdentityService(
        None,
        passwords,
        None,
        login_window=60,
        session_ttl=60,
        setup_ttl=60,
        providers=None,
        mail_cipher=None,
        mail_limits=None,
        **options,
    )


async def test_registration_hashing_cannot_starve_sign_in():
    passwords = BlockingPasswords()
    identity = service(passwords, login_hash_slots=2, account_hash_slots=2, hash_queue_timeout=0.2)
    password = "registration-password"
    # A burst of sign-ups fills every account slot...
    hashing = [asyncio.create_task(identity._hash(password)) for _ in range(2)]
    for _ in range(2):
        await asyncio.to_thread(passwords.started.acquire)
    try:
        # ...yet sign-in verification still gets a thread at once,
        assert await asyncio.wait_for(identity._verify("stored", "correct", login=True), 1)
        # and further sign-ups give up after the queue timeout instead of piling up.
        with pytest.raises(IdentityError) as busy:
            await identity._hash(password)
        assert (busy.value.code, busy.value.status, busy.value.retry_after) == (
            "authentication_unavailable",
            503,
            1,
        )
    finally:
        passwords.release.set()
    assert await asyncio.gather(*hashing) == ["hash", "hash"]


async def test_cancelled_requests_keep_their_slot_until_hashing_ends():
    passwords = BlockingPasswords()
    identity = service(passwords, account_hash_slots=1, hash_queue_timeout=0.2)
    first = asyncio.create_task(identity._hash("registration-password"))
    await asyncio.to_thread(passwords.started.acquire)
    first.cancel()
    await asyncio.sleep(0)
    try:
        # The thread is still hashing, so the slot is still taken.
        with pytest.raises(IdentityError):
            await identity._hash("registration-password")
    finally:
        passwords.release.set()
    await asyncio.sleep(0.05)
    passwords.release.set()
    assert await identity._hash("registration-password") == "hash"
