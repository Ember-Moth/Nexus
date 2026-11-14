import base64
import json
from uuid import uuid4

from redis.asyncio import Redis

from .gateway_cache import PREPARE, PUBLISH


class Cache:
    def __init__(self, url: str, *, timeout: float, namespace: str) -> None:
        self._client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            max_connections=10,
        )
        self._namespace = namespace

    async def check(self) -> None:
        await self._client.ping()

    async def allow_auth_attempt(
        self, subject: str, peer: str, account_limit: int, ip_limit: int, window: int
    ) -> bool:
        result = await self._client.eval(
            "local a = tonumber(redis.call('GET', KEYS[1]) or '0'); "
            "local b = tonumber(redis.call('GET', KEYS[2]) or '0'); "
            "if a >= tonumber(ARGV[1]) or b >= tonumber(ARGV[2]) then return 0 end; "
            "if redis.call('INCR', KEYS[1]) == 1 then redis.call('EXPIRE', KEYS[1], ARGV[3]) end; "
            "if redis.call('INCR', KEYS[2]) == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end; "
            "return 1",
            2,
            f"{self._namespace}:{{auth}}:account:{subject}",
            f"{self._namespace}:{{auth}}:peer:{peer}",
            account_limit,
            ip_limit,
            window,
        )
        return result == 1

    async def write_heartbeat(self, worker_id: str, payload: str, ttl: int) -> None:
        await self._client.set(f"{self._namespace}:workers:{worker_id}", payload, ex=ttl)

    async def read_heartbeat(self, worker_id: str) -> str | None:
        return await self._client.get(f"{self._namespace}:workers:{worker_id}")

    async def remove_heartbeat(self, worker_id: str, instance_id: str) -> None:
        # An older process must not delete its replacement's heartbeat during shutdown.
        await self._client.eval(
            "local raw = redis.call('GET', KEYS[1]); "
            "if not raw then return 0 end; "
            "local ok, value = pcall(cjson.decode, raw); "
            "if ok and value['instance_id'] == ARGV[1] then "
            "return redis.call('DEL', KEYS[1]) end; return 0",
            1,
            f"{self._namespace}:workers:{worker_id}",
            instance_id,
        )

    async def prepare_limits(self, org, first):
        await self._client.eval(
            PREPARE, 1, f"{self._namespace}:limit:{{{org}}}:epoch", int(first), str(uuid4())
        )

    async def publish_config(self, cluster, revision, raw, digest):
        prefix = f"{self._namespace}:cfg:{{{cluster}}}"
        value = json.dumps(
            {"sha256": digest, "bytes": base64.b64encode(raw).decode()}, separators=(",", ":")
        )
        await self._client.eval(
            PUBLISH,
            2,
            prefix + ":snapshot:" + revision,
            prefix + ":current",
            revision,
            value,
            f"{self._namespace}:cfg:{cluster}:changed",
        )

    async def close(self) -> None:
        await self._client.aclose()
