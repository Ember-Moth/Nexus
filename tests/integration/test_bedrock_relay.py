"""Exercise both public protocols through a TLS Bedrock Runtime peer and the settlement Worker."""

import asyncio
import hashlib
import hmac
import json
import struct
import zlib
from urllib.parse import quote

import httpx
import pytest
from test_billing import account_amounts, headers, make_budget
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401
from test_relay_conversion import scenarios
from websockets.asyncio.client import connect

pytestmark = pytest.mark.integration
MODEL = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/example"
REGION = "us-east-1"
KEYS = {
    "access_key_id": "EXAMPLEKEY",
    "secret_access_key": "example-signing-secret",
    "session_token": "temporary-token",
}
USAGE = {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}
FREE_GUARDRAIL_USAGE = {
    "contentPolicyUnits": 0,
    "topicPolicyUnits": 0,
    "sensitiveInformationPolicyUnits": 0,
    "contextualGroundingPolicyUnits": 0,
    "sensitiveInformationPolicyFreeUnits": 1,
    "wordPolicyUnits": 1,
}
FREE_TRACE = {
    "guardrail": {
        "inputAssessment": {"test": {"invocationMetrics": {"usage": FREE_GUARDRAIL_USAGE}}}
    }
}


def verify_signature(headers, path, data):
    algorithm, params = headers["authorization"].split(" ", 1)
    assert algorithm == "AWS4-HMAC-SHA256"
    params = dict(kv.split("=", 1) for kv in params.split(", "))
    date = headers["x-amz-date"]
    scope = f"{date[:8]}/{REGION}/bedrock/aws4_request"
    assert params["Credential"] == f"{KEYS['access_key_id']}/{scope}"
    assert headers["x-amz-security-token"] == KEYS["session_token"]
    signed = params["SignedHeaders"]
    assert {"content-type", "host", "x-amz-date", "x-amz-security-token"} <= set(signed.split(";"))
    canonical = "\n".join(
        [
            "POST",
            quote(path, safe="/-_.~"),
            "",
            "".join(f"{k}:{headers[k]}\n" for k in signed.split(";")),
            signed,
            hashlib.sha256(data).hexdigest(),
        ]
    )
    to_sign = "\n".join([algorithm, date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing_key = ("AWS4" + KEYS["secret_access_key"]).encode()
    for part in scope.split("/"):
        signing_key = hmac.digest(signing_key, part.encode(), "sha256")
    assert (
        hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest() == params["Signature"]
    )


def frame(kind, payload, exception=False):
    data = json.dumps(payload, ensure_ascii=False).encode()
    headers = b""
    for name, value in [
        (":message-type", "exception" if exception else "event"),
        (":exception-type" if exception else ":event-type", kind),
        (":content-type", "application/json"),
    ]:
        name, value = name.encode(), value.encode()
        headers += bytes([len(name)]) + name + b"\x07" + struct.pack(">H", len(value)) + value
    prelude = struct.pack(">II", 16 + len(headers) + len(data), len(headers))
    body = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + data
    return body + struct.pack(">I", zlib.crc32(body))


@pytest.mark.parametrize("sigv4", [False, True], ids=["bedrock_api_key", "sigv4_session"])
async def test_bedrock_converse_and_stream(http, admin, tls, relay_binary, sigv4):  # noqa: F811
    client, root, app, settings = http
    seen = []
    excerpt = "青い空"
    title = "Sky"
    source = "https://example.org/sky"
    locations = [
        {"documentChar": {"documentIndex": 0, "start": 0, "end": 3}},
        {"searchResultLocation": {"searchResultIndex": 0, "start": 0, "end": 1}},
    ]
    citations = [
        {
            "title": title,
            "location": loc,
            "sourceContent": [{"text": excerpt}],
            **({"source": source} if i else {}),
        }
        for i, loc in enumerate(locations)
    ]
    cited = {"content": [{"text": "Sky."}], "citations": citations}
    mode = "success"
    release = asyncio.Event()

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            data = await reader.readexactly(int(hdr["content-length"]))
            path = head[0].split()[1]
            if path.endswith("/count-tokens"):
                assert path == f"/model/{quote(MODEL, safe='')}/count-tokens"
                if sigv4:
                    verify_signature(hdr, path, data)
                else:
                    assert hdr["authorization"] == "Bearer provider-secret-do-not-echo"
                counted = json.loads(data)["input"]["converse"]
                assert (
                    counted["messages"]
                    and "inferenceConfig" not in counted
                    and "additionalModelResponseFieldPaths" not in counted
                    and "guardrailConfig" not in counted
                )
                assert counted["additionalModelRequestFields"] == {"top_k": 5}
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b'Content-Length: 18\r\nConnection: close\r\n\r\n{"inputTokens":19}'
                )
                await writer.drain()
                return
            stream = path.endswith("/converse-stream")
            assert path == f"/model/{quote(MODEL, safe='')}/" + (
                "converse-stream" if stream else "converse"
            )
            if sigv4:
                verify_signature(hdr, path, data)
            else:
                assert hdr["authorization"] == "Bearer provider-secret-do-not-echo"
            assert "x-api-key" not in hdr and "anthropic-version" not in hdr
            seen.append(json.loads(data))
            if mode == "http_error":
                writer.write(
                    b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n"
                    b"x-amzn-errortype: ThrottlingException\r\nConnection: close\r\n\r\n"
                    + b'{"message":"slow down"}'
                )
            elif not stream:
                reply = {
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"text": "京都"},
                                {
                                    "toolUse": {
                                        "toolUseId": "next",
                                        "name": "lookup",
                                        "input": {"q": "雨"},
                                    }
                                },
                            ],
                        }
                    },
                    "stopReason": "tool_use",
                    "usage": USAGE,
                    "trace": FREE_TRACE,
                }
                if mode == "private":
                    reply["output"]["message"]["content"] = [
                        {
                            "reasoningContent": {
                                "reasoningText": {
                                    "text": "public summary",
                                    "signature": "private-aws-signature",
                                }
                            }
                        },
                        {"reasoningContent": {"redactedContent": "AQID"}},
                        {"text": "answer"},
                    ]
                    reply["stopReason"] = "end_turn"
                if mode == "citations":
                    reply["output"]["message"]["content"] = [{"citationsContent": cited}]
                    reply["stopReason"] = "end_turn"
                if mode == "missing":
                    reply.pop("usage")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"x-amzn-requestid: aws-one\r\nConnection: close\r\n\r\n"
                    + json.dumps(reply).encode()
                )
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.amazon.eventstream\r\n"
                    b"x-amzn-requestid: aws-stream\r\nConnection: close\r\n\r\n"
                )
                messages = [
                    ("messageStart", {"role": "assistant"}),
                    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "京都"}}),
                    ("contentBlockStop", {"contentBlockIndex": 0}),
                    (
                        "contentBlockStart",
                        {
                            "contentBlockIndex": 1,
                            "start": {"toolUse": {"toolUseId": "next", "name": "lookup"}},
                        },
                    ),
                    (
                        "contentBlockDelta",
                        {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"q":'}}},
                    ),
                    (
                        "contentBlockDelta",
                        {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '"雨"}'}}},
                    ),
                    ("contentBlockStop", {"contentBlockIndex": 1}),
                    ("messageStop", {"stopReason": "tool_use"}),
                    (
                        "metadata",
                        {"usage": USAGE, "metrics": {"latencyMs": 20}, "trace": FREE_TRACE},
                    ),
                ]
                if mode == "citations":
                    fragments = [
                        citations[0],
                        {"title": title, "source": source, "location": locations[1]},
                        {"sourceContent": [{"text": "青い"}]},
                        {"sourceContent": [{"text": "空"}]},
                    ]
                    messages = [
                        ("messageStart", {"role": "assistant"}),
                        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Sky."}}),
                        *(
                            (
                                "contentBlockDelta",
                                {"contentBlockIndex": 0, "delta": {"citation": c}},
                            )
                            for c in fragments
                        ),
                        ("contentBlockStop", {"contentBlockIndex": 0}),
                        ("messageStop", {"stopReason": "end_turn"}),
                        ("metadata", {"usage": USAGE, "trace": FREE_TRACE}),
                    ]
                if mode == "private":
                    messages = [("messageStart", {"role": "assistant"})]
                    for index, pieces in enumerate(
                        [
                            [
                                {"reasoningContent": {"text": "public summary"}},
                                {"reasoningContent": {"signature": "private-aws-signature"}},
                            ],
                            [
                                {"reasoningContent": {"redactedContent": "AQ=="}},
                                {"reasoningContent": {"redactedContent": "AgM="}},
                            ],
                            [{"text": "answer"}],
                        ]
                    ):
                        messages.extend(
                            ("contentBlockDelta", {"contentBlockIndex": index, "delta": p})
                            for p in pieces
                        )
                        messages.append(("contentBlockStop", {"contentBlockIndex": index}))
                    messages.extend(
                        [
                            ("messageStop", {"stopReason": "end_turn"}),
                            ("metadata", {"usage": USAGE, "trace": FREE_TRACE}),
                        ]
                    )
                for i, (kind, obj) in enumerate(messages):
                    if i == 1 and mode == "exception":
                        writer.write(frame("throttlingException", {"message": "slow stream"}, True))
                        break
                    if kind == "metadata":
                        if mode == "truncated":
                            break
                        await release.wait()
                    blob = frame(kind, obj)
                    if i == 1 and mode == "crc":
                        blob = blob[:-1] + bytes([blob[-1] ^ 1])
                    for pos in range(0, len(blob), 5):
                        writer.write(blob[pos : pos + 5])
                        await writer.drain()
            await writer.drain()
        finally:
            writer.close()

    upstream = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        upstream,
        provision_plane(
            http,
            admin,
            "aws_bedrock",
            base_url=f"https://127.0.0.1:{upstream.sockets[0].getsockname()[1]}",
        ) as resources,
    ):
        peer, private, org, model, dep, _, key, cid = resources
        await make_budget(client, root, org, "100")
        requested = {
            "guardrailConfig": {
                "guardrailIdentifier": "wxyz",
                "guardrailVersion": "2",
                "trace": "enabled",
            },
            "additionalModelRequestFields": {"top_k": 7},
        }
        profile = {
            "endpoint": "bedrock_converse",
            "transports": ["http"],
            "streaming": True,
            "tools": True,
            "input_ceiling": 4,
            "output_ceiling": 6,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "isolated-converse",
            "bedrock_guardrail": {
                "guardrailIdentifier": "abcd",
                "guardrailVersion": "1",
                "trace": "enabled",
            },
            "bedrock_model_fields": {"top_k": 5},
            "bedrock_request_options": [requested],
        }
        changes = {"region": REGION, "upstream_model": MODEL, "relay_profile": profile}
        if sigv4:
            cred = await client.post(
                f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
                headers=auth(root),
                json={"name": "session", "secret": json.dumps(KEYS, separators=(",", ":"))},
            )
            assert cred.status_code == 201
            changes["credential_id"] = cred.json()["id"]
        route = f"/api/v1/deployments/{dep['id']}"
        invalid = await client.patch(
            route, headers=auth(root, dep["version"]), json={**changes, "region": "global"}
        )
        assert invalid.status_code == 422
        r = await client.patch(route, headers=auth(root, dep["version"]), json=changes)
        assert r.status_code == 200, r.text
        nid, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        principal = f"/api/v1/gateway-clusters/{cid}/principals/{nid}"
        caps = ["reservation.v1", "usage-events.v1", "relay.native.v1", "relay.sse.v1"]
        for extra, expected in [
            ([], 409),
            (["relay.bedrock-converse.v1"], 409),
            (["relay.bedrock-converse.v1", "relay.bedrock-settings.v1"], 409),
            (
                [
                    "relay.bedrock-converse.v1",
                    "relay.bedrock-settings.v1",
                    "relay.bedrock-request-options.v1",
                    "relay.bedrock-guardrail-usage.v1",
                ],
                202,
            ),
        ]:
            assert (
                await client.put(
                    principal,
                    headers=auth(root),
                    json={"node_id": str(nid), "database_role": role, "capabilities": caps + extra},
                )
            ).status_code == 200
            published = await client.post(
                f"/api/v1/gateway-clusters/{cid}/releases",
                headers=headers(root, "bedrock"),
                json={},
            )
            assert published.status_code == expected, published.text
        while await app.state.publication.distribute():
            pass
        urls = ["/v1/messages", "/v1/responses"]
        hdrs = [{"x-api-key": key}, {"Authorization": f"Bearer {key}"}]
        bodies = scenarios(model["public_name"])
        bodies[0]["messages"][0]["content"] = [
            {"type": "text", "text": "hello"},
            {
                "type": "video",
                "source": {"type": "base64", "media_type": "video/mp4", "data": "AAAA"},
            },
        ]
        bodies[1]["input"][0]["content"] = [
            {"type": "input_text", "text": "hello"},
            {
                "type": "input_file",
                "file_data": "data:video/mp4;base64,AAAA",
                "filename": "clip.mp4",
            },
        ]
        async with running_relay(
            relay_binary, peer, private, cid, settings, tls[1], peer.connection.info.port
        ) as relay:
            for i in [0, 1]:
                response = await relay.post(
                    urls[i], headers=hdrs[i], json={**bodies[i], "unknown_option": "reject"}
                )
                assert response.status_code == 400
            for bad in [
                {"bedrock": []},
                {"bedrock": {"guardrailConfig": None}},
                {
                    "bedrock": {
                        "guardrailConfig": {
                            "guardrailIdentifier": "foreign",
                            "guardrailVersion": "1",
                        }
                    }
                },
                {"bedrock": {"additionalModelRequestFields": {"max_tokens": 900}}},
                {"bedrock": requested, "top_k": 8},
            ]:
                response = await relay.post(urls[0], headers=hdrs[0], json={**bodies[0], **bad})
                assert response.status_code == 400
            assert not seen and admin.execute(
                "SELECT count(*) FROM billing.request_runs"
            ).fetchone() == (0,)
            for i in [0, 1]:
                response = await relay.post(urls[i], headers=hdrs[i], json=bodies[i])
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["id"] == "aws-one" and result["model"] == model["public_name"]
                if i == 0:
                    assert (
                        result["stop_reason"] == "tool_use" and result["content"][1]["id"] == "next"
                    )
                else:
                    assert result["usage"]["total_tokens"] == 5
                    assert (
                        result["status"] == "completed" and result["output"][1]["call_id"] == "next"
                    )
                await wait_events(admin, 2 * i + 2)
            count_body = {k: v for k, v in bodies[0].items() if k != "max_tokens"}
            count_body["stop_sequences"] = ["END"]
            count = await relay.post("/v1/messages/count_tokens", headers=hdrs[0], json=count_body)
            assert count.status_code == 200 and count.json() == {"input_tokens": 19}, count.text
            assert seen[0] == seen[1]
            req = seen[0]
            assert req["guardrailConfig"] == profile["bedrock_guardrail"]
            assert req["additionalModelRequestFields"] == {"top_k": 5}
            assert req["messages"][0]["content"][1] == {
                "video": {"format": "mp4", "source": {"bytes": "AAAA"}}
            }
            assert req["system"] == [{"text": "Look up the answer."}] and req[
                "inferenceConfig"
            ] == {"maxTokens": 6}
            assert req["messages"][-1]["content"][0]["toolResult"]["toolUseId"] == "history"
            assert (
                req["toolConfig"]["tools"][0]["toolSpec"]["name"] == "lookup" and "model" not in req
            )
            for i in [0, 1]:
                release.clear()
                async with relay.stream(
                    "POST",
                    urls[i],
                    headers=hdrs[i],
                    json={**bodies[i], "stream": True, "bedrock": requested},
                ) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    assert (await asyncio.wait_for(anext(lines), 4)) == "event: " + (
                        "message_start" if i == 0 else "response.created"
                    )
                    await asyncio.sleep(0.7)
                    assert admin.execute(
                        "SELECT count(*) FROM billing.usage_events"
                    ).fetchone() == (4 + i * 2,)
                    release.set()
                    events = [
                        json.loads(line[6:]) async for line in lines if line.startswith("data: ")
                    ]
                assert seen[-1]["guardrailConfig"] == {
                    **requested["guardrailConfig"],
                    "trace": "enabled",
                }
                assert seen[-1]["additionalModelRequestFields"] == {"top_k": 7}
                if i == 0:
                    assert (
                        events[-1]["type"] == "message_stop"
                        and events[-2]["usage"]["output_tokens"] == 3
                    )
                    assert json.loads(
                        "".join(
                            e["delta"]["partial_json"]
                            for e in events
                            if e.get("delta", {}).get("type") == "input_json_delta"
                        )
                    ) == {"q": "雨"}
                else:
                    assert events[-1]["response"]["usage"]["total_tokens"] == 5
                    assert events[-1]["response"]["output"][1]["call_id"] == "next"
                await wait_events(admin, 6 + i * 2)
            assert len(seen) == 4 and await app.state.usage.process() == 4
            assert (
                admin.execute(
                    "SELECT expected_usage_format,cost_amount FROM billing.request_attempts"
                ).fetchall()
                == [("aws.bedrock.converse.v1", 2)] * 4
            )
            assert account_amounts(admin, org) == (
                80,
                0,
            )
            mode = "missing"
            assert (await relay.post(urls[1], headers=hdrs[1], json=bodies[1])).status_code == 200
            await wait_events(admin, 10)
            mode = "http_error"
            e = await relay.post(urls[0], headers=hdrs[0], json=bodies[0])
            assert e.status_code == 429 and e.json()["error"]["type"] == "rate_limit_error"
            await wait_events(admin, 12)
            mode = "exception"
            e = await relay.post(urls[1], headers=hdrs[1], json={**bodies[1], "stream": True})
            assert "event: error" in e.text and "event: response.completed" not in e.text
            await wait_events(admin, 14)
            for n, fault in enumerate(["truncated", "crc"]):
                mode = fault
                with pytest.raises(httpx.RemoteProtocolError):
                    await relay.post(urls[0], headers=hdrs[0], json={**bodies[0], "stream": True})
                await wait_events(admin, 16 + 2 * n)
            await app.state.usage.process()
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (20, 30)
            async with connect(
                f"ws://127.0.0.1:{relay.base_url.port}/v1/responses",
                additional_headers=hdrs[1],
                proxy=None,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {"type": "response.create", "model": model["public_name"], "input": "hi"}
                    )
                )
                assert json.loads(await ws.recv())["error"]["code"] == "deployment_unavailable"
            assert len(seen) == 9
            mode = "citations"
            conversation = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "title": title,
                            "source": {"type": "text", "media_type": "text/plain", "data": excerpt},
                            "citations": {"enabled": True},
                        },
                        {
                            "type": "search_result",
                            "source": source,
                            "title": title,
                            "content": [{"type": "text", "text": excerpt}],
                            "citations": {"enabled": True},
                        },
                        {"type": "text", "text": "Cite both."},
                    ],
                }
            ]
            query = {"model": model["public_name"], "max_tokens": 6, "messages": conversation}
            expected = [
                {
                    "type": "char_location",
                    "cited_text": excerpt,
                    "document_index": 0,
                    "document_title": title,
                    "start_char_index": 0,
                    "end_char_index": 3,
                },
                {
                    "type": "search_result_location",
                    "cited_text": excerpt,
                    "source": source,
                    "title": title,
                    "search_result_index": 0,
                    "start_block_index": 0,
                    "end_block_index": 1,
                },
            ]
            answer = await relay.post(urls[0], headers=hdrs[0], json=query)
            assert answer.status_code == 200, answer.text
            content = answer.json()["content"]
            assert content == [{"type": "text", "text": "Sky.", "citations": expected}]
            upstream_content = seen[-1]["messages"][0]["content"]
            assert upstream_content[0]["document"]["citations"] == {"enabled": True}
            assert upstream_content[1]["searchResult"]["source"] == source
            conversation.extend(
                [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Continue."},
                ]
            )
            stream = await relay.post(urls[0], headers=hdrs[0], json={**query, "stream": True})
            assert stream.status_code == 200
            assert seen[-1]["messages"][1]["content"] == [{"citationsContent": cited}]
            events = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            assert [
                v["delta"]["citation"]
                for v in events
                if v.get("delta", {}).get("type") == "citations_delta"
            ] == expected
            assert events[-1]["type"] == "message_stop"
            await wait_events(admin, 22)
            assert await app.state.usage.process() == 2
            assert admin.execute(
                "SELECT charged_amount FROM billing.request_runs ORDER BY created_at DESC LIMIT 2"
            ).fetchall() == [(5,), (5,)]

            mode = "private"
            public_request = {
                "model": model["public_name"],
                "max_output_tokens": 6,
                "input": "go",
                "reasoning": {"effort": "low", "summary": "auto"},
            }
            first = await relay.post(
                urls[1], headers=hdrs[1], json=public_request | {"stream": True}
            )
            assert first.status_code == 200, first.text
            assert "private-aws-signature" not in first.text
            terminal = [
                json.loads(line[6:])
                for line in first.text.splitlines()
                if line.startswith("data: ")
            ][-1]
            assert terminal["type"] == "response.completed"
            items = terminal["response"]["output"]
            assert items[0]["summary"][0]["text"] == "public summary" and items[1]["summary"] == []
            follow = public_request | {
                "input": [
                    {"role": "user", "content": "go"},
                    *items,
                    {"role": "user", "content": "continue"},
                ]
            }
            second = await relay.post(urls[1], headers=hdrs[1], json=follow)
            assert second.status_code == 200, second.text
            assert seen[-1]["messages"][1]["content"][:2] == [
                {
                    "reasoningContent": {
                        "reasoningText": {
                            "text": "public summary",
                            "signature": "private-aws-signature",
                        }
                    }
                },
                {"reasoningContent": {"redactedContent": "AQID"}},
            ]
            assert seen[-1]["additionalModelRequestFields"]["output_config"] == {"effort": "low"}
            await wait_events(admin, 26)
            assert await app.state.usage.process() == 2


