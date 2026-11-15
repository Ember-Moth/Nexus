"""The Relay's admission path end to end: conditional configuration polling, the dedicated
refresh connection, cluster-wide admission slots and write-progress based delivery."""

import asyncio
import json
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from test_billing import account_amounts, headers, make_budget
from test_identity_api import auth
from test_plane import provision_plane
from test_project_credentials import key as issue_key
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401

pytestmark = pytest.mark.integration
USAGE = {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 0}}


def reply(rid="resp_admission"):
    return {
        "id": rid,
        "object": "response",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        "usage": USAGE,
    }


class Provider:
    """A TLS Responses upstream: JSON replies, or one large SSE stream of `stream_bytes`."""

    def __init__(self, stream_bytes=0):
        self.stream_bytes = stream_bytes
        self.finished_at = None

    async def __call__(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            if not body.get("stream"):
                out = json.dumps(reply()).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(out)}\r\nConnection: close\r\n\r\n".encode()
                    + out
                )
                await writer.drain()
                return
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )

            async def put(event):
                writer.write(f"data: {json.dumps(event)}\n\n".encode())
                await writer.drain()

            await put(
                {"type": "response.created", "response": {"id": "r", "status": "in_progress"}}
            )
            chunk = "x" * 32768
            for _ in range(self.stream_bytes // len(chunk)):
                await put({"type": "response.output_text.delta", "item_id": "m", "delta": chunk})
            await put(
                {
                    "type": "response.completed",
                    "response": {"id": "r", "status": "completed", "usage": USAGE},
                }
            )
            self.finished_at = time.monotonic()
        finally:
            writer.close()


@asynccontextmanager
async def relay_plane(http, admin, tls, binary, provider, *, log=False, env=None):  # noqa: F811
    """A cluster with one Relay node serving the provider; no quota policies are set.

    The node keeps its production lease (15 s) so requests may queue on locks; `env` adds
    NEXUS_RELAY_* settings.
    """
    client, root, app, settings = http
    upstream = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    port = upstream.sockets[0].getsockname()[1]
    async with (
        upstream,
        provision_plane(http, admin, base_url=f"https://127.0.0.1:{port}/v1") as plane,
    ):
        peer, private, org, model, dep, admission, token, cid = plane
        await make_budget(client, root, org, "100")
        profile = {
            "endpoint": "openai_responses",
            "transports": ["http"],
            "input_ceiling": 4,
            "output_ceiling": 6,
            "streaming": True,
            "bound_reference": "admission",
            "count_rule": "deployment_ceiling.v1",
        }
        r = await client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={"relay_profile": profile},
        )
        assert r.status_code == 200, r.text
        nid, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        caps = [
            "reservation.v1",
            "usage-events.v1",
            "relay.native.v1",
            "relay.provider-features.v1",
            "relay.sse.v1",
            "relay.cluster-policy.v1",
        ]
        r = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
            headers=auth(root),
            json={"node_id": str(nid), "database_role": role, "capabilities": caps},
        )
        assert r.status_code == 200, r.text
        await publish(http, cid, "relay")
        if log:
            admin.execute(
                sql.SQL("ALTER ROLE {} SET log_min_duration_statement = 0").format(
                    sql.Identifier(role)
                )
            )
        info = peer.connection.info
        try:
            async with running_relay(
                binary,
                peer,
                private,
                cid,
                settings,
                tls[1],
                info.port,
                {"NEXUS_RELAY_LEASE_MS": "15000", **(env or {})},
            ) as relay:
                relay.role, relay.org, relay.model = role, org, model["public_name"]
                relay.token, relay.cid, relay.plane = token, cid, plane
                yield relay
        finally:
            admin.execute(
                sql.SQL("ALTER ROLE {} RESET log_min_duration_statement").format(
                    sql.Identifier(role)
                )
            )


async def publish(http, cid, name):
    client, root, app, _ = http
    r = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases",
        headers=headers(root, name + uuid4().hex),
        json={},
    )
    assert r.status_code == 202, r.text
    while await app.state.publication.distribute():
        pass
    return int(r.json()["revision"])


async def until(predicate, seconds, what):
    """Poll an external condition (database, Relay) until it holds."""
    try:
        async with asyncio.timeout(seconds):
            while not await predicate():  # noqa: ASYNC110 - external state
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail(f"timed out waiting for: {what}")


