import csv
import io
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from test_billing import (
    adjustment,
    balance,
    headers,
    make_budget,
    settle_success,
    setup_model,
    setup_wallet,
)
from test_identity_api import auth, create_user, login
from test_plane import plane  # noqa: F401
from test_project_credentials import developer, project

pytestmark = pytest.mark.integration


async def test_usage_project_projection_finance_and_export(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, a = await setup_model(http, org)
    await app.state.reservations.reserve(a)
    _, facts, attempt = await settle_success(app, a, dep)
    base = f"/api/v1/organizations/{org['id']}"
    user = await create_user(client, root, "report-finance")
    assert (
        await client.post(
            base + "/members",
            headers=auth(root),
            json={"user_id": user["user"]["id"], "role": "org_finance"},
        )
    ).status_code == 201
    finance = await login(client, "report-finance")
    dev, _, grant = await developer(client, root, org, str(a.project_id))
    other = await project(client, root, org, "Private project")
    for url in ["/requests", "/charges"]:
        response = await client.get(base + url, headers=auth(dev))
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        assert "cost_amount" not in response.text
        assert (
            await client.get(base + url, headers=auth(dev), params={"project_id": other["id"]})
        ).status_code == 404
    assert (await client.get(base + "/ledger", headers=auth(dev))).status_code == 403
    assert (await client.get(base + "/upstream-costs", headers=auth(finance))).status_code == 403
    assert (
        await client.get(base + "/usage?include_cost=true", headers=auth(finance))
    ).status_code == 403
    await app.state.reports.project()
    report = await client.get(base + "/usage?include_cost=true", headers=auth(root))
    assert report.status_code == 200, report.text
    row = report.json()["items"][0]
    assert Decimal(row["charged_amount"]) == 5 and row["provisional"]
    await app.state.reservations.record_cost(
        a.organization_id, a.request_id, attempt, facts.quantities, "cost-evidence"
    )
    await app.state.reports.project()
    report = (await client.get(base + "/usage?include_cost=true", headers=auth(root))).json()
    assert Decimal(report["items"][0]["margin"]) == 3 and not report["items"][0]["provisional"]
    customer = (await client.get(base + "/usage", headers=auth(finance))).json()
    assert "margin" not in customer["items"][0]
    assert customer["watermark"]["pending_requests"] == 0
    result = await client.post(base + "/exports", headers=auth(finance), json={"kind": "charges"})
    assert result.status_code == 202, result.text
    path = base + "/exports/" + result.json()["id"]
    await app.state.reports.process_export()
    response = await client.get(path + "/download", headers=auth(finance))
    assert response.status_code == 200, response.text
    assert str(a.request_id) in response.text and "cost_amount" not in response.text
    member = (await client.get(base + "/members", headers=auth(root))).json()["items"]
    m = next(m for m in member if m["user_id"] == user["user"]["id"])
    assert (
        await client.delete(base + "/members/" + m["id"], headers=auth(root, m["version"]))
    ).status_code == 204
    assert (await client.get(path + "/download", headers=auth(finance))).status_code == 404
    assert (await client.get("/internal/metrics", headers=auth(dev))).status_code == 403
    metrics = await client.get("/internal/metrics", headers=auth(root))
    assert metrics.status_code == 200, metrics.text
    assert org["id"] not in metrics.text
    path = "/internal/metrics/prometheus"
    assert (await client.get(path, headers=auth(dev))).status_code == 403
    text = await client.get(path, headers=auth(root))
    assert text.status_code == 200, text.text
    assert text.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "\nnexus_billing_usage_backlog " in text.text and org["id"] not in text.text


async def test_review_waiver_replay_and_unfreeze(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, a = await setup_model(http, org)
    await make_budget(client, root, org, "10")
    await app.state.reservations.reserve(a)
    result, _, attempt = await settle_success(
        app, a, dep, {"input_uncached_tokens": 4, "output_tokens": 8}
    )
    assert result.state == "review"
    queue = await client.get("/api/v1/reconciliation-cases", headers=auth(root))
    assert queue.status_code == 200, queue.text
    case = next(c for c in queue.json()["items"] if c["request_id"] == str(a.request_id))
    command = {
        "action": "waive",
        "reason": "Agreed overage waiver",
        "evidence": "support-ticket-7",
        "attempt_id": str(attempt),
        "quantities": {"input_uncached_tokens": 4, "output_tokens": 8},
        "normalization_version": "review.v1",
        "waiver": "2",
    }
    url = "/api/v1/reconciliation-cases/" + case["id"] + "/resolve"
    h = headers(root, "resolve-request", case["version"])
    response = await client.post(url, headers=h, json=command)
    assert response.status_code == 200, response.text
    assert response.json()["charged_amount"] == "10.000000000000"
    assert (await client.post(url, headers=h, json=command)).json() == response.json()
    assert (
        await client.post(url, headers=h, json={**command, "reason": "changed"})
    ).status_code == 409
    assert (await balance(client, root, org))["balance"] == "90.000000000000"
    assert admin.execute("SELECT spent,reserved FROM billing.budget_periods").fetchone() == (10, 0)
    await app.state.usage.process()
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone()[0]
        == 1
    )
    account = await balance(client, root, org)
    reply = await client.post(
        f"/api/v1/organizations/{org['id']}/account/unfreeze",
        headers=headers(root, "unfreeze", account["version"]),
        json={"reason": "Verified resolution", "evidence": "support-ticket-7"},
    )
    assert reply.status_code == 200, reply.text
    assert (await balance(client, root, org))["status"] == "active"
    detail = (await client.get(url.removesuffix("/resolve"), headers=auth(root))).json()
    assert detail["decisions"][0]["evidence"] == "support-ticket-7"


async def test_cost_statement_correction_preserves_original_and_customer_charge(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, a = await setup_model(http, org)
    await app.state.reservations.reserve(a)
    _, facts, attempt = await settle_success(app, a, dep)
    await app.state.reservations.record_cost(
        a.organization_id, a.request_id, attempt, facts.quantities, "original-evidence"
    )
    base = f"/api/v1/organizations/{org['id']}"
    response = await client.post(
        "/api/v1/reconciliation-cases/upstream-statements",
        headers=headers(root, "statement"),
        json={
            "organization_id": org["id"],
            "attempt_id": str(attempt),
            "external_reference": "invoice-12",
            "amount": "3.5",
            "currency": "USD",
            "reason": "Supplier invoice difference",
            "evidence": "Invoice line 7",
        },
    )
    assert response.status_code == 201, response.text
    statement = response.json()["statement_id"]
    cases = (await client.get("/api/v1/reconciliation-cases", headers=auth(root))).json()["items"]
    case = next(c for c in cases if c["source_id"] == str(attempt))
    reply = await client.post(
        f"/api/v1/reconciliation-cases/{case['id']}/resolve",
        headers=headers(root, "confirm-cost", case["version"]),
        json={
            "action": "confirm_cost",
            "statement_id": statement,
            "reason": "Invoice verified",
            "evidence": "finance-ref-12",
        },
    )
    assert reply.status_code == 200, reply.text
    assert (
        admin.execute(
            "SELECT cost_amount FROM billing.request_attempts WHERE id=%s", (attempt,)
        ).fetchone()[0]
        == 2
    )
    assert (await balance(client, root, org))["balance"] == "95.000000000000"
    await app.state.reports.project()
    report = (await client.get(base + "/usage?include_cost=true", headers=auth(root))).json()[
        "items"
    ][0]
    assert Decimal(report["confirmed_cost"]) == Decimal("3.5")
    costs = await client.get(base + "/upstream-costs", headers=auth(root))
    assert costs.status_code == 200, costs.text
    assert sorted(Decimal(r["cost_amount"]) for r in costs.json()["items"]) == [
        Decimal("1.5"),
        Decimal(2),
    ]


async def test_refund_uses_its_own_posting_period_and_stable_pagination(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, a = await setup_model(http, org)
    await app.state.reservations.reserve(a)
    await settle_success(app, a, dep)
    since = admin.execute("SELECT clock_timestamp()").fetchone()[0].isoformat()
    source = admin.execute(
        "SELECT id FROM billing.ledger_entries WHERE kind='usage_debit'"
    ).fetchone()[0]
    refund = await adjustment(client, root, org, str(source), "2")
    result = await client.post(
        f"/api/v1/organizations/{org['id']}/adjustments/{refund['id']}/post",
        headers=headers(root, "refund", 1),
    )
    assert result.status_code == 200, result.text
    await app.state.reports.project()
    base = f"/api/v1/organizations/{org['id']}"
    params = {"start": since, "time_basis": "posted_at"}
    period = await client.get(base + "/usage", params=params, headers=auth(root))
    assert period.status_code == 200, period.text
    line = period.json()["items"][0]
    assert Decimal(line["charged_amount"]) == 0 and Decimal(line["net_charge"]) == -2
    assert (await client.get(base + "/charges", params=params, headers=auth(root))).json()["items"][
        0
    ]["kind"] == "refund"
    first = (await client.get(base + "/charges?limit=1", headers=auth(root))).json()
    second = (
        await client.get(
            base + "/charges",
            params={"cursor": first["next_cursor"], "limit": 1},
            headers=auth(root),
        )
    ).json()
    assert first["items"][0]["kind"] == "charge" and second["items"][0]["kind"] == "refund"
    assert not second["next_cursor"]
    export = await client.post(
        base + "/exports", headers=auth(root), json={"kind": "charges", "filters": params}
    )
    assert export.status_code == 202, export.text
    await app.state.reports.process_export()
    download = await client.get(
        base + f"/exports/{export.json()['id']}/download", headers=auth(root)
    )
    assert download.status_code == 200, download.text
    [refund_row] = csv.DictReader(io.StringIO(download.content.decode("utf-8-sig")))
    assert refund_row["kind"] == "refund" and Decimal(refund_row["amount"]) == -2


async def test_projection_crash_replays_and_export_is_fenced_and_expires(http, admin, monkeypatch):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, a = await setup_model(http, org)
    await app.state.reservations.reserve(a)
    reports = app.state.reports

    async def lost_ack(rows):
        raise RuntimeError("lost projection acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(reports.source, "acknowledge", lost_ack)
        with pytest.raises(RuntimeError):
            await reports.project()
    assert await reports.project() == 1
    assert admin.execute("SELECT count(*) FROM reporting.request_facts").fetchone()[0] == 1
    base = f"/api/v1/organizations/{org['id']}/exports"
    reports.rate = 1
    out = await client.post(base, headers=auth(root), json={"kind": "ledger"})
    assert out.status_code == 202, out.text
    assert (await client.post(base, headers=auth(root), json={"kind": "ledger"})).status_code == 429
    async with reports.tx() as repo:
        old = await repo.claim()
    admin.execute(
        "UPDATE reporting.export_jobs SET lease_until=clock_timestamp()-interval '1 second'"
    )
    await reports.process_export()
    async with reports.tx() as repo:
        await repo.finish_export(old, b"obsolete", 1)
    path = base + "/" + out.json()["id"] + "/download"
    assert b"funding_credit" in (await client.get(path, headers=auth(root))).content
    admin.execute(
        "UPDATE reporting.export_jobs SET expires_at=clock_timestamp()-interval '1 second'"
    )
    assert (await client.get(path, headers=auth(root))).status_code == 409
    await reports.process_export()
    assert admin.execute("SELECT state,content FROM reporting.export_jobs").fetchone() == (
        "expired",
        None,
    )


async def test_integrity_case_requires_recheck_and_audit_is_redacted(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    admin.execute(
        "UPDATE billing.accounts SET balance=balance+1 WHERE organization_id=%s", (org["id"],)
    )
    accounts = admin.execute("SELECT count(*) FROM billing.accounts").fetchone()[0]
    for _ in range(accounts):
        assert await app.state.operations.check_next() == 1
    assert await app.state.operations.check_next() == 0
    queue = (await client.get("/api/v1/reconciliation-cases", headers=auth(root))).json()["items"]
    case = next(
        c for c in queue if c["source_type"] == "account" and c["organization_id"] == org["id"]
    )
    url = "/api/v1/reconciliation-cases/" + case["id"] + "/resolve"
    command = {"action": "retry", "reason": "Source data reviewed", "evidence": "ops-ticket"}
    h = headers(root, "integrity-recheck", case["version"])
    assert (await client.post(url, headers=h, json=command)).status_code == 409
    admin.execute(
        "UPDATE billing.accounts SET balance=balance-1 WHERE organization_id=%s", (org["id"],)
    )
    assert (await client.post(url, headers=h, json=command)).status_code == 200
    admin.execute(
        """INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details)
        VALUES(gen_random_uuid(),%s,'test.redaction',gen_random_uuid(),
        '{"before":{"digest":"secret-value"},"reason":"Bearer secret-value"}')""",
        (org["id"],),
    )
    audit = await client.get(
        "/api/v1/audit-events",
        params={"organization_id": org["id"], "action": "test.redaction"},
        headers=auth(root),
    )
    assert audit.status_code == 200 and len(audit.json()["items"]) == 1
    assert "secret-value" not in audit.text


async def test_no_charge_does_not_absorb_a_late_attempt_cost(plane, http, admin):  # noqa: F811
    peer, _, org, model, dep, a, raw, _ = plane
    client, root, app, _ = http
    await peer.refresh()
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    attempt = uuid4()
    await peer.call("start_attempt", a.request_id, attempt, 1, UUID(dep["id"]))
    await peer.event(
        a.request_id,
        "request.closed",
        {
            "delivery_outcome": "unknown",
            "attempt_ids": [str(attempt)],
            "delivered_attempt_id": None,
        },
    )
    await app.state.usage.process()
    cases = (await client.get("/api/v1/reconciliation-cases", headers=auth(root))).json()["items"]
    case = next(c for c in cases if c["source_type"] == "request")
    result = await client.post(
        "/api/v1/reconciliation-cases/" + case["id"] + "/resolve",
        headers=headers(root, "no-charge", case["version"]),
        json={
            "action": "confirm_no_charge",
            "reason": "Delivery failed",
            "evidence": "verified incident",
        },
    )
    assert result.status_code == 200, result.text
    await peer.event(
        a.request_id,
        "attempt.finished",
        {
            "outcome": "failed",
            "elapsed_ms": 22,
            "upstream_request_id": "late",
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {"input_total_tokens": 2, "cached_input_tokens": 0, "output_tokens": 1},
        },
        attempt_id=attempt,
    )
    await app.state.usage.process()
    assert admin.execute(
        "SELECT state,cost_amount FROM billing.request_attempts WHERE id=%s", (attempt,)
    ).fetchone() == ("confirmed", Decimal("1.2"))
    assert (await balance(client, root, org))["balance"] == "100.000000000000"
    assert (
        admin.execute("SELECT count(*) FROM billing.review_cases WHERE state='open'").fetchone()[0]
        == 0
    )
