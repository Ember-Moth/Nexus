"""Real HTTP client -> Rust relay -> private HTTPS upstream, followed by SQL Worker settlement."""

import asyncio
import base64
import gzip
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pg_receipt import lose_gateway_receipts
from test_billing import account_amounts, headers, make_budget
from test_identity_api import auth
from test_plane import plane, provision_plane  # noqa: F401 - pytest fixture

pytestmark = pytest.mark.integration
CRATE = Path(__file__).resolve().parents[2] / "src" / "relay"


@pytest.fixture(scope="session")
def relay_binary():
    import subprocess

    subprocess.run(["cargo", "build", "--locked", "--bin", "nexus-relay"], cwd=CRATE, check=True)
    return CRATE / "target" / "debug" / ("nexus-relay.exe" if os.name == "nt" else "nexus-relay")


@pytest.fixture
def tls(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nexus isolated upstream")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    leaf = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(leaf.public_key())
        .serial_number(2)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), False
        )
        .sign(key, hashes.SHA256())
    )
    root = tmp_path / "ca.pem"
    root.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    pem = tmp_path / "upstream.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + leaf.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pem))
    return ctx, root


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assert_redacted_logs(text, secrets):
    """Relay stderr is JSON lines, and none of the given secrets appear in it."""
    for line in text.splitlines():
        record = json.loads(line)
        assert {"timestamp", "level", "target", "message"} <= record.keys(), line
    for secret in secrets:
        assert secret not in text


def relay_logs(client):
    return [json.loads(line) for line in client.relay_log.read_text().splitlines()]


async def relay_metrics(client):
    async with httpx.AsyncClient(
        base_url=client.metrics_url, timeout=5, trust_env=False
    ) as metrics:
        response = await metrics.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return {
        name: float(value)
        for name, value in (
            line.rsplit(" ", 1) for line in response.text.splitlines() if line[:1] not in "#"
        )
    }


