"""Run a bounded pair of generations through an explicitly configured Bedrock Runtime."""

import json
import os
import re

import pytest
from test_billing import headers, publish_price
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401

from nexus.modules.billing.domain.money import GUARDRAIL_DIMENSIONS

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("RELAY_ACCEPTANCE_BEDROCK_RUN") != "1",
    reason="requires a configured AWS Bedrock test deployment",
)
async def test_authorized_bedrock_generation_and_settlement(http, admin, tls, relay_binary):  # noqa: F811
    prefix = "RELAY_ACCEPTANCE_BEDROCK_"
    region = os.environ[prefix + "REGION"]
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", region):
        pytest.fail("Invalid Bedrock region")
    domain = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    base = f"https://bedrock-runtime.{region}.{domain}"
    mode = os.environ.get(prefix + "MODE", "converse")
    if mode not in {"converse", "claude_invoke"}:
        pytest.fail("Expected converse or claude_invoke")
    guardrail = os.environ.get(prefix + "GUARDRAIL_ID")
    if guardrail and (mode != "converse" or not os.environ.get(prefix + "GUARDRAIL_VERSION")):
        pytest.fail(
            "A Guardrail acceptance call requires Converse and a specified Guardrail version"
        )
    api, root, app, settings = http
    async with provision_plane(
        http, admin, "aws_bedrock", balance="300000", base_url=base
    ) as plane:
        peer, private, _, model, dep, _, token, cluster = plane
        credential = await api.post(
            f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
            headers=auth(root),
            json={"name": "acceptance", "secret": os.environ[prefix + "KEY"]},
        )
        if credential.status_code != 201:
            pytest.fail("Could not install the supplied Bedrock credential")
        profile = {
            "endpoint": "bedrock_converse",
            "bedrock_invoke": mode == "claude_invoke",
            "transports": ["http"],
            "streaming": True,
            "tools": False,
            "input_ceiling": model["max_input_tokens"],
            "output_ceiling": 64,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "isolated live test wallet",
        }
        if guardrail:
            profile["bedrock_guardrail"] = {
                "guardrailIdentifier": guardrail,
                "guardrailVersion": os.environ[prefix + "GUARDRAIL_VERSION"],
                "trace": "enabled",
            }
            profile["bedrock_guardrail_unit_ceiling"] = 64
            updated = await api.patch(
                f"/api/v1/models/{model['id']}",
                headers=auth(root, model["version"]),
                json={"tool_dimensions": sorted(GUARDRAIL_DIMENSIONS)},
            )
            assert updated.status_code == 200
            dims = sorted({"input_uncached_tokens", "output_tokens"} | GUARDRAIL_DIMENSIONS)
            await publish_price(api, root, "sale", model, dimensions=dims)
            await publish_price(api, root, "cost", dep, "0.4", dimensions=dims)
        configured = await api.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={
                "region": region,
                "upstream_model": os.environ[prefix + "MODEL"],
                "relay_profile": profile,
                "credential_id": credential.json()["id"],
            },
        )
        assert configured.status_code == 200
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cluster,),
        ).fetchone()
        caps = [
            "reservation.v1",
            "usage-events.v1",
            "relay.native.v1",
            "relay.sse.v1",
            "relay.bedrock-converse.v1",
        ]
        if guardrail:
            caps += ["relay.bedrock-settings.v1", "relay.bedrock-guardrail-usage.v1"]
        if mode == "claude_invoke":
            caps.append("relay.bedrock-claude-invoke.v1")
        principal = await api.put(
            f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
            headers=auth(root),
            json={"node_id": str(node), "database_role": role, "capabilities": caps},
        )
        assert principal.status_code == 200
        release = await api.post(
            f"/api/v1/gateway-clusters/{cluster}/releases",
            headers=headers(root, "live-bedrock"),
            json={},
        )
        assert release.status_code == 202
        async with running_relay(
            relay_binary, peer, private, cluster, settings, tls[1], peer.connection.info.port
        ) as relay:
            prompt = "Reply OK."
            single = await relay.post(
                "/v1/messages",
                headers={"x-api-key": token},
                timeout=90,
                json={
                    "model": model["public_name"],
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            assert single.status_code == 200
            assert (
                single.json()["type"] == "message" and single.json()["usage"]["output_tokens"] > 0
            )
            stream = await relay.post(
                "/v1/responses",
                headers={"Authorization": "Bearer " + token},
                timeout=90,
                json={
                    "model": model["public_name"],
                    "max_output_tokens": 64,
                    "input": prompt,
                    "stream": True,
                },
            )
            assert stream.status_code == 200
            events = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            assert events[-1]["type"] == "response.completed"
            assert events[-1]["response"]["usage"]["output_tokens"] > 0
            await wait_events(admin, 4)
            assert await app.state.usage.process() == 2
            assert admin.execute(
                "SELECT count(*) FROM billing.request_runs "
                "WHERE reservation_state='captured' AND charged_amount>0"
            ).fetchone() == (2,)
            assert admin.execute(
                "SELECT count(*) FROM billing.request_attempts WHERE cost_amount>0"
            ).fetchone() == (2,)
            if guardrail:
                receipts = admin.execute(
                    "SELECT envelope->'payload'->'raw_usage' FROM billing.usage_events "
                    "WHERE kind='attempt.finished'"
                ).fetchall()
                assert len(receipts) == 2
                for (raw,) in receipts:
                    assert raw and "guardrail_usage_unverified" not in raw
                    assert sum(raw[k] for k in GUARDRAIL_DIMENSIONS) > 0
            if os.environ.get(prefix + "CITATIONS") == "1":
                excerpt = "The dispatch identifier is JUNIPER 752."
                prompt = {
                    "model": model["public_name"],
                    "max_tokens": 64,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "title": "Dispatch",
                                    "source": {
                                        "type": "text",
                                        "media_type": "text/plain",
                                        "data": excerpt,
                                    },
                                    "citations": {"enabled": True},
                                },
                                {
                                    "type": "text",
                                    "text": "Give the dispatch identifier and cite its source.",
                                },
                            ],
                        }
                    ],
                }
                for streaming in [False, True]:
                    result = await relay.post(
                        "/v1/messages",
                        headers={"x-api-key": token},
                        timeout=90,
                        json=prompt | {"stream": streaming},
                    )
                    assert result.status_code == 200
                    if streaming:
                        frames = [
                            json.loads(s[6:])
                            for s in result.text.splitlines()
                            if s.startswith("data: ")
                        ]
                        assert frames[-1]["type"] == "message_stop"
                        citations = [
                            f["delta"]["citation"]
                            for f in frames
                            if f.get("delta", {}).get("type") == "citations_delta"
                        ]
                    else:
                        content = result.json()["content"]
                        citations = [c for block in content for c in (block.get("citations") or [])]
                        prompt["messages"].extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": "Confirm that identifier and cite the same source.",
                                },
                            ]
                        )
                    assert citations
                    for citation in citations:
                        assert citation["type"] == "char_location"
                        assert citation["document_index"] == 0 and citation["cited_text"]
                        start, end = citation["start_char_index"], citation["end_char_index"]
                        assert 0 <= start < end <= len(excerpt)
                        assert excerpt[start:end] == citation["cited_text"]
                await wait_events(admin, 8)
                assert await app.state.usage.process() == 2
                assert admin.execute(
                    "SELECT count(*) FROM billing.request_runs "
                    "WHERE reservation_state='captured' AND charged_amount>0"
                ).fetchone() == (4,)
                assert admin.execute(
                    "SELECT count(*) FROM billing.request_attempts WHERE cost_amount>0"
                ).fetchone() == (4,)
