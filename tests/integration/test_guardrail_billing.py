"""Converse policy receipts, frozen holds, correction and tenant billing."""

import asyncio
import json
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from test_bedrock_relay import FREE_GUARDRAIL_USAGE, USAGE, frame
from test_billing import (
    account_amounts,
    funding,
    headers,
    make_budget,
    post_funding,
    publish_discount,
    publish_price,
)
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401

pytestmark = pytest.mark.integration

# Quotes mix single-unit and per-thousand tariffs from one frozen price.
RATES = {
    "input_uncached_tokens": ("1", 1),
    "output_tokens": ("1", 1),
    "guardrail_content_units": ("250", 1000),
    "guardrail_image_units": ("2", 1),
    "guardrail_topic_units": ("500", 1000),
    "guardrail_sensitive_units": ("750", 1000),
    "guardrail_grounding_units": ("1", 1),
    "guardrail_reasoning_units": ("2500", 1000),
}
EXPECTED = {
    "input_uncached_tokens": 2,
    "output_tokens": 3,
    "guardrail_content_units": 2,
    "guardrail_image_units": 1,
    "guardrail_topic_units": 1,
    "guardrail_sensitive_units": 1,
    "guardrail_grounding_units": 1,
    "guardrail_reasoning_units": 1,
}


async def test_assessments_hold_settle_review_and_reconcile(http, admin, tls, relay_binary):  # noqa: F811
    client, root, app, settings = http
    mode = "ok"
    sent = []
    marker = "private-assessment-marker"

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {k.lower(): v.strip() for row in lines[1:] if row for k, v in [row.split(":", 1)]}
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            sent.append(body)
            incoming = FREE_GUARDRAIL_USAGE | {
                "contentPolicyUnits": 1,
                "contentPolicyImageUnits": 1,
                "topicPolicyUnits": 1,
                "automatedReasoningPolicyUnits": 1,
                "automatedReasoningPolicies": 1,
            }
            outgoing = FREE_GUARDRAIL_USAGE | {
                "contentPolicyUnits": 1,
                "sensitiveInformationPolicyUnits": 1,
                "contextualGroundingPolicyUnits": 1,
            }
            if mode == "multi":
                incoming["automatedReasoningPolicies"] = 2
            if mode == "over":
                outgoing["contentPolicyUnits"] = 4
            if mode == "unknown":
                outgoing["newFilterUnits"] = 2
            trace = {
                "guardrail": {
                    "modelOutput": [marker],
                    "inputAssessment": {
                        "policy": {
                            "contentPolicy": {"filters": [{"match": marker}]},
                            "invocationMetrics": {"usage": incoming},
                        }
                    },
                    "outputAssessments": {"policy": [{"invocationMetrics": {"usage": outgoing}}]},
                }
            }
            meta = {"usage": USAGE}
            if mode != "missing":
                meta["trace"] = trace
            if "/converse-stream " in lines[0]:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.amazon.eventstream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                for kind, obj in [
                    ("messageStart", {"role": "assistant"}),
                    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "ok"}}),
                    ("contentBlockStop", {"contentBlockIndex": 0}),
                    ("messageStop", {"stopReason": "end_turn"}),
                    ("metadata", meta),
                ]:
                    writer.write(frame(kind, obj))
            else:
                reply = {
                    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                    "stopReason": "end_turn",
                    **meta,
                }
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\n\r\n" + json.dumps(reply).encode()
                )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    port = server.sockets[0].getsockname()[1]
    async with (
        server,
        provision_plane(
            http, admin, "aws_bedrock", balance="20", base_url=f"https://127.0.0.1:{port}"
        ) as plane,
    ):
        peer, private, org, model, dep, _, token, cluster = plane
        dims = [k for k in RATES if k.startswith("guardrail_")]
        patch = await client.patch(
            f"/api/v1/models/{model['id']}",
            headers=auth(root, model["version"]),
            json={"tool_dimensions": dims},
        )
        assert patch.status_code == 200, patch.text
        for kind, target, factor in [("sale", model, Decimal(1)), ("cost", dep, Decimal("0.4"))]:
            path = f"/api/v1/{kind}-price-versions"
            price = await client.post(
                path,
                headers=headers(root, uuid4().hex),
                json={
                    ("model_id" if kind == "sale" else "deployment_id"): target["id"],
                    "currency": "USD",
                    "rates": {
                        k: {"unit_price": str(Decimal(n) * factor), "unit_quantity": units}
                        for k, (n, units) in RATES.items()
                    },
                },
            )
            assert price.status_code == 201, price.text
            v = price.json()
            assert (
                await client.post(
                    f"{path}/{v['id']}/publish",
                    headers=headers(root, uuid4().hex, v["version"]),
                    json={},
                )
            ).status_code == 200
        await publish_discount(client, root, org, "0.8")
        budget = await make_budget(client, root, org, "10")
        profile = {
            "endpoint": "bedrock_converse",
            "transports": ["http"],
            "streaming": True,
            "tools": False,
            "input_ceiling": 4,
            "output_ceiling": 6,
            "count_rule": "deployment_ceiling.v1",
            "bound_reference": "controlled Guardrail assessments",
            "bedrock_guardrail_unit_ceiling": 4,
            "bedrock_guardrail": {
                "guardrailIdentifier": "test",
                "guardrailVersion": "1",
                "trace": "enabled",
            },
        }
        changed = await client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={"region": "us-east-1", "upstream_model": "test", "relay_profile": profile},
        )
        assert changed.status_code == 200, changed.text
        model_view = (await client.get(f"/api/v1/models/{model['id']}", headers=auth(root))).json()
        removed = await client.patch(
            f"/api/v1/models/{model['id']}",
            headers=auth(root, model_view["version"]),
            json={"tool_dimensions": []},
        )
        assert (
            removed.status_code == 422
            and removed.json()["error"]["code"] == "price_dimensions_mismatch"
        )
        node, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cluster,),
        ).fetchone()
        caps = [
            "reservation.v1",
            "usage-events.v1",
            "relay.native.v1",
            "relay.provider-features.v1",
            "relay.sse.v1",
            "relay.bedrock-converse.v1",
            "relay.bedrock-settings.v1",
            "relay.bedrock-guardrail-usage.v1",
        ]
        changed = await client.put(
            f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
            headers=auth(root),
            json={"node_id": str(node), "database_role": role, "capabilities": caps},
        )
        assert changed.status_code == 200
        release = await client.post(
            f"/api/v1/gateway-clusters/{cluster}/releases",
            headers=headers(root, uuid4().hex),
            json={},
        )
        assert release.status_code == 202, release.text
        async with running_relay(
            relay_binary, peer, private, cluster, settings, tls[1], peer.connection.info.port
        ) as relay:

            async def generate(fixture, stream=False):
                nonlocal mode
                mode = fixture
                res = await relay.post(
                    "/v1/messages",
                    headers={"x-api-key": token},
                    json={
                        "model": model["public_name"],
                        "max_tokens": 6,
                        "messages": [{"role": "user", "content": "ok"}],
                        "stream": stream,
                    },
                )
                assert res.status_code == 200, res.text
                assert marker not in res.text
                rid = UUID(res.headers["x-request-id"])
                await wait_events(admin, len(sent) * 2)
                await app.state.usage.process()
                return rid

            async def resolve(rid, kind, quantities, reference):
                case = admin.execute(
                    "SELECT id,version FROM billing.review_cases "
                    "WHERE request_id=%s AND source_type=%s AND state='open'",
                    (rid, kind),
                ).fetchone()
                aid = admin.execute(
                    "SELECT id FROM billing.request_attempts WHERE request_id=%s", (rid,)
                ).fetchone()[0]
                return await client.post(
                    f"/api/v1/reconciliation-cases/{case[0]}/resolve",
                    headers=headers(root, uuid4().hex, case[1]),
                    json={
                        "action": "supplement_usage",
                        "reason": "Supplier assessment invoice",
                        "evidence": reference,
                        "attempt_id": str(aid),
                        "normalization_version": "supplier-units.v1",
                        "quantities": quantities,
                    },
                )

            def financials(rid):
                return admin.execute(
                    """SELECT r.guardrail_unit_limit,r.reserved_amount,r.charged_amount,
                    coalesce((SELECT c.amount FROM billing.cost_corrections c
                              WHERE c.attempt_id=a.id
                              ORDER BY c.created_at DESC LIMIT 1),a.cost_amount),r.state
                    FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
                    WHERE r.id=%s""",
                    (rid,),
                ).fetchone()

            async def denied(code):
                res = await relay.post(
                    "/v1/messages",
                    headers={"x-api-key": token},
                    json={
                        "model": model["public_name"],
                        "max_tokens": 6,
                        "messages": [{"role": "user", "content": "ok"}],
                    },
                )
                assert res.status_code == 402 and code in res.text, res.text
                assert not sent and admin.execute(
                    "SELECT count(*) FROM billing.request_runs"
                ).fetchone() == (0,)

            await denied("insufficient_funds")
            receiver = {
                "id": str(
                    admin.execute("SELECT id FROM billing.receiving_accounts LIMIT 1").fetchone()[0]
                )
            }
            record = await funding(client, root, org, receiver, "980")
            await post_funding(client, root, org, record)
            await denied("budget_exceeded")
            raised = await client.patch(
                f"/api/v1/organizations/{org['id']}/budget-policies/{budget['id']}",
                headers=auth(root, budget["version"]),
                json={"limit_amount": "800"},
            )
            assert raised.status_code == 200, raised.text
            for streaming in [False, True]:
                rid = await generate("ok", streaming)
                assert financials(rid) == (
                    4,
                    Decimal("30.4"),
                    Decimal("9.8"),
                    Decimal("4.9"),
                    "settled",
                )
                e = admin.execute(
                    "SELECT envelope FROM billing.usage_events "
                    "WHERE request_id=%s AND kind='attempt.finished'",
                    (rid,),
                ).fetchone()[0]
                raw = e["payload"]["raw_usage"]
                assert (
                    raw["guardrail_word_units"] == 2 and raw["guardrail_sensitive_free_units"] == 2
                )
                assert {k: raw[k] for k in dims} == {k: EXPECTED[k] for k in dims}
                assert marker not in json.dumps(e) and marker not in relay.relay_log.read_text()
                assert await app.state.usage.process() == 0
            record = await client.get(
                f"/api/v1/organizations/{org['id']}/requests/{rid}", headers=auth(root)
            )
            assert record.status_code == 200, record.text
            assert record.json()["guardrail_unit_limit"] == 4
            assert account_amounts(admin, org) == (Decimal("980.4"), 0)

            revision = int(release.json()["revision"])
            test_id = uuid4()
            base = (test_id, plane[5].api_key_id, plane[3]["id"], revision, 4, 6)
            with pytest.raises(psycopg.errors.RaiseException, match="deployment_unavailable"):
                await peer.connection.execute(
                    "SELECT gateway_v1.reserve_guardrail_request(%s,%s,%s,%s,%s,%s,%s,3)",
                    (*base, dep["id"]),
                )
            assert admin.execute(
                "SELECT count(*) FROM billing.request_runs WHERE id=%s", (test_id,)
            ).fetchone() == (0,)
            replay = await peer.connection.execute(
                "SELECT gateway_v1.reserve_guardrail_request(%s,%s,%s,%s,%s,%s,%s,4)",
                (rid, *base[1:], dep["id"]),
            )
            assert (await replay.fetchone())[0]["replayed"] is True
            with pytest.raises(psycopg.errors.RaiseException, match="request_conflict"):
                await peer.connection.execute(
                    "SELECT gateway_v1.reserve_request(%s,%s,%s,%s,%s,%s)", (rid, *base[1:])
                )
            for fixture in ["missing", "multi", "unknown"]:
                invoice = EXPECTED | {"guardrail_reasoning_units": 2 if fixture == "multi" else 1}
                expected_amounts = (
                    (Decimal("11.8"), Decimal("5.9"))
                    if fixture == "multi"
                    else (Decimal("9.8"), Decimal("4.9"))
                )
                rid = await generate(fixture)
                if fixture == "unknown":
                    revised = {
                        k: str(3 * Decimal(price) / units) for k, (price, units) in RATES.items()
                    }
                    await publish_price(client, root, "sale", model, dimensions=revised)
                    await publish_price(client, root, "cost", dep, "0.4", dimensions=revised)
                assert financials(rid)[-1] == "review"
                assert financials(rid)[3] is None
                assert (
                    await resolve(rid, "request", invoice | {"output_tokens": 4}, fixture)
                ).status_code == 409
                if fixture != "missing":
                    assert (await resolve(rid, "request", invoice, "missing")).status_code == 409
                for kind in ["attempt", "request"]:
                    fixed = await resolve(rid, kind, invoice, fixture)
                    assert fixed.status_code == 200, fixed.text
                assert financials(rid)[2:4] == expected_amounts
                await app.state.usage.process()
            rid = await generate("over")
            assert admin.execute(
                "SELECT review_reason,reservation_state FROM billing.request_runs WHERE id=%s",
                (rid,),
            ).fetchone() == ("authorized_bound_exceeded", "held")
