import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from test_catalog import make_credential, make_deployment, make_model, make_policy, make_upstream
from test_identity_api import auth, create_org, create_user, login
from test_project_credentials import account as service_account
from test_project_credentials import key as api_key
from test_project_credentials import project

from nexus.bootstrap import Application
from nexus.modules.billing import Admission, Settlement
from nexus.modules.billing.persistence.repository import BillingRepository
from nexus.shared.errors import ServiceError

pytestmark = pytest.mark.integration


def account_amounts(connection, organization):
    return connection.execute(
        "SELECT balance,held_amount FROM billing.accounts WHERE organization_id=%s",
        (organization["id"],),
    ).fetchone()


def headers(token, key=None, version=None):
    result = auth(token, version)
    if key is not None:
        result["Idempotency-Key"] = key
    return result


async def finance_role(client, root, user_id=None, roles=None):
    if user_id is None:
        user_id = (await client.get("/api/v1/me", headers=auth(root))).json()["user"]["id"]
    current = await client.get(f"/api/v1/users/{user_id}/platform-roles", headers=auth(root))
    assert current.status_code == 200, current.text
    response = await client.patch(
        f"/api/v1/users/{user_id}/platform-roles",
        headers=auth(root, current.json()["version"]),
        json={"roles": roles or ["platform_admin", "platform_finance"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def receiving(client, token, reference="bank-usd"):
    response = await client.post(
        "/api/v1/receiving-accounts",
        headers=headers(token, reference),
        json={"name": "USD bank", "reference": reference, "currency": "USD"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def funding(client, token, org, receiver, amount="100", reference=None):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/funding-records",
        headers=headers(token, uuid4().hex),
        json={
            "receiving_account_id": receiver["id"],
            "external_reference": reference or uuid4().hex,
            "amount": amount,
            "currency": "USD",
            "received_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "evidence": "bank statement test fixture",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def post_funding(client, token, org, record, key=None):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/funding-records/{record['id']}/post",
        headers=headers(token, key or uuid4().hex, record["version"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def publish_price(client, root, kind, target, amount="1", *, dimensions=None):
    path = "/api/v1/" + kind + "-price-versions"
    body = {
        "currency": "USD",
        "rates": {
            dimension: {
                "unit_price": amount
                if not isinstance(dimensions, dict)
                else str(Decimal(amount) * Decimal(dimensions[dimension])),
                "unit_quantity": 1,
            }
            for dimension in dimensions or ("input_uncached_tokens", "output_tokens")
        },
    }
    body["model_id" if kind == "sale" else "deployment_id"] = target["id"]
    result = await client.post(path, headers=headers(root, uuid4().hex), json=body)
    assert result.status_code == 201, result.text
    record = result.json()
    result = await client.post(
        path + "/" + record["id"] + "/publish",
        headers=headers(root, uuid4().hex, record["version"]),
        json={},
    )
    assert result.status_code == 200, result.text
    return result.json()


async def publish_discount(client, root, org, factor):
    path = f"/api/v1/organizations/{org['id']}/discount-versions"
    response = await client.post(path, headers=headers(root, uuid4().hex), json={"factor": factor})
    assert response.status_code == 201, response.text
    row = response.json()
    response = await client.post(
        path + "/" + row["id"] + "/publish",
        headers=headers(root, uuid4().hex, row["version"]),
        json={},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def setup_wallet(http, amount="100"):
    client, root, app, _ = http
    await finance_role(client, root)
    org = await create_org(client, root, "Billing enterprise")
    await app.state.process_billing()
    receiver = await receiving(client, root)
    record = await funding(client, root, org, receiver, amount)
    await post_funding(client, root, org, record)
    return org, receiver, record


async def setup_model(
    http, org, protocol="openai_compatible", *, dimensions=None, **upstream_options
):
    client, root, _, _ = http
    model = await make_model(client, root, "billable-model", [protocol], dimensions=dimensions)
    upstream = await make_upstream(client, root, "Billing upstream", protocol, **upstream_options)
    credential = await make_credential(client, root, upstream)
    deployment = await make_deployment(client, root, model, upstream, credential)
    sale = await publish_price(client, root, "sale", model, dimensions=dimensions)
    cost = await publish_price(client, root, "cost", deployment, "0.4", dimensions=dimensions)
    await make_policy(client, root, org, [model["id"]])
    proj = await project(client, root, org, "metered-project")
    sa = await service_account(client, root, org, proj["id"])
    key = (await api_key(client, root, org, sa["id"])).json()
    admission = Admission(
        request_id=uuid4(),
        organization_id=org["id"],
        project_id=proj["id"],
        service_account_id=sa["id"],
        api_key_id=key["key"]["id"],
        model_id=model["id"],
        input_limit=4,
        output_limit=6,
    )
    return model, deployment, sale, cost, admission


async def balance(client, token, org):
    result = await client.get(f"/api/v1/organizations/{org['id']}/account", headers=auth(token))
    assert result.status_code == 200, result.text
    return result.json()


async def settle_success(app, admission, deployment, quantities=None):
    attempt = uuid4()
    await app.state.reservations.start_attempt(
        admission.organization_id, admission.request_id, attempt, UUID(deployment["id"])
    )
    facts = Settlement(
        outcome="success",
        delivered_attempt_id=attempt,
        quantities=quantities or {"input_uncached_tokens": 2, "output_tokens": 3},
        evidence_reference="test-final",
        normalization_version="test.normalization.v1",
    )
    result = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, facts
    )
    return result, facts, attempt


async def test_account_provisioning_is_replayable_and_keeps_suspension(http, admin):
    client, root, app, settings = http
    org = await create_org(client, root, "waiting-account")
    path = f"/api/v1/organizations/{org['id']}"
    assert (await client.get(path + "/account", headers=auth(root))).status_code == 409
    events = await app.state.identity_service.pending_organizations()
    event = next(event for event in events if str(event.organization_id) == org["id"])
    ids = await asyncio.gather(
        *(app.state.billing_service.provision_account(event) for _ in range(6))
    )
    assert len(set(ids)) == 1
    assert (await client.get(path, headers=auth(root))).json()["status"] == "provisioning"
    response = await client.patch(path, headers=auth(root, 1), json={"status": "suspended"})
    assert response.status_code == 200
    ready = (await app.state.billing_service.pending_accounts())[0]
    await app.state.identity_service.account_ready(
        ready.event_id,
        ready.organization_id,
        ready.account_id,
        schema_version=ready.schema_version,
    )
    # Simulate a crash before acknowledging the committed activation event.
    await app.state.process_billing()
    await app.state.process_billing()
    result = (await client.get(path, headers=auth(root))).json()
    assert result["status"] == "suspended" and result["version"] == 3
    result = await client.patch(path, headers=auth(root, 3), json={"status": "active"})
    assert result.status_code == 200, result.text
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.accounts WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM identity.inbox WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        == 1
    )
    changed_currency = Application(settings.model_copy(update={"billing_currency": "CNY"}))
    try:
        assert (await changed_currency.readiness())["billing_currency"] == "unavailable"
    finally:
        await changed_currency.close()


async def test_finance_permissions_and_role_updates(http):
    client, root, app, _ = http
    owner = await create_user(client, root, "finance-owner")
    await create_org(client, root, "Finance company", owner["user"]["id"])
    await app.state.process_billing()
    root_id = (await client.get("/api/v1/me", headers=auth(root))).json()["user"]["id"]
    path = "/api/v1/users/" + root_id + "/platform-roles"
    response = await client.patch(path, headers=auth(root, 1), json={"roles": ["platform_finance"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "last_administrator"
    payload = {"name": "bank", "reference": "bank-a", "currency": "USD"}
    assert (
        await client.post("/api/v1/receiving-accounts", headers=headers(root, "r"), json=payload)
    ).status_code == 403
    owner_token = await login(client, "finance-owner")
    assert (await client.get(path, headers=auth(owner_token))).status_code == 403
    assert (
        await client.get("/api/v1/cost-price-versions", headers=auth(owner_token))
    ).status_code == 403
    await finance_role(client, root, owner["user"]["id"], ["platform_finance"])
    assert (
        await client.post(
            "/api/v1/receiving-accounts", headers=headers(owner_token, "r"), json=payload
        )
    ).status_code == 201
    assert (
        await client.get("/api/v1/cost-price-versions", headers=auth(owner_token))
    ).status_code == 403
    assert (
        await client.patch(path, headers=auth(root, 99), json={"roles": ["platform_admin"]})
    ).status_code == 412
    # Last-admin protection serializes concurrent revocations, with fresh actor roles.
    await finance_role(client, root, owner["user"]["id"], ["platform_admin", "platform_finance"])
    me = (await client.get("/api/v1/me", headers=auth(owner_token))).json()
    targets = [(root, root_id, 1), (owner_token, owner["user"]["id"], me["user"]["version"])]
    responses = await asyncio.gather(
        *(
            client.patch(
                "/api/v1/users/" + uid + "/platform-roles",
                headers=auth(token, version),
                json={"roles": ["platform_finance"]},
            )
            for token, uid, version in targets
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]


async def test_funding_business_uniqueness_replay_and_post_commit_cancellation(
    http, admin, monkeypatch
):
    client, root, app, _ = http
    org, receiver, _ = await setup_wallet(http, "10")
    record = await funding(client, root, org, receiver, "5", "one-receipt")
    url = f"/api/v1/organizations/{org['id']}/funding-records/{record['id']}/post"
    replies = await asyncio.gather(
        *(client.post(url, headers=headers(root, "same-post", 1)) for _ in range(8))
    )
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert (await balance(client, root, org))["balance"] == "15.000000000000"
    assert (await client.post(url, headers=headers(root, "same-post", 2))).status_code == 409
    other = await create_org(client, root, "Other company")
    await app.state.process_billing()
    data = {
        name: record[name]
        for name in (
            "receiving_account_id",
            "external_reference",
            "amount",
            "currency",
            "received_at",
            "evidence",
        )
    }
    duplicate = await client.post(
        f"/api/v1/organizations/{other['id']}/funding-records",
        headers=headers(root, "different-org"),
        json=data,
    )
    assert duplicate.status_code == 409
    # Failure after commit is an unknown response outcome, not permission to credit again.
    lost = await funding(client, root, org, receiver, "7")
    service = app.state.billing_service
    original = service._transactions

    @asynccontextmanager
    async def lose_response():
        async with original() as repo:
            yield repo
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_transactions", lose_response)
    with pytest.raises(asyncio.CancelledError):
        await service.post_funding(root, UUID(org["id"]), UUID(lost["id"]), 1, "lost-response")
    monkeypatch.setattr(service, "_transactions", original)
    replayed = await service.post_funding(
        root, UUID(org["id"]), UUID(lost["id"]), 1, "lost-response"
    )
    assert replayed.status == "posted"
    assert (await balance(client, root, org))["balance"] == "22.000000000000"
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE source_id=%s", (lost["id"],)
        ).fetchone()[0]
        == 1
    )


async def test_funding_failure_rolls_back_ledger_and_idempotency(http, admin, monkeypatch):
    client, root, app, _ = http
    org, receiver, _ = await setup_wallet(http, "10")
    record = await funding(client, root, org, receiver, "5")
    original = BillingRepository.audit

    async def failing(self, actor, action, *args, **kwargs):
        if action == "funding.posted":
            raise RuntimeError("test crash after wallet update")
        return await original(self, actor, action, *args, **kwargs)

    monkeypatch.setattr(BillingRepository, "audit", failing)
    path = f"/api/v1/organizations/{org['id']}/funding-records/{record['id']}/post"
    response = await client.post(path, headers=headers(root, "retry-after-crash", 1))
    assert response.status_code == 500
    assert (await balance(client, root, org))["balance"] == "10.000000000000"
    assert (
        admin.execute(
            "SELECT status FROM billing.funding_records WHERE id=%s", (record["id"],)
        ).fetchone()[0]
        == "draft"
    )
    monkeypatch.setattr(BillingRepository, "audit", original)
    await post_funding(client, root, org, record, "retry-after-crash")
    assert (await balance(client, root, org))["balance"] == "15.000000000000"


async def test_concurrent_reservations_cannot_spend_held_funds(http):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http, "25")
    _, deployment, _, _, admission = await setup_model(http, org)
    admissions = [admission.model_copy(update={"request_id": uuid4()}) for _ in range(16)]
    results = await asyncio.gather(
        *(app.state.reservations.reserve(item) for item in admissions), return_exceptions=True
    )
    accepted = [item for item in results if not isinstance(item, Exception)]
    rejected = [item for item in results if isinstance(item, ServiceError)]
    assert len(accepted) == 2 and len(rejected) == 14
    assert all(error.code == "insufficient_funds" for error in rejected)
    wallet = await balance(client, root, org)
    assert wallet["balance"] == "25.000000000000" and wallet["held_amount"] == "20.000000000000"
    winner = next(item for item in admissions if item.request_id == accepted[0].id)
    repeated = await asyncio.gather(*(app.state.reservations.reserve(winner) for _ in range(8)))
    assert all(item.id == winner.request_id for item in repeated)
    with pytest.raises(ServiceError, match="different authorization facts"):
        await app.state.reservations.reserve(winner.model_copy(update={"output_limit": 5}))
    result, facts, _ = await settle_success(app, winner, deployment)
    assert result.charged_amount == 5
    repeated = await asyncio.gather(
        *(
            app.state.reservations.settle(winner.organization_id, winner.request_id, facts)
            for _ in range(8)
        )
    )
    assert all(item.charged_amount == 5 for item in repeated)
    wallet = await balance(client, root, org)
    assert wallet["balance"] == "20.000000000000" and wallet["held_amount"] == "10.000000000000"
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/account/reconcile", headers=auth(root)
    )
    assert response.json()["consistent"] is True, response.text


async def test_locked_prices_discounts_and_per_attempt_costs(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    model, deployment, old_sale, old_cost, admission = await setup_model(http, org)
    first = await app.state.reservations.reserve(admission)
    assert str(first.discount_version_id) == "00000000-0000-0000-0000-000000000001"
    attempt1 = uuid4()
    await app.state.reservations.start_attempt(
        admission.organization_id, admission.request_id, attempt1, UUID(deployment["id"])
    )
    await publish_price(client, root, "sale", model, "2")
    await publish_price(client, root, "cost", deployment, "0.7")
    discount = await publish_discount(client, root, org, "0.5")
    attempt2 = uuid4()
    second = await app.state.reservations.start_attempt(
        admission.organization_id, admission.request_id, attempt2, UUID(deployment["id"])
    )
    assert str(second["cost_price_version_id"]) != old_cost["id"]
    usage = {"input_uncached_tokens": 2, "output_tokens": 3}
    c1 = await app.state.reservations.record_cost(
        admission.organization_id, admission.request_id, attempt1, usage, "attempt-1"
    )
    c2 = await app.state.reservations.record_cost(
        admission.organization_id, admission.request_id, attempt2, usage, "attempt-2"
    )
    assert Decimal(c1["cost_amount"]) == 2 and Decimal(c2["cost_amount"]) == Decimal("3.5")
    facts = Settlement(
        outcome="success",
        delivered_attempt_id=attempt2,
        quantities=usage,
        evidence_reference="second-delivered",
        normalization_version="test.v1",
    )
    settled = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, facts
    )
    assert settled.charged_amount == 5 and str(settled.sale_price_version_id) == old_sale["id"]
    next_admission = admission.model_copy(update={"request_id": uuid4()})
    next_hold = await app.state.reservations.reserve(next_admission)
    assert str(next_hold.discount_version_id) == discount["id"] and next_hold.reserved_amount == 10
    assert next_hold.sale_price_version_id != first.sale_price_version_id
    quote = await client.get(
        f"/api/v1/organizations/{org['id']}/pricing",
        headers=auth(root),
        params={"model_id": model["id"]},
    )
    assert quote.status_code == 200 and quote.json()["discount_factor"] == "0.500000"
    assert "cost" not in quote.text
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE billing.price_versions SET rates='{}'::jsonb WHERE id=%s", (old_sale["id"],)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE billing.discount_versions SET factor=.1 WHERE id=%s", (discount["id"],)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE billing.request_runs SET reserved_amount=0 WHERE id=%s", (admission.request_id,)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE billing.ledger_entries SET signed_amount=99")


async def test_price_version_lists_filter_by_kind_and_scope(http):
    client, root, _, _ = http
    org, _, _ = await setup_wallet(http)
    model, deployment, sale, cost, _ = await setup_model(http, org)
    other = await make_model(client, root, "other-model", ["openai_compatible"])
    other_sale = await publish_price(client, root, "sale", other)
    for kind, scope, own, foreign in [
        ("sale", model, sale, other_sale),
        ("cost", deployment, cost, None),
    ]:
        path = f"/api/v1/{kind}-price-versions"
        listed = await client.get(path, headers=auth(root))
        assert listed.status_code == 200, listed.text
        rows = listed.json()["items"]
        assert {row["kind"] for row in rows} == {kind}
        assert own["id"] in {row["id"] for row in rows}
        if foreign:
            assert foreign["id"] in {row["id"] for row in rows}
        scoped = await client.get(path, headers=auth(root), params={"scope_id": scope["id"]})
        assert scoped.status_code == 200, scoped.text
        assert [row["id"] for row in scoped.json()["items"]] == [own["id"]]
        page = await client.get(path, headers=auth(root), params={"limit": 1})
        assert page.status_code == 200 and len(page.json()["items"]) == 1, page.text
    # A model ID is not a cost scope: each list keeps its own kind.
    response = await client.get(
        "/api/v1/cost-price-versions", headers=auth(root), params={"scope_id": model["id"]}
    )
    assert response.status_code == 200 and response.json()["items"] == []


async def test_price_publication_order_idempotency_and_unsupported_values(http):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    model, _, _, _, _ = await setup_model(http, org)
    body = {
        "model_id": model["id"],
        "currency": "USD",
        "rates": {
            "input_uncached_tokens": {"unit_price": "1", "unit_quantity": 1000},
            "output_tokens": {"unit_price": "2", "unit_quantity": 1000},
        },
    }
    path = "/api/v1/sale-price-versions"
    response = await client.post(path, headers=headers(root, "draft-one"), json=body)
    row = response.json()
    assert response.status_code == 201, response.text
    for when in [datetime.now(UTC) - timedelta(days=1)]:
        response = await client.post(
            path + "/" + row["id"] + "/publish",
            headers=headers(root, "invalid-time", 1),
            json={"effective_at": when.isoformat()},
        )
        assert response.status_code == 422
    future = datetime.now(UTC) + timedelta(days=1)
    responses = await asyncio.gather(
        *(
            client.post(
                path + "/" + row["id"] + "/publish",
                headers=headers(root, "future-publication", 1),
                json={"effective_at": future.isoformat()},
            )
            for _ in range(4)
        )
    )
    assert all(response.status_code == 200 for response in responses)
    body2 = {k: v for k, v in body.items() if k != "model_id"}
    response = await client.patch(path + "/" + row["id"], headers=auth(root, 2), json=body2)
    assert response.status_code == 409
    response = await client.post(path, headers=headers(root, "draft-two"), json=body)
    row2 = response.json()
    response = await client.post(
        path + "/" + row2["id"] + "/publish", headers=headers(root, "out-of-order", 1), json={}
    )
    assert response.status_code == 422
    for price in [1.5, "NaN", "Infinity", "-1", "0.0000000000001"]:
        invalid = {
            **body,
            "rates": {
                **body["rates"],
                "output_tokens": {"unit_price": price, "unit_quantity": 1000},
            },
        }
        response = await client.post(path, headers=headers(root, uuid4().hex), json=invalid)
        assert response.status_code == 422
    response = await client.post(
        path, headers=headers(root, uuid4().hex), json={**body, "currency": "CNY"}
    )
    assert response.status_code == 422


async def make_budget(client, root, org, amount, **values):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/budget-policies",
        headers=auth(root),
        json={"currency": "USD", "limit_amount": amount, "period": "month", **values},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_all_budget_layers_hold_money_and_midperiod_backfill(http):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, deployment, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    key_budget = await make_budget(
        client, root, org, "15", scope_type="key", scope_id=str(admission.api_key_id)
    )
    await make_budget(client, root, org, "100", layer="platform")
    await make_budget(
        client, root, org, "100", scope_type="project", scope_id=str(admission.project_id)
    )
    with pytest.raises(ServiceError) as error:
        await app.state.reservations.reserve(admission.model_copy(update={"request_id": uuid4()}))
    assert error.value.code == "budget_exceeded"
    await settle_success(app, admission, deployment)
    # Adding a budget after settlement must include already-spent money.
    tiny = await make_budget(client, root, org, "4", period="day")
    periods_path = f"/api/v1/organizations/{org['id']}/budget-policies/{tiny['id']}/periods"
    period = (await client.get(periods_path, headers=auth(root))).json()["items"][0]
    assert period["spent"] == "5.000000000000" and period["over_limit"] is True
    budget_path = f"/api/v1/organizations/{org['id']}/budget-policies/{tiny['id']}"
    result = await client.patch(budget_path, headers=auth(root, 1), json={"status": "disabled"})
    assert result.status_code == 200
    second = admission.model_copy(update={"request_id": uuid4()})
    await app.state.reservations.reserve(second)
    await settle_success(app, second, deployment)
    result = await client.patch(
        budget_path, headers=auth(root, 2), json={"status": "active", "limit_amount": "100"}
    )
    assert result.status_code == 200
    period = (await client.get(periods_path, headers=auth(root))).json()["items"][0]
    assert period["spent"] == "10.000000000000"
    result = await client.patch(
        f"/api/v1/organizations/{org['id']}/budget-policies/{key_budget['id']}",
        headers=auth(root, 1),
        json={"limit_amount": "1"},
    )
    assert result.status_code == 200
    with pytest.raises(ServiceError) as error:
        await app.state.reservations.reserve(admission.model_copy(update={"request_id": uuid4()}))
    assert error.value.code == "budget_exceeded"
    result = await client.post(
        f"/api/v1/organizations/{org['id']}/account/reconcile", headers=auth(root)
    )
    assert result.json()["consistent"] is True, result.text


async def adjustment(client, root, org, source_id, amount):
    result = await client.post(
        f"/api/v1/organizations/{org['id']}/adjustments",
        headers=headers(root, uuid4().hex),
        json={
            "source_entry_id": source_id,
            "amount": amount,
            "currency": "USD",
            "reason": "test correction",
            "evidence": "test financial evidence",
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


async def test_refunds_target_original_budget_period_and_cannot_exceed_charge(
    http, admin, monkeypatch
):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, deployment, _, _, admission = await setup_model(http, org)
    at = datetime(2029, 1, 31, 23, 59, 59, tzinfo=UTC)

    async def clock(self):
        return at

    monkeypatch.setattr(BillingRepository, "now", clock)
    budget = await make_budget(client, root, org, "20")
    await app.state.reservations.reserve(admission)
    at = datetime(2029, 2, 1, tzinfo=UTC)
    await settle_success(app, admission, deployment)
    next_admission = admission.model_copy(update={"request_id": uuid4()})
    await app.state.reservations.reserve(next_admission)
    source = admin.execute(
        "SELECT id FROM billing.ledger_entries WHERE source_type='request' AND source_id=%s",
        (admission.request_id,),
    ).fetchone()[0]
    one = await adjustment(client, root, org, str(source), "4")
    two = await adjustment(client, root, org, str(source), "4")
    results = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/organizations/{org['id']}/adjustments/{row['id']}/post",
                headers=headers(root, uuid4().hex, 1),
            )
            for row in [one, two]
        )
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    rows = admin.execute(
        "SELECT period_start,spent,reserved FROM billing.budget_periods "
        "WHERE policy_id=%s ORDER BY period_start",
        (budget["id"],),
    ).fetchall()
    assert rows[0][1:] == (Decimal(1), Decimal(0))
    assert rows[1][1:] == (Decimal(0), Decimal(10))
    assert (await balance(client, root, org))["balance"] == "99.000000000000"
    result = await client.post(
        f"/api/v1/organizations/{org['id']}/account/reconcile", headers=auth(root)
    )
    assert result.json()["consistent"] is True, result.text


async def test_funding_reversal_never_uses_reserved_money(http, admin):
    client, root, app, _ = http
    org, _, funding_record = await setup_wallet(http, "10")
    _, _, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    source = admin.execute(
        "SELECT id FROM billing.ledger_entries WHERE source_type='funding' AND source_id=%s",
        (funding_record["id"],),
    ).fetchone()[0]
    reverse = await adjustment(client, root, org, str(source), "1")
    path = f"/api/v1/organizations/{org['id']}/adjustments/{reverse['id']}/post"
    result = await client.post(path, headers=headers(root, "reversal", 1))
    assert result.status_code == 409 and result.json()["error"]["code"] == "insufficient_funds"
    facts = Settlement(
        outcome="not_started",
        evidence_reference="never-dispatched",
        normalization_version="test.v1",
    )
    await app.state.reservations.settle(admission.organization_id, admission.request_id, facts)
    result = await client.post(path, headers=headers(root, "reversal", 1))
    assert result.status_code == 200, result.text
    assert (await balance(client, root, org))["balance"] == "9.000000000000"


async def test_timeout_and_unknown_results_keep_holds_and_late_usage_settles(
    http, admin, monkeypatch
):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, deployment, _, _, admission = await setup_model(http, org)
    admission = admission.model_copy(update={"ttl_seconds": 1})
    await app.state.reservations.reserve(admission)
    attempt = uuid4()
    await app.state.reservations.start_attempt(
        admission.organization_id, admission.request_id, attempt, UUID(deployment["id"])
    )
    not_started = Settlement(
        outcome="not_started",
        evidence_reference="unconfirmed-no-response",
        normalization_version="test.v1",
    )
    result = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, not_started
    )
    assert result.state == "review" and result.reservation_state == "held"
    assert (await balance(client, root, org))["held_amount"] == "10.000000000000"
    late = Settlement(
        outcome="customer_cancelled",
        delivered_attempt_id=attempt,
        quantities={"input_uncached_tokens": 1, "output_tokens": 2},
        evidence_reference="late-final",
        normalization_version="test.v1",
    )
    result = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, late
    )
    assert result.charged_amount == 3 and result.reservation_state == "captured"
    # A separate timed-out reservation is marked for review, never released by time alone.
    stale = admission.model_copy(update={"request_id": uuid4()})
    await app.state.reservations.reserve(stale)
    await asyncio.sleep(1.05)
    assert await app.state.reservations.mark_expired() == 1
    row = admin.execute(
        "SELECT reservation_state,state,review_reason FROM billing.request_runs WHERE id=%s",
        (stale.request_id,),
    ).fetchone()
    assert row == ("held", "review", "reservation_expired")
    assert (await balance(client, root, org))["held_amount"] == "10.000000000000"


async def test_usage_overrun_freezes_account_without_clipping_or_overdraft(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http, "10")
    _, deployment, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    result, _, _ = await settle_success(
        app, admission, deployment, {"input_uncached_tokens": 4, "output_tokens": 7}
    )
    assert result.state == "review" and result.charged_amount is None
    wallet = await balance(client, root, org)
    assert wallet["balance"] == "10.000000000000" and wallet["held_amount"] == "10.000000000000"
    assert wallet["status"] == "frozen"
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(ServiceError) as error:
        await app.state.reservations.reserve(admission.model_copy(update={"request_id": uuid4()}))
    assert error.value.code == "account_frozen"


async def test_reconciliation_reports_drift_without_rewriting_facts(http, admin):
    client, root, _, _ = http
    org, _, _ = await setup_wallet(http, "10")
    admin.execute("UPDATE billing.accounts SET balance=11 WHERE organization_id=%s", (org["id"],))
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/account/reconcile", headers=auth(root)
    )
    assert response.status_code == 200
    assert response.json()["consistent"] is False
    assert response.json()["issues"][0]["kind"] == "balance"
    assert (await balance(client, root, org))["balance"] == "11.000000000000"


async def test_finance_tenant_isolation_and_budget_ownership(http):
    client, root, app, _ = http
    org, _, record = await setup_wallet(http)
    first = await create_user(client, root, "tenant-finance")
    second = await create_user(client, root, "other-owner")
    org2 = await create_org(client, root, "Other business", second["user"]["id"])
    await app.state.process_billing()
    base = f"/api/v1/organizations/{org['id']}"
    response = await client.post(
        base + "/members",
        headers=auth(root),
        json={"user_id": first["user"]["id"], "role": "org_finance"},
    )
    assert response.status_code == 201
    finance = await login(client, "tenant-finance")
    outsider = await login(client, "other-owner")
    assert (await client.get(base + "/account", headers=auth(finance))).status_code == 200
    assert (await client.get(base + "/ledger", headers=auth(finance))).status_code == 200
    assert (await client.get(base + "/account", headers=auth(outsider))).status_code == 404
    assert (
        await client.get(
            f"/api/v1/organizations/{org2['id']}/funding-records/{record['id']}",
            headers=auth(outsider),
        )
    ).status_code == 404
    assert (
        await client.post(
            base + f"/funding-records/{record['id']}/post", headers=headers(finance, "no-credit", 2)
        )
    ).status_code == 403
    body = {"currency": "USD", "limit_amount": "10", "period": "day"}
    assert (
        await client.post(base + "/budget-policies", headers=auth(finance), json=body)
    ).status_code == 403
    # An owner may add its own cap, but cannot edit the platform cap.
    platform = await make_budget(client, root, org2, "10", layer="platform")
    own_base = f"/api/v1/organizations/{org2['id']}/budget-policies"
    assert (await client.post(own_base, headers=auth(outsider), json=body)).status_code == 201
    assert (
        await client.patch(
            own_base + "/" + platform["id"], headers=auth(outsider, 1), json={"limit_amount": "20"}
        )
    ).status_code == 403


async def test_zero_charge_and_failed_attempt_cost_do_not_debit_customer(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, deployment, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    attempt = uuid4()
    await app.state.reservations.start_attempt(
        admission.organization_id, admission.request_id, attempt, UUID(deployment["id"])
    )
    await app.state.reservations.record_cost(
        admission.organization_id,
        admission.request_id,
        attempt,
        {"input_uncached_tokens": 2, "output_tokens": 1},
        "failed-attempt-evidence",
    )
    failed = Settlement(
        outcome="upstream_failed",
        evidence_reference="terminal-upstream-failure",
        normalization_version="test.v1",
    )
    result = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, failed
    )
    assert result.charged_amount == 0 and result.reservation_state == "released"
    assert (await balance(client, root, org))["balance"] == "100.000000000000"
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone()[0]
        == 0
    )
    assert admin.execute(
        "SELECT cost_amount FROM billing.request_attempts WHERE id=%s", (attempt,)
    ).fetchone()[0] == Decimal("1.2")
    with pytest.raises(ServiceError) as error:
        await app.state.reservations.settle(
            admission.organization_id,
            admission.request_id,
            failed.model_copy(update={"evidence_reference": "different-fact"}),
        )
    assert error.value.code == "settlement_conflict"


async def test_unknown_account_event_versions_are_not_consumed(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Versioned event business")
    admin.execute("UPDATE identity.outbox SET schema_version=2")
    with pytest.raises(ServiceError) as rejected:
        await app.state.process_billing()
    assert rejected.value.code == "unsupported_event_version"
    assert admin.execute("SELECT count(*) FROM billing.accounts").fetchone()[0] == 0
    admin.execute("UPDATE identity.outbox SET schema_version=1")
    events = await app.state.identity_service.pending_organizations()
    event = next(event for event in events if str(event.organization_id) == org["id"])
    await app.state.billing_service.provision_account(event)
    admin.execute("UPDATE billing.outbox SET schema_version=2")
    with pytest.raises(ServiceError) as rejected:
        await app.state.process_billing()
    assert rejected.value.code == "unsupported_event_version"
    assert (
        admin.execute(
            "SELECT status FROM identity.organizations WHERE id=%s", (org["id"],)
        ).fetchone()[0]
        == "provisioning"
    )
    assert admin.execute("SELECT count(*) FROM identity.inbox").fetchone()[0] == 0
    assert (
        admin.execute(
            "SELECT delivered_at FROM billing.outbox WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        is None
    )
    admin.execute("UPDATE billing.outbox SET schema_version=1")
    await app.state.process_billing()
    assert (
        admin.execute(
            "SELECT status FROM identity.organizations WHERE id=%s", (org["id"],)
        ).fetchone()[0]
        == "active"
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.accounts WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.inbox WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM identity.inbox WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]
        == 1
    )


async def test_fractional_reservation_matches_the_m3_acceptance_case(http, admin):
    client, root, app, _ = http
    org, receiver, _ = await setup_wallet(http, "1")
    model, _, _, _, admission = await setup_model(http, org)
    await publish_price(client, root, "sale", model, "0.08")
    await make_budget(client, root, org, "1")
    candidates = [admission, admission.model_copy(update={"request_id": uuid4()})]
    results = await asyncio.gather(
        *(app.state.reservations.reserve(item) for item in candidates), return_exceptions=True
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1 and successes[0].reserved_amount == Decimal("0.8")
    failures = [result for result in results if isinstance(result, ServiceError)]
    assert len(failures) == 1 and failures[0].code == "insufficient_funds"
    wallet = await balance(client, root, org)
    assert (wallet["balance"], wallet["held_amount"], wallet["available_balance"]) == (
        "1.000000000000",
        "0.800000000000",
        "0.200000000000",
    )
    assert admin.execute("SELECT reserved,spent FROM billing.budget_periods").fetchone() == (
        Decimal("0.8"),
        Decimal(0),
    )
    # More deposited money cannot replenish an independent monetary budget.
    record = await funding(client, root, org, receiver, "1")
    await post_funding(client, root, org, record)
    with pytest.raises(ServiceError) as blocked:
        await app.state.reservations.reserve(admission.model_copy(update={"request_id": uuid4()}))
    assert blocked.value.code == "budget_exceeded"


async def test_unavailable_prices_never_default_to_zero(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    model, deployment, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    for kind, target in [("cost", deployment), ("sale", model)]:
        path = "/api/v1/" + kind + "-price-versions"
        body = {
            "currency": "USD",
            "enabled": False,
            "rates": {
                dimension: {"unit_price": "1", "unit_quantity": 1}
                for dimension in ("input_uncached_tokens", "output_tokens")
            },
        }
        body["model_id" if kind == "sale" else "deployment_id"] = target["id"]
        response = await client.post(path, headers=headers(root, uuid4().hex), json=body)
        assert response.status_code == 201, response.text
        row = response.json()
        response = await client.post(
            path + "/" + row["id"] + "/publish", headers=headers(root, uuid4().hex, 1), json={}
        )
        assert response.status_code == 200, response.text
    with pytest.raises(ServiceError) as blocked:
        await app.state.reservations.start_attempt(
            admission.organization_id, admission.request_id, uuid4(), UUID(deployment["id"])
        )
    assert blocked.value.code == "cost_unavailable"
    with pytest.raises(ServiceError) as blocked:
        await app.state.reservations.reserve(admission.model_copy(update={"request_id": uuid4()}))
    assert blocked.value.code == "price_unavailable"
    assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone()[0] == 0
    assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone()[0] == 1
    assert (await balance(client, root, org))["held_amount"] == "10.000000000000"


async def test_unknown_usage_dimensions_keep_the_original_reservation(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, deployment, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    result, facts, _ = await settle_success(
        app,
        admission,
        deployment,
        {
            "input_uncached_tokens": 2,
            "output_tokens": 3,
            "unknown_cache_tokens": 0,
        },
    )
    assert result.state == "review" and result.review_reason == "invalid_final_usage"
    assert result.charged_amount is None and result.reservation_state == "held"
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone()[0]
        == 0
    )
    assert (await balance(client, root, org))["held_amount"] == "10.000000000000"
    corrected = facts.model_copy(
        update={
            "quantities": {"input_uncached_tokens": 2, "output_tokens": 3},
            "evidence_reference": "normalized-late-evidence",
        }
    )
    result = await app.state.reservations.settle(
        admission.organization_id, admission.request_id, corrected
    )
    assert result.charged_amount == 5


async def test_billing_rejects_cross_tenant_financial_references(http, admin):
    client, root, app, _ = http
    org, receiver, _ = await setup_wallet(http)
    _, _, _, _, admission = await setup_model(http, org)
    other = await create_org(client, root, "Other funded organization")
    await app.state.process_billing()
    receipt = await funding(client, root, other, receiver, "100")
    await post_funding(client, root, other, receipt)
    bad = admission.model_copy(update={"request_id": uuid4(), "organization_id": UUID(other["id"])})
    with pytest.raises(ServiceError) as rejected:
        await app.state.reservations.reserve(bad)
    assert rejected.value.code == "billing_conflict"
    assert (await balance(client, root, other))["held_amount"] == "0.000000000000"
    await make_budget(client, root, org, "100")
    other_budget = await make_budget(client, root, other, "100")
    await app.state.reservations.reserve(admission)
    period = admin.execute(
        "SELECT id FROM billing.budget_periods WHERE policy_id=%s", (other_budget["id"],)
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO billing.budget_holds "
            "(organization_id,request_id,period_id,reserved,spent) VALUES (%s,%s,%s,1,0)",
            (org["id"], admission.request_id, period),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "UPDATE billing.budget_periods SET organization_id=%s WHERE id=%s", (org["id"], period)
        )
    for company in (org, other):
        response = await client.post(
            f"/api/v1/organizations/{company['id']}/account/reconcile", headers=auth(root)
        )
        assert response.json()["consistent"] is True, response.text


async def test_reconcile_finds_hold_mismatch_even_when_period_total_matches(http, admin):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, _, _, _, admission = await setup_model(http, org)
    await make_budget(client, root, org, "50")
    await app.state.reservations.reserve(admission)
    # Preserve the aggregate but break the association with the authoritative request.
    admin.execute(
        "UPDATE billing.budget_holds SET reserved=reserved-1 WHERE request_id=%s",
        (admission.request_id,),
    )
    admin.execute(
        "UPDATE billing.budget_periods SET reserved=reserved-1 WHERE organization_id=%s",
        (org["id"],),
    )
    path = f"/api/v1/organizations/{org['id']}/account/reconcile"
    report = await client.post(path, headers=auth(root))
    assert report.status_code == 200, report.text
    assert [(i["kind"], i["request_id"]) for i in report.json()["issues"]] == [
        ("request_budget", str(admission.request_id))
    ]
