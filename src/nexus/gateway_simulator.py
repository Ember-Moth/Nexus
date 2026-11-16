"""Async protocol peer for contract testing with a provisioned gateway login."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

import psycopg
from nacl.public import PrivateKey, SealedBox
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .infrastructure.gateway_cache import GatewayLimits


class GatewayOutcomeUnknown(RuntimeError):
    """The caller can replay the original arguments after reconnecting."""

    def __init__(self, operation, args):
        super().__init__(f"Commit outcome requires verification for {operation}")
        self.operation = operation
        self.retry_args = args


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def key_set_digest(keys):
    """The digest a release names for its key set (docs/plane-protocol.md)."""
    digest = hashlib.sha256()
    for k in sorted(keys, key=lambda k: k["id"]):
        expires = k["expires_at"]
        micros = (
            (datetime.fromisoformat(expires) - EPOCH) // timedelta(microseconds=1)
            if expires
            else "-"
        )
        fields = (
            k["id"],
            k["digest"],
            k["organization_id"],
            k["project_id"],
            k["service_account_id"],
            micros,
        )
        digest.update(("\t".join(map(str, fields)) + "\n").encode())
    return digest.hexdigest()


class GatewaySimulator:
    def __init__(self, connection, redis, namespace="nexus:v1", reconnect=None):
        if not connection.autocommit:
            raise ValueError("Gateway transport requires an idle autocommit connection")
        self.connection = connection
        self.reconnect = reconnect
        self.redis = redis
        self.namespace = namespace
        self.lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.snapshot = None
        self.snapshot_sha256 = None
        # The cluster's keys by secret digest, at self.key_sequence.
        self.keys = {}
        self.key_sequence = None
        self.limits = GatewayLimits(redis, namespace)

    @classmethod
    @asynccontextmanager
    async def connect(cls, dsn, redis_url, namespace="nexus:v1"):
        connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        redis = Redis.from_url(redis_url, decode_responses=True)

        async def reconnect():
            return await psycopg.AsyncConnection.connect(dsn, autocommit=True)

        peer = cls(connection, redis, namespace, reconnect)
        try:
            yield peer
        finally:
            await peer.connection.close()
            await redis.aclose()

    async def call(self, operation, *args):
        allowed = {
            "config_head",
            "config_keys",
            "get_config_snapshot",
            "get_credential_envelope",
            "ack_config",
            "reserve_request",
            "start_attempt",
            "get_request_status",
            "append_usage_event",
            "cancel_unstarted_request",
        }
        if operation not in allowed:
            raise ValueError("Unknown gateway operation")
        async with self.lock:
            for attempt in range(2):
                try:
                    return await self._call_once(operation, args)
                except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                    if error.sqlstate and not error.sqlstate.startswith("08"):
                        raise
                    if attempt or self.reconnect is None:
                        raise GatewayOutcomeUnknown(operation, args) from error
                    await self.connection.close()
                    try:
                        self.connection = await self.reconnect()
                    except psycopg.Error as reconnect_error:
                        raise GatewayOutcomeUnknown(operation, args) from reconnect_error

    async def _call_once(self, operation, args):
        if self.connection.info.pipeline_status != psycopg.pq.PipelineStatus.OFF:
            # A pipeline can defer the implicit commit until its synchronization point.
            raise RuntimeError("Gateway operation requires a non-pipelined connection")
        if not self.connection.autocommit or self.connection.info.transaction_status not in {
            psycopg.pq.TransactionStatus.IDLE,
            psycopg.pq.TransactionStatus.UNKNOWN,
        }:
            raise RuntimeError("Gateway operation requires its own transaction")
        # One function call is one implicit transaction. A normal (buffered)
        # autocommit execute waits for PostgreSQL to finish the transaction;
        # do not hold the wallet lock across a client-issued COMMIT round trip.
        placeholders = ",".join(["%s"] * len(args))
        query = (
            f"SELECT coalesce(jsonb_agg(to_jsonb(k)),'[]') FROM gateway_v1.{operation}"
            f"({placeholders}) k"
            if operation == "config_keys"
            else f"SELECT gateway_v1.{operation}({placeholders})"
        )
        cursor = await self.connection.execute(query, args)
        return (await cursor.fetchone())[0]

    async def refresh(self):
        async with self.config_lock:
            return await self._refresh()

    async def _refresh(self):
        # Every poll reads the head only; the payload and key changes when they moved.
        head = await self.call("config_head")
        rev = head["revision"]
        if (
            self.snapshot
            and self.snapshot["revision"] == rev
            and self.snapshot_sha256 == head["sha256"]
            and head["applied_revision"] == rev
        ):
            self.last_refresh = monotonic()
            return self.snapshot
        cluster = head["cluster_id"]
        raw = None
        try:
            cached = await self.redis.get(f"{self.namespace}:cfg:{{{cluster}}}:snapshot:{rev}")
        except RedisError:
            cached = None
        if cached:
            try:
                candidate = base64.b64decode(json.loads(cached)["bytes"], validate=True)
                if hashlib.sha256(candidate).hexdigest() == head["sha256"]:
                    raw = candidate
            except ValueError, KeyError, TypeError:
                pass
        if raw is None:
            raw = bytes(await self.call("get_config_snapshot", int(rev)))
        if hashlib.sha256(raw).hexdigest() != head["sha256"]:
            raise ValueError("Invalid snapshot digest")
        data = json.loads(raw)
        if data["schema_version"] != 1 or set(data["required_capabilities"]) - {
            "reservation.v1",
            "usage-events.v1",
        }:
            await self.call("ack_config", int(rev), head["sha256"], False, "unsupported_capability")
            raise ValueError("Unsupported snapshot")
        if self.snapshot and int(rev) < int(self.snapshot["revision"]):
            raise ValueError("Configuration revision regressed")
        await self._keys(int(rev), data["key_set"])
        if self.snapshot != data or head["applied_revision"] != rev:
            await self.call("ack_config", int(rev), head["sha256"], True, "")
        self.last_refresh = monotonic()
        self.node_id = head["node_id"]
        self.snapshot = data
        self.snapshot_sha256 = head["sha256"]
        return data

    async def _keys(self, rev, key_set):
        """Bring self.keys to the release's key sequence: net changes, else all keys."""
        target = int(key_set["sequence"])
        since = self.key_sequence
        if since == target and len(self.keys) == key_set["count"]:
            return
        if since is None or since > target:
            since = 0
        for _ in range(2):
            keys = {} if since == 0 else dict(self.keys)
            by_id = {k["id"]: d for d, k in keys.items()}
            after = (None, None, None)
            while True:
                rows = await self.call("config_keys", rev, since, *after, 5000)
                for row in rows:
                    if row["present"]:
                        keys[row["digest"]] = {
                            "id": row["change_key"],
                            "digest": row["digest"],
                            "organization_id": row["organization_id"],
                            "project_id": row["project_id"],
                            "service_account_id": row["service_account_id"],
                            "expires_at": row["expires_at"],
                        }
                        by_id[row["change_key"]] = row["digest"]
                    elif row["change_key"] in by_id:
                        keys.pop(by_id.pop(row["change_key"]), None)
                if len(rows) < 5000:
                    break
                last = rows[-1]
                after = (last["change_sequence"], UUID(last["change_key"]), last["present"])
            if len(keys) == key_set["count"] and key_set_digest(keys.values()) == key_set["sha256"]:
                self.keys, self.key_sequence = keys, target
                return
            since = 0
        raise ValueError("Key set does not match the release")

    async def watch(self, stop, refresh_interval=5):
        data = await self.refresh()
        channel = f"{self.namespace}:cfg:{data['cluster_id']}:changed"
        while not stop.is_set():
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.subscribe(channel)
                    while not stop.is_set():
                        msg = await subscription.get_message(timeout=1)
                        if msg and msg["type"] == "subscribe":
                            break
                    await self.refresh()
                    while not stop.is_set():
                        await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=refresh_interval
                        )
                        pointer = await self.redis.get(
                            f"{self.namespace}:cfg:{{{data['cluster_id']}}}:current"
                        )
                        if (
                            pointer != self.snapshot["revision"]
                            or monotonic() - self.last_refresh >= refresh_interval
                        ):
                            await self.refresh()
            except RedisError:
                await self.refresh()
                try:
                    await asyncio.wait_for(stop.wait(), refresh_interval)
                except TimeoutError:
                    pass

    async def credential(self, ref, private_key):
        rev = int(self.snapshot["revision"])
        envelope = await self.call(
            "get_credential_envelope", rev, UUID(ref["credential_id"]), ref["credential_version"]
        )
        if envelope["algorithm"] != "libsodium-sealed-box":
            raise ValueError("Unknown envelope")
        data = json.loads(
            SealedBox(PrivateKey(private_key)).decrypt(base64.b64decode(envelope["ciphertext"]))
        )
        if data["cluster_id"] != self.snapshot["cluster_id"] or any(
            data[k] != ref[k] for k in ("credential_id", "credential_version", "key_version")
        ):
            raise ValueError("Credential binding mismatch")
        return data["secret"]

    async def reserve(
        self, secret, model, inputs, outputs, request_id=None, deployment=None, attempt_id=None
    ):
        """Reserve; with a deployment the first attempt is registered in the same call."""
        snapshot = self.snapshot
        resources = snapshot["resources"]
        digest = hashlib.sha256(secret.encode()).hexdigest()
        key = self.keys.get(digest)
        if key is None or not hmac.compare_digest(key["digest"], digest):
            raise ValueError("Invalid API key")
        model_id = str(model)
        definition = next((m for m in resources["models"] if m["id"] == model_id), None)
        if (
            definition is None
            or type(inputs) is not int
            or type(outputs) is not int
            or not (
                0 <= inputs <= definition["max_input_tokens"]
                and 0 <= outputs <= definition["max_output_tokens"]
            )
        ):
            raise ValueError("Invalid model or token bounds")
        if key["expires_at"] and datetime.fromisoformat(key["expires_at"]) <= datetime.now(UTC):
            raise ValueError("Expired API key")
        scope = {
            "organization": key["organization_id"],
            "project": key["project_id"],
            "key": key["id"],
        }
        policies = [
            p
            for p in resources["access_policies"]
            if p["organization_id"] == key["organization_id"]
            and p["scope_id"] == scope[p["scope_type"]]
        ]
        if not any(
            p["layer"] == "platform" and p["scope_type"] == "organization" for p in policies
        ) or any(model_id not in p["allowed_models"] for p in policies):
            raise ValueError("Model outside key policy")
        if not any(
            r["model_id"] == model_id and r["organization_id"] == key["organization_id"]
            for r in resources["routes"]
        ):
            raise ValueError("No eligible route")
        rid = request_id or uuid4()
        lease = await self.limits.acquire(
            key["organization_id"],
            key["project_id"],
            key["id"],
            rid,
            inputs + outputs,
            resources["quota_policies"],
            binding=[self.node_id, str(model), inputs, outputs, snapshot["revision"]],
        )
        first = () if deployment is None else (attempt_id or uuid4(), UUID(str(deployment)))
        try:
            result = await self.call(
                "reserve_request",
                rid,
                UUID(key["id"]),
                UUID(str(model)),
                int(snapshot["revision"]),
                inputs,
                outputs,
                *first,
            )
        except psycopg.errors.RaiseException:
            await self.limits.finish(lease, 0)
            raise
        return result, lease

    async def heartbeat(self, ttl=30):
        await self.redis.set(
            f"{self.namespace}:node:{self.node_id}",
            json.dumps(
                {"revision": self.snapshot["revision"], "seen_at": datetime.now(UTC).isoformat()}
            ),
            ex=ttl,
        )

    async def start_attempt(self, rid, aid, sequence, dep, lease):
        from time import monotonic

        if monotonic() >= lease["safe_until"] or not await self.limits.renew(lease):
            raise RuntimeError("Concurrency lease expired")
        result = await self.call("start_attempt", rid, aid, sequence, UUID(str(dep)))
        if monotonic() >= lease["safe_until"]:
            raise RuntimeError("Concurrency lease expired before execution")
        return result

    def prepare_event(
        self, request_id, event_type, payload, attempt_id=None, event_id=None, revision=None
    ):
        message = {
            "schema_version": 1,
            "event_type": event_type,
            "event_id": str(event_id or uuid4()),
            "request_id": str(request_id),
            "attempt_id": str(attempt_id) if attempt_id else None,
            "config_revision": revision or self.snapshot["revision"],
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        return json.dumps(message, separators=(",", ":"))

    async def event(self, *args, **kwargs):
        return await self.call("append_usage_event", self.prepare_event(*args, **kwargs))

    async def simulate(self, secret, model, inputs, outputs):
        await self.refresh()
        key = self.keys.get(hashlib.sha256(secret.encode()).hexdigest())
        if key is None:
            raise ValueError("Invalid API key")
        route = next(
            r
            for r in self.snapshot["resources"]["routes"]
            if r["model_id"] == str(model) and r["organization_id"] == key["organization_id"]
        )
        candidates = route["candidates"]
        tier = min(item["priority"] for item in candidates)
        active = [item for item in candidates if item["priority"] == tier]
        deployment = random.choices(active, weights=[d["weight"] for d in active], k=1)[0]
        aid = uuid4()
        # The reservation and the first attempt commit together.
        result, lease = await self.reserve(
            secret, model, inputs, outputs, deployment=deployment["deployment_id"], attempt_id=aid
        )
        if result["state"] != "running" or result["closed"] or len(result["attempts"]) != 1:
            return result
        rid = UUID(result["request_id"])
        usage = {"input_total_tokens": inputs, "cached_input_tokens": 0, "output_tokens": outputs}
        model_config = next(
            m for m in self.snapshot["resources"]["models"] if m["id"] == str(model)
        )
        for dimension, field in (
            ("input_cache_write_5m_tokens", "cache_write_5m_tokens"),
            ("input_cache_write_1h_tokens", "cache_write_1h_tokens"),
        ):
            if dimension in model_config["token_dimensions"]:
                usage[field] = 0
        for kind, payload, attempt in (
            (
                "attempt.finished",
                {
                    "outcome": "succeeded",
                    "upstream_request_id": "simulated-" + aid.hex,
                    "usage_format": "fixture.tokens.v1",
                    "raw_usage": usage,
                    "elapsed_ms": 0,
                },
                aid,
            ),
            (
                "request.closed",
                {
                    "delivery_outcome": "succeeded",
                    "attempt_ids": [str(aid)],
                    "delivered_attempt_id": str(aid),
                },
                None,
            ),
        ):
            reply = await self.event(
                rid, kind, payload, attempt_id=attempt, revision=result["config_revision"]
            )
            if reply["status"] != "accepted":
                raise RuntimeError("Conflicting gateway evidence")
        await self.limits.finish(lease, inputs + outputs)
        await self.heartbeat()
        async with asyncio.timeout(30):
            while True:
                status = await self.call("get_request_status", rid)
                if status["processing_state"] in {"done", "review"}:
                    return status
                await asyncio.sleep(0.1)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=UUID)
    parser.add_argument("--input-tokens", type=int, default=2)
    parser.add_argument("--output-tokens", type=int, default=1)
    args = parser.parse_args()
    async with GatewaySimulator.connect(
        os.environ["NEXUS_GATEWAY_DSN"],
        os.environ["NEXUS_REDIS_URL"],
        os.environ.get("NEXUS_REDIS_NAMESPACE", "nexus:v1"),
    ) as peer:
        status = await peer.simulate(
            os.environ["NEXUS_SIMULATION_API_KEY"],
            args.model,
            args.input_tokens,
            args.output_tokens,
        )
        print(json.dumps(status, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