def statements(offset):
    """gateway_v1 calls in the PostgreSQL log after byte `offset`, by function name."""
    text = (Path(os.environ["NEXUS_TEST_SERVICES"]) / "postgres.log").read_bytes()[offset:]
    counts = {}
    for name in ("config_head", "get_config_snapshot", "config_keys"):
        # One execution logs its parse/bind/execute steps; any count above zero means it ran.
        counts[name] = text.count(f"gateway_v1.{name}(".encode())
    return counts


def log_size():
    return (Path(os.environ["NEXUS_TEST_SERVICES"]) / "postgres.log").stat().st_size


def body(relay, **extra):
    return {"model": relay.model, "input": "hi", **extra}


def bearer(token):
    return {"authorization": f"Bearer {token}"}


async def test_polling_reads_only_the_head_and_still_sees_revocation_and_scope_expiry(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    client, root, app, _ = http
    async with relay_plane(http, admin, tls, relay_binary, Provider(), log=True) as relay:
        assert (
            await relay.post("/v1/responses", headers=bearer(relay.token), json=body(relay))
        ).status_code == 200
        # Idle polls read config_head only; nothing changed, so no payload or keys are read.
        offset = log_size()
        await asyncio.sleep(6)
        idle = statements(offset)
        assert (
            idle["config_head"] >= 2 and idle["get_config_snapshot"] == idle["config_keys"] == 0
        ), idle
        # A new key arrives as a key change of the next release.
        _, _, org, _, _, admission, _, cid = relay.plane
        issued = (await issue_key(client, root, org, str(admission.service_account_id))).json()
        fresh = bearer(issued["secret"])
        assert (
            await relay.post("/v1/responses", headers=fresh, json=body(relay))
        ).status_code == 401
        offset = log_size()
        await publish(http, cid, "new-key")

        async def accepted():
            return (
                await relay.post("/v1/responses", headers=fresh, json=body(relay))
            ).status_code == 200

        await until(accepted, 8, "new key accepted")
        changed = statements(offset)
        assert changed["get_config_snapshot"] >= 1 and changed["config_keys"] >= 1, changed
        # Revocation: the node drops the key once the release that removes it is polled.
        revoked = await client.post(
            f"/api/v1/organizations/{org['id']}/api-keys/{issued['key']['id']}/revoke",
            headers=auth(root, 1),
        )
        assert revoked.status_code == 200, revoked.text
        await publish(http, cid, "revoked")

        async def rejected():
            return (
                await relay.post("/v1/responses", headers=fresh, json=body(relay))
            ).status_code == 401

        await until(rejected, 8, "revoked key rejected")
        # Scope expiry fails config_head itself, so the node leaves the load balancer.
        current = (await client.get(f"/api/v1/gateway-clusters/{cid}", headers=auth(root))).json()
        r = await client.patch(
            f"/api/v1/gateway-clusters/{cid}",
            headers=auth(root, current["version"]),
            json={"organization_ids": [str(uuid4())]},
        )
        assert r.status_code == 200, r.text

        async def unready():
            return (await relay.get("/ready")).status_code == 503

        await until(unready, 8, "scope expiry detected")
        assert (
            await relay.post("/v1/responses", headers=bearer(relay.token), json=body(relay))
        ).status_code == 503


@asynccontextmanager
async def organization_lock(admin, org):
    """Hold the organization's admission lock, as a long serial section would."""
    info = admin.info
    with psycopg.connect(
        host=info.host,
        port=info.port,
        dbname=info.dbname,
        user=info.user,
        password=info.password,
        autocommit=True,
    ) as holder:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s::text,398221))", (org["id"],))
        try:
            yield
        finally:
            holder.execute("SELECT pg_advisory_unlock_all()")


def lock_waiters(admin, role):
    return admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock'",
        (role,),
    ).fetchone()[0]


async def post_all(relay, n):
    async with httpx.AsyncClient(base_url=str(relay.base_url), timeout=30, trust_env=False) as c:
        return await asyncio.gather(
            *(
                c.post("/v1/responses", headers=bearer(relay.token), json=body(relay))
                for _ in range(n)
            )
        )