async def test_claude_invoke_transport(http, admin, tls, relay_binary):  # noqa: F811
    import base64

    from test_relay_conversion import output, stream_events

    calls = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            op = head[0].split()[1].rsplit("/", 1)[1]
            calls.append((op, body))
            if op == "count-tokens":
                source = json.loads(base64.b64decode(body["input"]["invokeModel"]["body"]))
                assert source["anthropic_version"] == "bedrock-2023-05-31"
                wire = json.dumps({"inputTokens": 12}).encode()
                media = "application/json"
            elif op == "invoke":
                wire = json.dumps(output(True)).encode()
                media = "application/json"
            else:
                assert op == "invoke-with-response-stream"
                wire = b"".join(
                    frame("chunk", {"bytes": base64.b64encode(json.dumps(e).encode()).decode()})
                    for e in stream_events(True, output(True))
                )
                media = "application/vnd.amazon.eventstream"
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {media}\r\n"
                    f"Content-Length: {len(wire)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    client, root, app, settings = http
    async with (
        server,
        provision_plane(
            http,
            admin,
            "aws_bedrock",
            base_url=f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}",
        ) as plane,
    ):
        peer, private, org, model, dep, _, key, cid = plane
        profile = {
            "endpoint": "bedrock_converse",
            "bedrock_invoke": True,
            "transports": ["http"],
            "tools": True,
            "streaming": True,
            "input_ceiling": 4,
            "output_ceiling": 6,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "local AWS Claude peer",
        }
        patch = await client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={"upstream_model": MODEL, "region": REGION, "relay_profile": profile},
        )
        assert patch.status_code == 200, patch.text
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        caps = [
            "reservation.v1",
            "usage-events.v1",
            "relay.native.v1",
            "relay.sse.v1",
            "relay.bedrock-converse.v1",
            "relay.bedrock-claude-invoke.v1",
        ]
        ack = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{node}",
            headers=auth(root),
            json={"node_id": str(node), "database_role": role, "capabilities": caps},
        )
        assert ack.status_code == 200
        published = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases",
            headers=headers(root, "invoke-config"),
            json={},
        )
        assert published.status_code == 202, published.text
        async with running_relay(
            relay_binary, peer, private, cid, settings, tls[1], peer.connection.info.port
        ) as public:
            body = {
                "model": model["public_name"],
                "messages": [{"role": "user", "content": "hello"}],
            }
            hdr = {"x-api-key": key}
            count = await public.post("/v1/messages/count_tokens", headers=hdr, json=body)
            assert count.status_code == 200 and count.json() == {"input_tokens": 12}
            for streaming in [False, True]:
                r = await public.post(
                    "/v1/messages", headers=hdr, json=body | {"stream": streaming, "max_tokens": 6}
                )
                assert r.status_code == 200, r.text
                if streaming:
                    assert "message_stop" in r.text
                else:
                    assert r.json()["content"][0]["text"] == "京都"
                assert calls[-1][1]["anthropic_version"] == "bedrock-2023-05-31"
                assert "model" not in calls[-1][1] and "stream" not in calls[-1][1]
            await wait_events(admin, 4)
            assert await app.state.usage.process() == 2
            assert account_amounts(admin, org) == (90, 0)
            assert (
                admin.execute(
                    "SELECT expected_usage_format,cost_amount FROM billing.request_attempts"
                ).fetchall()
                == [("anthropic.messages.v1", 2)] * 2
            )