@asynccontextmanager
async def running_relay(program, peer, private, cid, settings, ca, proxy_port):
    port, metrics_port = free_port(), free_port()
    info = peer.connection.info
    login = f"{info.user}:{quote(info.password, safe='')}"
    redis_url = settings.redis_url.get_secret_value()
    env = {
        "NEXUS_RELAY_CLUSTER_ID": cid,
        "NEXUS_RELAY_PRIVATE_KEY": base64.b64encode(bytes(private)).decode(),
        "NEXUS_RELAY_DATABASE_URL": f"postgresql://{login}@127.0.0.1:{proxy_port}/{info.dbname}?sslmode=disable",
        "NEXUS_RELAY_REDIS_URL": redis_url,
        "NEXUS_RELAY_NAMESPACE": settings.redis_namespace,
        "NEXUS_RELAY_UPSTREAM_CA": str(ca),
        "NEXUS_RELAY_LISTEN": f"127.0.0.1:{port}",
        "NEXUS_RELAY_METRICS_LISTEN": f"127.0.0.1:{metrics_port}",
        "NEXUS_RELAY_LEASE_MS": "600",
    }
    secrets = [env["NEXUS_RELAY_PRIVATE_KEY"], info.password, urlsplit(redis_url).password]
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_ACCEPTANCE_")}
    log_dir = Path(tempfile.mkdtemp(prefix="relay-"))
    log_path = log_dir / "stderr.jsonl"
    # Append mode: tests read the file while the relay keeps writing to it.
    with log_path.open("ab") as log:
        process = await asyncio.create_subprocess_exec(
            str(program), env={**inherited, **env}, stderr=log
        )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
        ) as client:
            async with asyncio.timeout(12):
                while True:
                    if process.returncode is not None:
                        pytest.fail(f"Relay exited with {process.returncode}")
                    try:
                        if (await client.get("/ready")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
            client.relay_process = process
            client.relay_log = log_path
            client.metrics_url = f"http://127.0.0.1:{metrics_port}"
            yield client
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
        logs = log_path.read_text(errors="replace")
        sys.stderr.write(logs)  # pytest shows the relay's log with a failing test
        shutil.rmtree(log_dir)
    assert_redacted_logs(logs, [s for s in secrets if s])


async def wait_events(admin, expected):
    async with asyncio.timeout(6):
        while admin.execute("SELECT count(*) FROM billing.usage_events").fetchone()[0] != expected:  # noqa: ASYNC110 - external SQL inbox
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    "family,path", [("anthropic", "/v1/messages"), ("openai_compatible", "/v1/responses")]
)
async def test_native_http_to_ledger(http, admin, tls, relay_binary, family, path):
    app_client, root, app, settings = http
    arrived = asyncio.Event()
    received = []
    mode = "success"
    messages = family == "anthropic"
    endpoint = "anthropic_messages" if messages else "openai_responses"
    native_usage = (
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        if messages
        else {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 0}}
    )
    reply = (
        {
            "id": "msg_native",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
        }
        if messages
        else {
            "id": "resp_native",
            "object": "response",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        }
    )

    async def upstream(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            received.append((head[0], hdr, body))
            arrived.set()
            if mode == "cancel":
                await reader.read()
                return
            if mode == "slow":
                await asyncio.sleep(1)
            out = json.dumps(
                reply if mode == "missing" else {**reply, "usage": native_usage}
            ).encode()
            if mode == "slow":
                assert "gzip" in hdr.get("accept-encoding", "")
                out = gzip.compress(out)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + (b"Content-Encoding: gzip\r\n" if mode == "slow" else b"")
                + f"Content-Length: {len(out)}\r\nConnection: close\r\n\r\n".encode()
                + out
            )
            await writer.drain()
        finally:
            writer.close()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=tls[0])
    port = upstream_server.sockets[0].getsockname()[1]
    async with (
        upstream_server,
        provision_plane(http, admin, family, base_url=f"https://127.0.0.1:{port}/v1") as resources,
    ):
        peer, private, org, model, dep, admission, token, cid = resources
        await make_budget(app_client, root, org, "100")
        profile = {
            "endpoint": endpoint,
            "transports": ["http"],
            "input_ceiling": 4,
            "output_ceiling": 6,
            "bound_reference": "isolated-http",
            "count_rule": "deployment_ceiling.v1",
        }
        patched = await app_client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, 1),
            json={"relay_profile": profile},
        )
        assert patched.status_code == 200, patched.text
        node_id, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        principal = f"/api/v1/gateway-clusters/{cid}/principals/{node_id}"
        resp = await app_client.put(
            principal,
            headers=auth(root),
            json={
                "node_id": str(node_id),
                "database_role": role,
                "capabilities": [
                    "reservation.v1",
                    "usage-events.v1",
                    "relay.native.v1",
                    "relay.provider-features.v1",
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        quota = await app_client.put(
            f"/api/v1/organizations/{org['id']}/quota-policies"
            f"/customer/organization/{org['id']}/concurrency",
            headers=auth(root),
            json={"limit_value": 1},
        )
        assert quota.status_code == 201, quota.text
        release = f"/api/v1/gateway-clusters/{cid}/releases"
        resp = await app_client.post(release, headers=headers(root, "enable-relay"), json={})
        assert resp.status_code == 202, resp.text
        while await app.state.publication.distribute():
            pass
        keyhdr = {"x-api-key": token} if messages else {"authorization": f"Bearer {token}"}
        body = (
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 6}
            if messages
            else {"input": "hi"}
        )
        body["model"] = model["public_name"]
        info = peer.connection.info
        async with (
            lose_gateway_receipts(info.host, info.port) as (proxy_port, dropped),
            running_relay(relay_binary, peer, private, cid, settings, tls[1], proxy_port) as client,
        ):
            rejected = await client.post(path, json=body, headers={"X-Request-ID": "caller-value"})
            assert (
                rejected.status_code == 401 and rejected.headers["x-request-id"] != "caller-value"
            )
            # One response per connection (each response has its own write receipt).
            assert rejected.headers["connection"] == "close"
            assert (
                await client.post(path, headers=keyhdr, json={**body, "model": "forbidden"})
            ).status_code == 403
            assert (
                await client.post(path, headers=keyhdr, json={**body, "stream": True})
            ).status_code == 400
            assert not received and admin.execute(
                "SELECT count(*) FROM billing.request_runs"
            ).fetchone() == (0,)
            mode = "slow"
            first = asyncio.create_task(
                client.post(path, headers={**keyhdr, "x-organization-id": "untrusted"}, json=body)
            )
            waiting = asyncio.create_task(arrived.wait())
            await asyncio.wait([waiting, first], timeout=5, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if first.done():
                assert first.result().status_code == 200, first.result().text
            await asyncio.sleep(0.65)
            assert (await client.post(path, headers=keyhdr, json=body)).status_code == 429
            result = await first
            assert result.status_code == 200, result.text
            assert result.json() == {**reply, "usage": native_usage}
            assert admin.execute(
                "SELECT count(*) FROM billing.request_runs WHERE id=%s",
                (result.headers["x-request-id"],),
            ).fetchone() == (1,)
            assert result.headers["x-request-id"] != rejected.headers["x-request-id"]
            assert len(received) == 1
            line, hdr, outgoing = received[0]
            assert line == f"POST {path} HTTP/1.1" and "x-organization-id" not in hdr
            if messages:
                assert (
                    hdr["x-api-key"] == "provider-secret-do-not-echo"
                    and hdr["anthropic-version"] == "2023-06-01"
                )
                assert outgoing["max_tokens"] == 6
            else:
                assert hdr["authorization"] == "Bearer provider-secret-do-not-echo"
                assert outgoing["max_output_tokens"] == 6 and outgoing["store"] is False
            assert outgoing["model"] == dep["upstream_model"]
            await wait_events(admin, 2)
            assert dropped == ["reserve_request", "start_attempt", "append_usage_event"]
            for (event,) in admin.execute("SELECT envelope FROM billing.usage_events"):
                assert (await peer.call("append_usage_event", json.dumps(event)))[
                    "status"
                ] == "accepted"
            assert await app.state.usage.process() == 1
            assert account_amounts(admin, org) == (
                95,
                0,
            )
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (5, 0)
            assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchall() == [
                (2,)
            ]
            assert admin.execute(
                "SELECT signed_amount FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchall() == [(-5,)]
            # Both billing rejections use HTTP 402, but have different customer remedies.
            for table, column, expected in [
                ("accounts", "balance", "insufficient_funds"),
                ("budget_policies", "limit_amount", "budget_exceeded"),
            ]:
                original = admin.execute(
                    f"SELECT {column} FROM billing.{table} WHERE organization_id=%s", (org["id"],)
                ).fetchone()[0]
                try:
                    admin.execute(
                        f"UPDATE billing.{table} SET {column}=1 WHERE organization_id=%s",
                        (org["id"],),
                    )
                    denied = await client.post(path, headers=keyhdr, json=body)
                    assert denied.status_code == 402, denied.text
                    assert denied.json()["error"]["type" if messages else "code"] == expected
                finally:
                    admin.execute(
                        f"UPDATE billing.{table} SET {column}=%s WHERE organization_id=%s",
                        (original, org["id"]),
                    )
            mode = "missing"
            result = await client.post(path, headers=keyhdr, json=body)
            assert result.status_code == 200 and result.json() == reply
            await wait_events(admin, 4)
            assert await app.state.usage.process() == 1
            assert account_amounts(admin, org) == (
                95,
                10,
            )
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (5, 10)
            assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (2,)
            # Disconnect a real client after the upstream received the POST.
            # The supervised request must close exactly once and release its concurrency slot.
            mode = "cancel"
            arrived.clear()
            _, socket_writer = await asyncio.open_connection("127.0.0.1", client.base_url.port)
            data = json.dumps(body).encode()
            auth_name, auth_value = next(iter(keyhdr.items()))
            socket_writer.write(
                f"POST {path} HTTP/1.1\r\nHost: localhost\r\n".encode()
                + f"{auth_name}: {auth_value}\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                + data
            )
            await socket_writer.drain()
            await asyncio.wait_for(arrived.wait(), 5)
            socket_writer.transport.abort()
            await wait_events(admin, 6)
            assert await app.state.usage.process() == 1
            assert account_amounts(admin, org) == (
                95,
                20,
            )
            assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (3,)
            assert admin.execute(
                "SELECT count(*) FROM billing.usage_events WHERE kind='request.closed' "
                "AND envelope->'payload'->>'delivery_outcome'='unknown'"
            ).fetchone() == (1,)
            # Rotate the upstream credential. An in-memory revision which still
            # references the revoked envelope cannot send another POST.
            credentials = f"/api/v1/upstreams/{dep['upstream_id']}/credentials"
            new_credential = await app_client.post(
                credentials,
                headers=auth(root),
                json={"name": "rotated", "secret": "rotated-test-upstream"},
            )
            assert new_credential.status_code == 201, new_credential.text
            patch = await app_client.patch(
                f"/api/v1/deployments/{dep['id']}",
                headers=auth(root, patched.json()["version"]),
                json={"credential_id": new_credential.json()["id"]},
            )
            assert patch.status_code == 200, patch.text
            revoke = await app_client.post(
                credentials + f"/{dep['credential_id']}/revoke", headers=auth(root, 1)
            )
            assert revoke.status_code == 200, revoke.text
            assert (await client.post(path, headers=keyhdr, json=body)).status_code == 503
            assert len(received) == 3
            published = await app_client.post(
                release, headers=headers(root, "credential-rotation"), json={}
            )
            assert published.status_code == 202, published.text
            # Deliberately withhold Redis publication. Periodic PG reconciliation must install it.
            async with asyncio.timeout(8):
                while True:
                    ack = admin.execute(
                        "SELECT applied_revision FROM publication.gateway_principals "
                        "WHERE node_id=%s",
                        (node_id,),
                    ).fetchone()
                    if ack == (int(published.json()["revision"]),):
                        break
                    await asyncio.sleep(0.03)
            mode = "success"
            assert (await client.post(path, headers=keyhdr, json=body)).status_code == 200
            assert received[-1][1]["x-api-key" if messages else "authorization"] == (
                "rotated-test-upstream" if messages else "Bearer rotated-test-upstream"
            )
            await wait_events(admin, 8)
            assert await app.state.usage.process() == 1
            assert account_amounts(admin, org) == (
                90,
                20,
            )
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (10, 20)
            # Invalidate the owner while an upstream POST is in flight. The renewal timer
            # must cancel the upstream without sending a second POST.
            mode = "cancel"
            arrived.clear()
            pending = asyncio.create_task(client.post(path, headers=keyhdr, json=body))
            await asyncio.wait_for(arrived.wait(), 5)
            tag = "{" + org["id"] + "}"
            slots = f"{settings.redis_namespace}:concurrency:{tag}:{quota.json()['id']}:concurrency"
            assert await peer.redis.delete(slots) == 1
            assert (await pending).status_code == 502
            await wait_events(admin, 10)
            assert await app.state.usage.process() == 1
            assert account_amounts(admin, org) == (
                90,
                30,
            )
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (10, 30)
            resp = await app_client.post(
                f"/api/v1/organizations/{org['id']}/api-keys/{admission.api_key_id}/revoke",
                headers=auth(root, 1),
            )
            assert resp.status_code == 200
            assert (await client.post(path, headers=keyhdr, json=body)).status_code == 401
            assert len(received) == 5
            assert (await client.get("/live")).status_code == 200
            samples = await relay_metrics(client)
            series = f'endpoint="{endpoint}",transport="http"'
            assert samples[f'nexus_relay_requests_total{{{series},outcome="succeeded"}}'] >= 2
            assert samples[f'nexus_relay_requests_total{{{series},outcome="unknown"}}'] >= 2
            assert samples[f'nexus_relay_requests_total{{{series},outcome="rejected"}}'] >= 4
            stream = f'endpoint="{endpoint}",transport="sse",outcome="rejected"'
            assert samples[f"nexus_relay_requests_total{{{stream}}}"] == 1
            # Five upstream POSTs; the two cancelled ones never received response headers.
            assert samples[f"nexus_relay_upstream_first_byte_seconds_count{{{series}}}"] == 3
            assert samples[f"nexus_relay_upstream_duration_seconds_count{{{series}}}"] == 5
            reserve = 'dependency="database",operation="reserve_request",kind="unavailable"'
            assert samples[f"nexus_relay_control_errors_total{{{reserve}}}"] >= 1
            assert samples["nexus_relay_completion_event_replays_total"] >= 1
            assert samples["nexus_relay_snapshot_revision"] >= 2
            assert samples["nexus_relay_control_plane_healthy"] == 1
            assert samples["nexus_relay_draining"] == 0
            assert samples["nexus_relay_capacity_limit"] == 128
            logs = relay_logs(client)
            finished = [r for r in logs if r["message"] == "request finished"]
            assert {r["delivery"] for r in finished} >= {"succeeded", "unknown"}
            assert all(r["endpoint"] == endpoint and r["transport"] == "http" for r in finished)
            assert any(r["message"] == "configuration snapshot applied" for r in logs)
            text = client.relay_log.read_text()
            for secret in [token, "provider-secret-do-not-echo", "rotated-test-upstream"]:
                assert secret not in text


async def test_rust_replays_python_quota_lease(plane, http, admin, relay_binary):  # noqa: F811
    peer, _, org, model, _, admission, _, cid = plane
    client, root, app, settings = http
    for metric, limit in [("rpm", 5), ("tpm", 30), ("concurrency", 1)]:
        result = await client.put(
            f"/api/v1/organizations/{org['id']}/quota-policies"
            f"/platform/key/{admission.api_key_id}/{metric}",
            headers=auth(root),
            json={"limit_value": limit},
        )
        assert result.status_code == 201, result.text
    result = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "lease-test"), json={}
    )
    assert result.status_code == 202
    while await app.state.publication.distribute():
        pass
    snapshot = await peer.refresh()
    policies = snapshot["resources"]["quota_policies"]
    from uuid import uuid4

    rid = uuid4()

    def acquire(request):
        return peer.limits.acquire(
            org["id"],
            admission.project_id,
            admission.api_key_id,
            request,
            10,
            policies,
            ttl_ms=10000,
            binding=[peer.node_id, model["id"], 4, 6, snapshot["revision"]],
        )

    info = peer.connection.info
    login = f"{info.user}:{quote(info.password, safe='')}"

    async def rust(request, expired):
        inputs = {
            "url": f"postgresql://{login}@{info.host}:{info.port}/{info.dbname}",
            "redis": settings.redis_url.get_secret_value(),
            "namespace": settings.redis_namespace,
            "cluster": cid,
            "key_id": str(admission.api_key_id),
            "request": str(request),
            "model": model["id"],
            "revision": int(snapshot["revision"]),
            "digest": result.json()["sha256"],
            "expired": expired,
        }
        proc = await asyncio.create_subprocess_exec(
            "cargo",
            "test",
            "--locked",
            "--test",
            "control",
            "cross_language_lease",
            "--",
            "--ignored",
            cwd=CRATE,
            env={**os.environ, "NEXUS_RELAY_TEST_INPUT": json.dumps(inputs)},
        )
        assert await proc.wait() == 0

    first = await acquire(rid)
    await rust(rid, False)
    assert await peer.limits.finish(first, 5) and not await peer.limits.renew(first)
    for policy, key in zip(first["specs"], first["keys"][1:], strict=False):
        if policy["metric"] in {"tpm", "rpm"}:
            value = await peer.redis.get(key)
            if value is None:
                assert (await peer.redis.time())[0] // 60 > first["window"]
            else:
                assert value == ("5" if policy["metric"] == "tpm" else "1")
    replacement = uuid4()
    active = await acquire(replacement)
    await rust(rid, True)
    assert await peer.limits.finish(first, 5)
    assert await peer.limits.renew(active)
    assert await peer.limits.finish(active, 0)


async def test_node_validates_cluster_key_before_ack(plane, http, admin, tls, relay_binary):  # noqa: F811
    from nacl.public import PrivateKey

    peer, private, _, _, _, _, _, cid = plane
    client, root, _, settings = http
    info = peer.connection.info
    replacement = PrivateKey.generate()
    async with running_relay(relay_binary, peer, private, cid, settings, tls[1], info.port) as old:
        patch = await client.patch(
            f"/api/v1/gateway-clusters/{cid}",
            headers=auth(root, 2),
            json={
                "public_key": base64.b64encode(bytes(replacement.public_key)).decode(),
                "key_grace_seconds": 0,
            },
        )
        assert patch.status_code == 200, patch.text
        result = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases",
            headers=headers(root, "rotate-cluster"),
            json={},
        )
        assert result.status_code == 202
        new_revision = int(result.json()["revision"])
        async with asyncio.timeout(8):
            while (await old.get("/ready")).status_code != 503:  # noqa: ASYNC110 - HTTP probe
                await asyncio.sleep(0.05)
        assert admin.execute(
            "SELECT applied_revision FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone() == (new_revision - 1,)
    async with running_relay(
        relay_binary, peer, replacement, cid, settings, tls[1], info.port
    ) as new:
        assert (await new.get("/ready")).status_code == 200
        assert admin.execute(
            "SELECT applied_revision FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone() == (new_revision,)


async def test_upgrade_in_flight_legacy_attempt(plane, http, admin, migration_settings):  # noqa: F811
    from uuid import UUID, uuid4

    from alembic import command
    from alembic.config import Config

    peer, _, org, model, dep, a, raw, _ = plane
    config = Config(str(CRATE.parents[1] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    await asyncio.to_thread(command.downgrade, config, "0012_gateway_admission")
    try:
        old = await peer.refresh()
        assert "relay_profile" not in old["resources"]["deployments"][0]
        await peer.reserve(raw, model["id"], 4, 6, a.request_id)
        attempt = uuid4()
        await peer.call("start_attempt", a.request_id, attempt, 1, UUID(dep["id"]))
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    assert (await peer.refresh()) == old
    assert (await peer.call("start_attempt", a.request_id, attempt, 1, UUID(dep["id"])))["replayed"]
    assert admin.execute(
        "SELECT expected_usage_format FROM billing.request_attempts WHERE id=%s", (attempt,)
    ).fetchone() == (None,)
    await peer.event(
        a.request_id,
        "attempt.finished",
        {
            "outcome": "succeeded",
            "upstream_request_id": None,
            "elapsed_ms": 1,
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {"input_total_tokens": 2, "cached_input_tokens": 0, "output_tokens": 3},
        },
        attempt_id=attempt,
    )
    await peer.event(
        a.request_id,
        "request.closed",
        {
            "delivery_outcome": "succeeded",
            "attempt_ids": [str(attempt)],
            "delivered_attempt_id": str(attempt),
        },
    )
    assert await http[2].state.usage.process() == 1
    assert account_amounts(admin, org) == (95, 0)