async def test_refresh_keeps_its_own_connection_when_the_pool_is_exhausted(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    env = {"NEXUS_RELAY_POOL_SIZE": "1"}
    async with relay_plane(http, admin, tls, relay_binary, Provider(), env=env) as relay:
        async with organization_lock(admin, relay.org):
            pending = asyncio.create_task(post_all(relay, 1))

            async def queued():
                return lock_waiters(admin, relay.role) == 1

            await until(queued, 5, "request waits on the organization lock")
            # The only pool connection waits on the lock; a new release still applies.
            rev = await publish(http, relay.cid, "while-exhausted")

            async def applied():
                assert (await relay.get("/ready")).status_code == 200
                return (
                    admin.execute(
                        "SELECT applied_revision FROM publication.gateway_principals "
                        "WHERE database_role=%s",
                        (relay.role,),
                    ).fetchone()[0]
                    == rev
                )

            await until(applied, 8, "release applied on the refresh connection")
        assert [r.status_code for r in await pending] == [200]


async def test_admission_slots_bound_the_sessions_queued_in_the_database(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    client, root, _, _ = http
    env = {
        "NEXUS_RELAY_TENANT_ADMISSIONS": "1",
        "NEXUS_RELAY_ADMISSION_WAIT_MS": "1500",
        "NEXUS_RELAY_POOL_SIZE": "4",
    }
    async with relay_plane(http, admin, tls, relay_binary, Provider(), env=env) as relay:
        async with organization_lock(admin, relay.org):
            pending = asyncio.create_task(post_all(relay, 3))

            async def queued():
                return lock_waiters(admin, relay.role) == 1

            await until(queued, 5, "one admission reaches the database")
            await asyncio.sleep(0.5)
            # The other two wait for a slot in the Relay, not on the lock.
            assert lock_waiters(admin, relay.role) == 1
            await asyncio.sleep(1.5)
        results = await pending
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 429, 429], [r.text for r in results]
        assert {r.json()["error"]["code"] for r in results if r.status_code == 429} == {
            "rate_limited"
        }
        # The organization's platform policy overrides the node default.
        r = await client.put(
            f"/api/v1/organizations/{relay.org['id']}/quota-policies"
            f"/platform/organization/{relay.org['id']}/admission_concurrency",
            headers=auth(root),
            json={"limit_value": 3},
        )
        assert r.status_code == 201, r.text
        customer = await client.put(
            f"/api/v1/organizations/{relay.org['id']}/quota-policies"
            f"/customer/organization/{relay.org['id']}/admission_concurrency",
            headers=auth(root),
            json={"limit_value": 3},
        )
        assert customer.status_code == 422, customer.text
        rev = await publish(http, relay.cid, "admissions")

        async def applied():
            return (
                admin.execute(
                    "SELECT applied_revision FROM publication.gateway_principals "
                    "WHERE database_role=%s",
                    (relay.role,),
                ).fetchone()[0]
                == rev
            )

        await until(applied, 8, "policy applied")
        async with organization_lock(admin, relay.org):
            pending = asyncio.create_task(post_all(relay, 3))

            async def all_queued():
                return lock_waiters(admin, relay.role) == 3

            await until(all_queued, 5, "three admissions reach the database")
        assert [r.status_code for r in await pending] == [200, 200, 200]


async def test_a_slow_reader_that_keeps_reading_receives_and_pays_for_its_stream(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    # One second without write progress ends a wait; this client never pauses that long but
    # needs several seconds to read what is still buffered when the upstream finishes.
    provider = Provider(stream_bytes=4 * 1024 * 1024)
    env = {"NEXUS_RELAY_STREAM_IDLE_MS": "1000"}
    async with relay_plane(http, admin, tls, relay_binary, provider, env=env) as relay:
        payload = json.dumps(body(relay, stream=True)).encode()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, ("127.0.0.1", relay.base_url.port))
        await loop.sock_sendall(
            sock,
            b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
            + f"Authorization: Bearer {relay.token}\r\n".encode()
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload,
        )
        received = bytearray()
        try:
            async with asyncio.timeout(60):
                while not received.endswith(b"0\r\n\r\n"):
                    chunk = await loop.sock_recv(sock, 32768)
                    if not chunk:
                        break
                    received += chunk
                    await asyncio.sleep(0.05)
        finally:
            sock.close()
        done = time.monotonic()
        assert b"response.completed" in received and received.endswith(b"0\r\n\r\n")
        assert provider.finished_at is not None
        drained = done - provider.finished_at
        await wait_events(admin, 2)
        await http[2].state.usage.process()
        closed = admin.execute(
            "SELECT envelope#>>'{payload,delivery_outcome}' FROM billing.usage_events "
            "WHERE kind='request.closed'"
        ).fetchone()[0]
        assert closed == "succeeded", (closed, drained)
        assert account_amounts(admin, relay.org) == (95, 0)
