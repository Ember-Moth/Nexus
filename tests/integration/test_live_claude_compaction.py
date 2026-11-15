"""Two opt-in merchant calls to verify a signed compaction block and its replay."""

import json
import os
from uuid import UUID

import pytest
from test_billing import headers
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401

pytestmark = pytest.mark.integration
PREFIX = "RELAY_ACCEPTANCE_CLAUDE_COMPACTION_"


@pytest.mark.skipif(
    os.getenv(PREFIX + "RUN") != "1",
    reason="requires a Claude deployment with signed on-demand compaction",
)
async def test_signed_compaction_and_stateless_replay(http, admin, tls, relay_binary):  # noqa: F811
    api, root, app, settings = http
    cap = int(os.environ.get(PREFIX + "OUTPUT", "4096"))
    dims = [
        "input_uncached_tokens",
        "output_tokens",
        "input_cache_read_tokens",
        "input_cache_write_5m_tokens",
        "input_cache_write_1h_tokens",
    ]
    async with provision_plane(
        http,
        admin,
        "anthropic",
        balance="400000",
        base_url=os.environ[PREFIX + "URL"],
        dimensions=dims,
    ) as plane:
        peer, private, _, model, dep, _, key, cluster = plane
        credential = await api.post(
            f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
            headers=auth(root),
            json={"name": "native-compaction", "secret": os.environ[PREFIX + "KEY"]},
        )
        if credential.status_code != 201:
            pytest.fail("Unable to install the supplied Claude credential")
        deployment = await api.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={
                "upstream_model": os.environ[PREFIX + "MODEL"],
                "credential_id": credential.json()["id"],
                "relay_profile": {
                    "endpoint": "anthropic_messages",
                    "transports": ["http"],
                    "streaming": True,
                    "input_ceiling": model["max_input_tokens"],
                    "output_ceiling": cap,
                    "count_rule": "deployment_ceiling.v1",
                    "bound_reference": "isolated compaction acceptance wallet",
                },
            },
        )
        assert deployment.status_code == 200
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cluster,),
        ).fetchone()
        principal = await api.put(
            f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
            headers=auth(root),
            json={
                "node_id": str(node),
                "database_role": role,
                "capabilities": [
                    "reservation.v1",
                    "usage-events.v1",
                    "relay.native.v1",
                    "relay.sse.v1",
                ],
            },
        )
        assert principal.status_code == 200
        release = await api.post(
            f"/api/v1/gateway-clusters/{cluster}/releases",
            headers=headers(root, "native-compaction"),
            json={},
        )
        assert release.status_code == 202
        async with running_relay(
            relay_binary, peer, private, cluster, settings, tls[1], peer.connection.info.port
        ) as relay:
            h = {"x-api-key": key, "anthropic-beta": "compact-2026-09-04"}
            body = {"model": model["public_name"], "max_tokens": cap}
            first = await relay.post(
                "/v1/messages",
                headers=h,
                timeout=120,
                json=body
                | {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Retain the dispatch identifier JUNIPER-752.",
                        }
                    ],
                    "compaction": {"type": "summarize"},
                },
            )
            assert first.status_code == 200
            data = first.json()
            assert data["stop_reason"] == "compaction" and len(data["content"]) == 1
            block = data["content"][0]
            assert block["type"] == "compaction" and block["signature"] and block["content"]
            iterations = data["usage"]["iterations"]
            expected = sum(
                row["output_tokens"] for row in iterations if row["type"] == "compaction"
            )
            assert expected > 0
            second = await relay.post(
                "/v1/messages",
                headers=h,
                timeout=120,
                json=body
                | {
                    "stream": True,
                    "messages": [
                        {"role": "assistant", "content": [block]},
                        {"role": "user", "content": "Give only the dispatch identifier."},
                    ],
                },
            )
            assert second.status_code == 200
            events = [json.loads(x[6:]) for x in second.text.splitlines() if x.startswith("data: ")]
            assert events[-1]["type"] == "message_stop"
            answer = "".join(
                e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta"
            )
            assert "JUNIPER-752" in answer
            await wait_events(admin, 4)
            assert await app.state.usage.process() == 2
            ids = [UUID(r.headers["x-request-id"]) for r in (first, second)]
            for i, rid in enumerate(ids):
                raw = admin.execute(
                    "SELECT envelope->'payload'->'raw_usage' FROM billing.usage_events "
                    "WHERE request_id=%s AND kind='attempt.finished'",
                    (rid,),
                ).fetchone()[0]
                assert raw.get("compaction_output_tokens", 0) == (expected if i == 0 else 0)
                assert admin.execute(
                    """SELECT r.reservation_state='captured'
                    AND r.charged_amount>0 AND a.cost_amount>0 FROM billing.request_runs r
                    JOIN billing.request_attempts a ON a.request_id=r.id WHERE r.id=%s""",
                    (rid,),
                ).fetchone() == (True,)
