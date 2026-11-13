import hashlib
import hmac

from .cache import Cache


class RedisAuthLimits:
    def __init__(self, cache: Cache, key: bytes, *, account_limit: int, ip_limit: int, window: int):
        self._cache = cache
        self._key = key
        self._account_limit = account_limit
        self._ip_limit = ip_limit
        self._window = window

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    async def allow(self, subject: str, peer: str) -> bool:
        return await self._cache.allow_auth_attempt(
            self._digest("account:" + subject),
            self._digest("peer:" + peer),
            self._account_limit,
            self._ip_limit,
            self._window,
        )
