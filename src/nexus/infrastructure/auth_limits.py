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


class RedisMailLimits:
    """Identity mail budgets; every message kind draws on the same recipient budgets.

    The per-recipient limit is counted per requester network, so a requester that exhausts it
    blocks only itself; the wider recipient total only bounds how much mail one mailbox can be
    sent when many networks take part.
    """

    def __init__(
        self,
        cache: Cache,
        key: bytes,
        *,
        per_recipient: int,
        per_recipient_total: int,
        per_account: int,
        per_ip: int,
        global_limit: int,
        window: int,
    ):
        self._cache = cache
        self._key = key
        self._limits = {
            "recipient": per_recipient,
            "recipient_total": per_recipient_total,
            "account": per_account,
            "peer": per_ip,
            "global": global_limit,
        }
        self._window = window

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    async def allow(self, recipient: str, peer: str, account: str | None) -> int:
        buckets = {
            "peer": peer,
            "recipient": f"{peer}\n{recipient}",
            "recipient_total": recipient,
            "global": "",
        }
        if account is not None:
            buckets["account"] = account
        return await self._cache.spend_budgets(
            [f"{kind}:{self._digest(kind + ':' + value)}" for kind, value in buckets.items()],
            [self._limits[kind] for kind in buckets],
            self._window,
        )
