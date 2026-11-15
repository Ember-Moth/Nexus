"""Explicit semantic approximations use actual provider rates and complete receipts."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration
ALLOW = "x-nexus-allow-approximation"
APPLIED = "x-nexus-applied-approximation"
BILLING = "x-nexus-approximation-billing"


async def test_prefill_cache_and_local_stop_require_individual_consent(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    sent = []
    release = asyncio.Event()
    truncated = False

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            h = {
                k.lower(): v.strip() for line in lines[1:] if line for k, v in [line.split(":", 1)]
            }
            assert ALLOW not in h
            req = json.loads(await reader.readexactly(int(h["content-length"])))
            sent.append(req)

            def msg(id, value):
                return {
                    "id": id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": value, "annotations": []}],
                }

            items = [msg("m0", "可见。結"), msg("m1", "尾private-after-stop")]
            usage = {
                "input_tokens": 4,
                "output_tokens": 6,
                "input_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 2,
                    "cache_write_tokens_details": {"ttl_30m_tokens": 2, "ttl_12h_tokens": 0},
                },
            }
            result = {
                "id": "reply",
                "status": "completed",
                "object": "response",
                "model": req["model"],
                "output": items,
                "usage": usage,
            }
            if req.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )

                async def frame(v):
                    writer.write(f"event: {v['type']}\ndata: {json.dumps(v)}\n\n".encode())
                    await writer.drain()

                await frame(
                    {
                        "type": "response.created",
                        "response": {"id": "reply", "status": "in_progress"},
                    }
                )
                await frame(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": items[0] | {"content": [], "status": "in_progress"},
                    }
                )
                await frame(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "m0",
                        "content_index": 0,
                        "output_index": 0,
                        "delta": "可见。",
                    }
                )
                await frame(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "m0",
                        "content_index": 0,
                        "output_index": 0,
                        "delta": "結",
                    }
                )
                if truncated:
                    await frame(
                        {
                            "type": "response.output_text.delta",
                            "item_id": "m0",
                            "content_index": 0,
                            "output_index": 0,
                            "delta": "尾unread",
                        }
                    )
                    return
                await release.wait()
                for i, item in enumerate(items):
                    await frame(
                        {"type": "response.output_item.done", "output_index": i, "item": item}
                    )
                await frame({"type": "response.completed", "response": result})
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\n\r\n" + json.dumps(result).encode()
                )
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            "openai_compatible",
            convert_from="anthropic_messages",
            auto_cache=True,
        ) as (client, model, key, _, _),
    ):
        url = "/v1/messages"
        hdr = {"x-api-key": key}
        q = {
            "model": model,
            "max_tokens": 6,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "story",
                            "cache_control": {"type": "ephemeral", "ttl": "5m"},
                        }
                    ],
                },
                {"role": "assistant", "content": "Opening:"},
            ],
            "stop_sequences": ["結尾", "END"],
        }
        rules = "cache-5m=30m, stop=local, prefill=prompt"
        for options in [
            None,
            "stop=local, prefill=prompt",
            "cache-5m=30m, prefill=prompt",
            "cache-5m=30m, stop=local",
            "cache-5m=12h, stop=local, prefill=prompt",
            "stop=local, stop=local",
            "cache-5m=1h",
            "all",
        ]:
            result = await client.post(
                url, headers=hdr | ({ALLOW: options} if options else {}), json=q
            )
            assert result.status_code == 400, result.text
        assert not sent and admin.execute(
            "SELECT count(*) FROM billing.request_runs"
        ).fetchone() == (0,)
        for stream in [False, True]:
            h = hdr | {ALLOW: rules}
            if stream:
                async with client.stream(
                    "POST", url, headers=h, json=q | {"stream": True}
                ) as result:
                    assert result.status_code == 200
                    lines = result.aiter_lines()
                    frames = []
                    async with asyncio.timeout(5):
                        async for line in lines:
                            if line.startswith("data: "):
                                f = json.loads(line[6:])
                                frames.append(f)
                                if f.get("delta", {}).get("text") == "可见。":
                                    break
                    assert not release.is_set()
                    release.set()
                    frames.extend(
                        [json.loads(s[6:]) async for s in lines if s.startswith("data: ")]
                    )
                assert frames[-1]["type"] == "message_stop"
                assert frames[-2]["delta"] == {
                    "stop_reason": "stop_sequence",
                    "stop_sequence": "結尾",
                }
                assert "".join(f.get("delta", {}).get("text", "") for f in frames) == "可见。"
                assert "private-after-stop" not in json.dumps(frames)
            else:
                result = await client.post(url, headers=h, json=q)
                assert result.status_code == 200, result.text
                assert result.json()["content"] == [{"type": "text", "text": "可见。"}]
                assert (
                    result.json()["stop_reason"] == "stop_sequence"
                    and result.json()["stop_sequence"] == "結尾"
                )
            assert result.headers[APPLIED] == rules
            assert (
                result.headers[BILLING]
                == "input_cache_write_30m_tokens, full_upstream_output, continuation_prompt_input"
            )
            wire = sent[-1]
            assert wire["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
                "mode": "explicit",
                "ttl": "30m",
            }
            assert wire["input"][-2] == {"role": "assistant", "content": "Opening:"}
            assert (
                wire["input"][-1]["role"] == "user"
                and "continuation" in wire["input"][-1]["content"]
            )
            assert "stop_sequences" not in wire
            await wait_events(admin, 2 * len(sent))
            assert await http[2].state.usage.process() == 1
        assert (
            admin.execute(
                "SELECT reserved_amount,charged_amount FROM billing.request_runs"
            ).fetchall()
            == [(11, Decimal("10.5"))] * 2
        )
        assert (
            admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchall()
            == [(Decimal("4.2"),)] * 2
        )
        api, token, _, _ = http
        from test_identity_api import auth

        org = admin.execute("SELECT organization_id FROM billing.request_runs LIMIT 1").fetchone()[
            0
        ]
        report = await api.get(
            f"/api/v1/organizations/{org}/requests/{result.headers['x-request-id']}",
            headers=auth(token),
        )
        assert report.status_code == 200, report.text
        lines = {line["dimension"]: line for line in report.json()["charge_details"]}
        assert lines["input_cache_write_30m_tokens"]["quantity"] == 2
        assert Decimal(lines["input_cache_write_30m_tokens"]["unit_price"]) == Decimal("1.25")
        assert lines["output_tokens"]["quantity"] == 6
        logs = [
            json.loads(line)
            for line in client.relay_log.read_text().splitlines()
            if line.startswith("{")
        ]
        audits = [
            v
            for v in logs
            if v.get("request_id") == result.headers["x-request-id"]
            and v.get("message") == "request approximation applied"
        ]
        assert (
            len(audits) == 1
            and audits[0]["approximation"] == rules
            and audits[0]["billing_basis"] == result.headers[BILLING]
        )
        assert all("private-after-stop" not in str(v) and "Opening:" not in str(v) for v in logs)
        chat = {
            "model": model,
            "messages": [{"role": "user", "content": "go"}],
            "stop": "結尾",
            "max_tokens": 6,
        }
        for stream in [False, True]:
            r = await client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer " + key, ALLOW: "stop=local"},
                json=chat | {"stream": stream},
            )
            assert r.status_code == 200, r.text
            assert (
                r.headers[APPLIED] == "stop=local" and r.headers[BILLING] == "full_upstream_output"
            )
            if stream:
                frames = [
                    json.loads(s[6:])
                    for s in r.text.splitlines()
                    if s.startswith("data: ") and "[DONE]" not in s
                ]
                assert frames[-1]["choices"][0]["finish_reason"] == "stop"
                assert (
                    "".join(f["choices"][0]["delta"].get("content", "") for f in frames) == "可见。"
                )
            else:
                assert r.json()["choices"][0]["message"]["content"] == "可见。"
            await wait_events(admin, 2 * len(sent))
            assert await http[2].state.usage.process() == 1
        native = await client.post(
            "/v1/responses",
            headers={"authorization": "Bearer " + key, ALLOW: rules},
            json={"model": model, "input": "go"},
        )
        assert (
            native.status_code == 200
            and APPLIED not in native.headers
            and BILLING not in native.headers
        )

        await wait_events(admin, 2 * len(sent))
        await http[2].state.usage.process()
        truncated = True
        received = []
        async with client.stream(
            "POST", url, headers=hdr | {ALLOW: rules}, json=q | {"stream": True}
        ) as lost:
            assert lost.status_code == 200
            with pytest.raises(httpx.RemoteProtocolError):
                async for line in lost.aiter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))
        assert all(f["type"] not in {"message_stop", "message_delta"} for f in received)
        await wait_events(admin, 2 * len(sent))
        await http[2].state.usage.process()
        assert admin.execute(
            "SELECT state,reservation_state,charged_amount FROM billing.request_runs WHERE id=%s",
            (lost.headers["x-request-id"],),
        ).fetchone() == ("review", "held", None)


async def test_exact_first_then_consented_failover(http, admin, tls, relay_binary):  # noqa: F811
    from uuid import uuid4

    from test_billing import headers, publish_price
    from test_catalog import make_deployment, make_upstream
    from test_identity_api import auth
    from test_plane import provision_plane
    from test_relay import running_relay

    api, root, app, settings = http
    decline = False
    paths = []

    async def upstream(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            h = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            path = head[0].split()[1]
            q = json.loads(await reader.readexactly(int(h["content-length"])))
            paths.append(path)
            status = "200 OK"
            if path.endswith("messages"):
                assert q["stop_sequences"] == ["END"]
                if decline:
                    status = "503 Service Unavailable"
                    body = {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "busy"},
                    }
                else:
                    body = {
                        "id": "r",
                        "type": "message",
                        "role": "assistant",
                        "model": q["model"],
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "stop_sequence",
                        "stop_sequence": "END",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    }
            else:
                assert path.endswith("responses") and "stop_sequences" not in q
                body = {
                    "id": "r",
                    "object": "response",
                    "status": "completed",
                    "model": q["model"],
                    "output": [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "okENDtail", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 4,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + json.dumps(body).encode()
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=tls[0])
    base = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    async with server, provision_plane(http, admin, "anthropic", base_url=base) as plane:
        peer, private, org, model, exact, _, key, cluster = plane
        extended = await api.patch(
            f"/api/v1/models/{model['id']}",
            headers=auth(root, model["version"]),
            json={"protocols": ["anthropic", "openai_compatible"]},
        )
        assert extended.status_code == 200, extended.text
        source = await make_upstream(
            api, root, "Responses alternative", "openai_compatible", base_url=base
        )
        cred = await api.post(
            f"/api/v1/upstreams/{source['id']}/credentials",
            headers=auth(root),
            json={"name": "alternative", "secret": "fixture"},
        )
        alternate = await make_deployment(api, root, model, source, cred.json())
        await publish_price(api, root, "cost", alternate, "0.2")
        profile = {
            "endpoint": "anthropic_messages",
            "transports": ["http"],
            "input_ceiling": 4,
            "output_ceiling": 6,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "isolated fallback bounds",
        }
        for dep, p in [
            (exact, profile),
            (
                alternate,
                profile | {"endpoint": "openai_responses", "convert_from": "anthropic_messages"},
            ),
        ]:
            changed = await api.patch(
                f"/api/v1/deployments/{dep['id']}",
                headers=auth(root, dep["version"]),
                json={"relay_profile": p},
            )
            assert changed.status_code == 200, changed.text
        route = await api.put(
            f"/api/v1/route-policies/{org['id']}/{model['id']}",
            headers=auth(root, 1),
            json={
                "candidates": [
                    {"deployment_id": alternate["id"], "priority": 0},
                    {"deployment_id": exact["id"], "priority": 5},
                ]
            },
        )
        assert route.status_code == 200, route.text
        version = admin.execute(
            "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        changed = await api.patch(
            f"/api/v1/gateway-clusters/{cluster}",
            headers=auth(root, version),
            json={"deployment_ids": [exact["id"], alternate["id"]]},
        )
        assert changed.status_code == 200
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cluster,),
        ).fetchone()
        reg = await api.put(
            f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
            headers=auth(root),
            json={
                "node_id": str(node),
                "database_role": role,
                "capabilities": [
                    "reservation.v1",
                    "usage-events.v1",
                    "relay.native.v1",
                    "relay.messages-to-responses.v1",
                ],
            },
        )
        assert reg.status_code == 200
        release = await api.post(
            f"/api/v1/gateway-clusters/{cluster}/releases",
            headers=headers(root, uuid4().hex),
            json={},
        )
        assert release.status_code == 202, release.text
        async with running_relay(
            relay_binary, peer, private, cluster, settings, tls[1], peer.connection.info.port
        ) as client:
            h = {"x-api-key": key, ALLOW: "stop=local"}
            q = {
                "model": model["public_name"],
                "max_tokens": 6,
                "messages": [{"role": "user", "content": "go"}],
                "stop_sequences": ["END"],
            }
            r = await client.post("/v1/messages", headers=h, json=q)
            assert r.status_code == 200 and APPLIED not in r.headers and paths == ["/v1/messages"]
            await wait_events(admin, 2)
            await app.state.usage.process()
            decline = True
            r = await client.post("/v1/messages", headers=h, json=q)
            assert r.status_code == 200, r.text
            assert r.headers[APPLIED] == "stop=local" and r.json()["content"][0]["text"] == "ok"
            assert paths == ["/v1/messages", "/v1/messages", "/v1/responses"]
            await wait_events(admin, 5)
            await app.state.usage.process()
            rid = r.headers["x-request-id"]
            assert admin.execute(
                "SELECT charged_amount FROM billing.request_runs WHERE id=%s", (rid,)
            ).fetchone() == (5,)
            assert admin.execute(
                "SELECT cost_amount FROM billing.request_attempts "
                "WHERE request_id=%s AND deployment_id=%s",
                (rid, alternate["id"]),
            ).fetchone() == (1,)


@pytest.mark.parametrize("messages", [True, False], ids=["12h_to_1h", "1h_to_12h"])
async def test_explicit_long_lifetime_prices_target_dimension(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    messages,
):
    sent = []
    target, source = ("1h", "12h") if messages else ("12h", "1h")
    rule = f"cache-{source}={target}"
    dim = f"input_cache_write_{target}_tokens"

    async def upstream(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            h = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            q = json.loads(await reader.readexactly(int(h["content-length"])))
            sent.append(q)
            if messages:
                result = {
                    "type": "message",
                    "id": "r",
                    "role": "assistant",
                    "model": q["model"],
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 2,
                        },
                    },
                }
            else:
                result = {
                    "object": "response",
                    "id": "r",
                    "model": q["model"],
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 2,
                        "input_tokens_details": {
                            "cached_tokens": 0,
                            "cache_write_tokens": 2,
                            "cache_write_tokens_details": {
                                "ttl_30m_tokens": 0,
                                "ttl_12h_tokens": 2,
                            },
                        },
                    },
                }
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\n\r\n" + json.dumps(result).encode()
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=tls[0])
    family = "anthropic" if messages else "openai_compatible"
    origin = "openai_responses" if messages else "anthropic_messages"
    native = "messages" if messages else "input"
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            family,
            convert_from=origin,
            auto_cache=True,
            cache_rates={dim: "2" if messages else "2.5"},
        ) as (relay, model, key, _, _),
    ):
        url = "/v1/responses" if messages else "/v1/messages"
        h = {"authorization": "Bearer " + key} if messages else {"x-api-key": key}
        q = {"model": model}
        if messages:
            q |= {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "data",
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                ],
                "prompt_cache_options": {"ttl": source, "mode": "implicit"},
            }
        else:
            q |= {
                "max_tokens": 6,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "data",
                                "cache_control": {"type": "ephemeral", "ttl": source},
                            }
                        ],
                    }
                ],
                "cache_control": {"type": "ephemeral", "ttl": source},
            }
        assert (await relay.post(url, headers=h, json=q)).status_code == 400
        res = await relay.post(url, headers=h | {ALLOW: rule}, json=q)
        assert res.status_code == 200, res.text
        assert res.headers[APPLIED] == rule and res.headers[BILLING] == dim
        marker = "cache_control" if messages else "prompt_cache_breakpoint"
        assert sent[-1][native][0]["content"][0][marker]["ttl"] == target
        assert sent[-1]["cache_control" if messages else "prompt_cache_options"]["ttl"] == target
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        expected = Decimal(8 if messages else 9)
        assert admin.execute("SELECT charged_amount FROM billing.request_runs").fetchone() == (
            expected,
        )
        assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchone() == (
            expected * Decimal("0.4"),
        )
        if messages:
            # The default is ignored when explicit-only caching has no breakpoints.
            empty = q | {
                "input": "plain",
                "prompt_cache_options": {"mode": "explicit", "ttl": "12h"},
            }
            no_write = await relay.post(url, headers=h | {ALLOW: rule}, json=empty)
            assert no_write.status_code == 200 and APPLIED not in no_write.headers
            assert "cache_control" not in sent[-1]
            initial = len(sent)
            invalid = q | {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "short",
                                "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "30m"},
                            },
                            {
                                "type": "input_text",
                                "text": "long",
                                "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "12h"},
                            },
                        ],
                    }
                ]
            }
            invalid_result = await relay.post(
                url, headers=h | {ALLOW: rule + ", cache-30m=5m"}, json=invalid
            )
            assert invalid_result.status_code == 400 and len(sent) == initial
