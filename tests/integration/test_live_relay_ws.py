"""Opt-in two-turn P3b check. The merchant key is supplied only to the isolated catalog."""

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit, urlunsplit

import pytest
from test_billing import headers
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401
from websockets.asyncio.client import connect

pytestmark = pytest.mark.integration


@asynccontextmanager
async def authorized_endpoint(url, context):
    target = urlsplit(url)
    if (
        target.path != "/v1/responses"
        or target.username
        or target.password
        or target.query
        or target.fragment
    ):
        pytest.fail("Expected a Responses WebSocket endpoint without URL credentials")
    if target.scheme == "wss":
        yield urlunsplit(("https", target.netloc, "/v1", "", "")), None
        return
    if target.scheme != "ws" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Plain WS is supported only for an explicitly selected loopback endpoint")
    statuses = []

    async def forward(client, writer):
        upstream = None
        tasks = []
        try:
            reader, upstream = await asyncio.open_connection(target.hostname, target.port or 80)
            head = await client.readuntil(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            upstream.write(
                b"\r\n".join(
                    b"Host: " + target.netloc.encode()
                    if line.lower().startswith(b"host:")
                    else line
                    for line in lines
                )
            )
            await upstream.drain()
            response = await reader.readuntil(b"\r\n\r\n")
            statuses.append(int(response.split(b" ", 2)[1]))
            writer.write(response)
            await writer.drain()

            async def copy(src, dst):
                while chunk := await src.read(16384):
                    dst.write(chunk)
                    await dst.drain()

            tasks = [
                asyncio.create_task(copy(client, upstream)),
                asyncio.create_task(copy(reader, writer)),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for sock in (upstream, writer):
                if sock:
                    sock.close()
                    with suppress(OSError):
                        await sock.wait_closed()

    bridge = await asyncio.start_server(forward, "127.0.0.1", 0, ssl=context)
    async with bridge:
        yield f"https://127.0.0.1:{bridge.sockets[0].getsockname()[1]}/v1", statuses


@pytest.mark.skipif(
    os.environ.get("RELAY_ACCEPTANCE_WS_RUN") != "1",
    reason="requires explicitly authorized merchant WS deployment",
)
async def test_authorized_two_turns(http, admin, tls, relay_binary):  # noqa: F811
    endpoint = os.environ["RELAY_ACCEPTANCE_WS_URL"]
    # This value must never appear in pytest parameters, logs, exception text, or reports.
    secret = os.environ["RELAY_ACCEPTANCE_WS_KEY"]
    upstream_model = os.environ["RELAY_ACCEPTANCE_WS_MODEL"]
    cap = int(os.environ.get("RELAY_ACCEPTANCE_WS_OUTPUT", "64"))
    client, root, app, settings = http
    async with (
        authorized_endpoint(endpoint, tls[0]) as (base, statuses),
        provision_plane(http, admin, balance="300000", base_url=base) as values,
    ):
        peer, private, _, model, dep, _, token, cid = values
        r = await client.post(
            f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
            headers=auth(root),
            json={"name": "authorized-test", "secret": secret},
        )
        if r.status_code != 201:
            pytest.fail("Unable to install the authorized catalog credential")
        profile = {
            "endpoint": "openai_responses",
            "transports": ["websocket"],
            "input_ceiling": model["max_input_tokens"],
            "output_ceiling": cap,
            "continuation": True,
            "streaming": True,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "isolated acceptance wallet, maximum public-model input",
        }
        r = await client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={
                "relay_profile": profile,
                "credential_id": r.json()["id"],
                "upstream_model": upstream_model,
            },
        )
        assert r.status_code == 200
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        r = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{node}",
            headers=auth(root),
            json={
                "node_id": str(node),
                "database_role": role,
                "capabilities": [
                    "reservation.v1",
                    "usage-events.v1",
                    "relay.native.v1",
                    "relay.provider-features.v1",
                    "relay.responses-ws.v1",
                ],
            },
        )
        assert r.status_code == 200
        r = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases",
            headers=headers(root, "authorized-wss"),
            json={},
        )
        assert r.status_code == 202
        async with (
            running_relay(
                relay_binary, peer, private, cid, settings, tls[1], peer.connection.info.port
            ) as public,
            connect(
                f"ws://127.0.0.1:{public.base_url.port}/v1/responses",
                additional_headers={"Authorization": f"Bearer {token}"},
                proxy=None,
            ) as ws,
        ):
            observed = []
            parent = None
            for prompt in ("Reply with OK.", "Reply with OK again."):
                create = {
                    "type": "response.create",
                    "model": model["public_name"],
                    "input": [{"role": "user", "content": prompt}],
                    "max_output_tokens": cap,
                }
                if parent:
                    create["previous_response_id"] = parent
                await ws.send(json.dumps(create))
                async with asyncio.timeout(45):
                    while True:
                        event = json.loads(await ws.recv())
                        if event["type"] in {"error", "response.failed", "response.incomplete"}:
                            code = event.get("error", {}).get("code") or event.get(
                                "response", {}
                            ).get("incomplete_details", {}).get("reason")
                            pytest.fail(f"Authorized deployment returned {event['type']} ({code})")
                        if event["type"] == "response.completed":
                            resp = event["response"]
                            assert resp["id"] != parent
                            parent = resp["id"]
                            measured = resp["usage"]
                            observed.append((measured["input_tokens"], measured["output_tokens"]))
                            break
            await wait_events(admin, 4)
            assert await app.state.usage.process() == 2
            debits = admin.execute(
                "SELECT signed_amount FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchall()
            assert len(debits) == 2 and all(d[0] < 0 for d in debits)
            if statuses is not None:
                assert statuses == [101]
            print(
                "Authorized WS turns verified",
                ws.response.status_code,
                statuses,
                observed,
                "isolated ledger entries",
                len(debits),
            )
