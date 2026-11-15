import hashlib
import hmac

from .cache import Cache


class RedisAuthLimits:
    """Attempt limits per subject and per peer; the limits are passed with every attempt."""

    def __init__(self, cache: Cache, key: bytes):
        self._cache = cache
        self._key = key

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    async def allow(
        self, subject: str, peer: str, *, account_limit: int, ip_limit: int, window: int
    ) -> bool:
        return await self._cache.allow_auth_attempt(
            self._digest("account:" + subject),
            self._digest("peer:" + peer),
            account_limit,
            ip_limit,
            window,
        )


class RedisMailLimits:
    """Identity mail budgets; every message kind draws on the same recipient budgets.

    The per-recipient limit is counted per requester network, so a requester that exhausts it
    blocks only itself; the wider recipient total only bounds how much mail one mailbox can be
    sent when many networks take part. ``budgets`` carries the current policy values
    (``per_recipient``, ``per_recipient_total``, ``per_account``, ``per_ip``, ``global_limit``,
    ``window_seconds``).
    """

    def __init__(self, cache: Cache, key: bytes):
        self._cache = cache
        self._key = key

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    async def allow(self, recipient: str, peer: str, account: str | None, budgets) -> int:
        limits = {
            "recipient": budgets.per_recipient,
            "recipient_total": budgets.per_recipient_total,
            "account": budgets.per_account,
            "peer": budgets.per_ip,
            "global": budgets.global_limit,
        }
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
            [limits[kind] for kind in buckets],
            budgets.window_seconds,
        )
