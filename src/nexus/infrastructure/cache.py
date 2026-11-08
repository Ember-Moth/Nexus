from redis.asyncio import Redis


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

    async def close(self) -> None:
        await self._client.aclose()
