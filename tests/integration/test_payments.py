import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_billing import account_amounts, balance, finance_role, headers
from test_identity_api import PASSWORD, auth, create_org, login

from nexus.api import create_app
from nexus.shared.storage import SCHEMA_REVISION
from scripts.mock_payment_provider import MockPaymentProvider

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY = "mock-api-key-for-local-tests-0000000001"
SIGNING_KEY = "mock-signing-key-for-local-tests-000001"
NEW_API_KEY = "mock-api-key-for-local-tests-0000000002"
NEW_SIGNING_KEY = "mock-signing-key-for-local-tests-000002"


@pytest.fixture
async def payments(runtime_settings):
    provider = await MockPaymentProvider(
        {API_KEY: ("merchant-a", "test"), NEW_API_KEY: ("merchant-a", "test")}
    ).start()
    settings = runtime_settings.model_copy(
        update={
            "mock_payment_url": provider.url,
            "payment_public_base_url": "https://control.nexus.test",
            "payment_return_url": "https://billing.nexus.test/payments",
            "redis_namespace": "nexus:payments-test:" + uuid4().hex,
        }
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            await app.state.identity_service.bootstrap_admin("root-admin", PASSWORD)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                root = await login(client)
                yield SimpleNamespace(
                    client=client,
                    root=root,
                    app=app,
                    settings=settings,
                    provider=provider,
                    http=(client, root, app, settings),
                )
    finally:
        await provider.close()


async def channel(p, **values):
    body = {
        "name": "Mock USD",
        "adapter_type": "mock",
        "stable_merchant_id": "merchant-a",
        "environment": "test",
        "currency": "USD",
        "min_amount": "1",
        "max_amount": "1000",
        "configuration": {"api_key": API_KEY, "webhook_secret": SIGNING_KEY},
        **values,
    }
    response = await p.client.post(
        "/api/v1/payment-channels", headers=headers(p.root, uuid4().hex), json=body
    )
    assert response.status_code == 201, response.text
    assert API_KEY not in response.text and SIGNING_KEY not in response.text
    return response.json()


async def enterprise(p):
    org = await create_org(p.client, p.root, "Payment business " + uuid4().hex[:6])
    await p.app.state.process_billing()
    return org


async def order(p, org, chan, amount="10", key=None, token=None):
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders",
        headers=headers(token or p.root, key or uuid4().hex),
        json={"channel_id": chan["id"], "amount": amount, "currency": "USD"},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def read_order(p, org, item, token=None):
    response = await p.client.get(
        f"/api/v1/organizations/{org['id']}/payment-orders/{item['id']}",
        headers=auth(token or p.root),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def notify(p, chan, values, secret=SIGNING_KEY, mode="raw_json"):
    raw, signed = p.provider.signed_notice(values, secret, mode=mode)
    response = await p.client.post("/webhooks/payments/" + chan["id"], headers=signed, content=raw)
    assert response.headers["cache-control"] == "no-store"
    return response


async def create_remote(p, org, item):
    assert await p.app.state.payment_processor.process(1) == 1
    result = await read_order(p, org, item)
    assert result["checkout_state"] == "ready", result
    assert result["checkout"]["destination"].startswith("https://checkout.mock.nexus.test/")
    return result


async def test_payment_end_to_end_uses_gross_amount_and_one_funding_record(payments, admin):
    p = payments
    org = await enterprise(p)
    from test_billing import publish_discount

    await publish_discount(p.client, p.root, org, "0.5")
    chan = await channel(p)
    item = await order(p, org, chan, key="new-payment")
    assert item["credit_state"] == "uncredited" and item["checkout"] is None
    repeated = await order(p, org, chan, key="new-payment")
    assert repeated["id"] == item["id"]
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    p.provider.fact(item["merchant_order_ref"]).update(fee_minor=100, net_minor=900)
    notice = p.provider.notice(item["merchant_order_ref"], event_id="payment-success")
    response = await notify(p, chan, notice)
    assert response.status_code == 200, response.text
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["credit_state"] == "credited", result
    assert result["checkout"] is None
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    for current in [notice, {**notice, "event_id": "different-event-same-payment"}]:
        assert (await notify(p, chan, current)).status_code == 200
        await p.app.state.payment_processor.process()
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.funding_records WHERE source_type='gateway'"
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='funding_credit'"
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.outbox WHERE topic='billing.funding_posted'"
        ).fetchone()[0]
        == 1
    )
    assert admin.execute(
        "SELECT created_by,posted_by,system_actor FROM billing.funding_records"
    ).fetchone() == (None, None, "payment-worker")
    await finance_role(p.client, p.root)
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/account/reconcile", headers=auth(p.root)
    )
    assert response.json()["consistent"] is True, response.text


async def test_invalid_signature_and_amount_precision_never_enqueue_credit(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    for value in ["1.001", 1, "NaN", "0", "1001"]:
        response = await p.client.post(
            f"/api/v1/organizations/{org['id']}/payment-orders",
            headers=headers(p.root, uuid4().hex),
            json={"channel_id": chan["id"], "amount": value, "currency": "USD"},
        )
        assert response.status_code == 422, response.text
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    response = await notify(
        p,
        chan,
        p.provider.notice(item["merchant_order_ref"]),
        secret="incorrect-signing-key-for-fixture",
    )
    assert response.status_code == 400
    assert admin.execute("SELECT count(*) FROM billing.payment_events").fetchone()[0] == 0
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"


async def test_query_binding_mismatch_prevents_automatic_credit(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    p.provider.query_override = lambda values: {**values, "amount_minor": 999}
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["review_required"] and result["credit_state"] == "uncredited"
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"
    assert admin.execute("SELECT count(*) FROM billing.payment_observations").fetchone()[0] >= 1
    assert admin.execute("SELECT count(*) FROM billing.payment_cases").fetchone()[0] >= 1


async def test_create_response_loss_is_recovered_by_query_without_new_remote_payment(payments):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    p.provider.drop_create_responses = 1
    await p.app.state.payment_processor.process(1)
    assert p.provider.create_calls == 1
    p.provider.paid(item["merchant_order_ref"])
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["credit_state"] == "credited", result
    assert p.provider.create_calls == 1
    assert len(p.provider.objects) == 1
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"


async def settle_paid(p, org, item, chan):
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"


async def test_configuration_is_encrypted_and_rotation_keeps_receipt_identity(
    payments, admin, caplog
):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    old_config = chan["active_config_id"]
    response = await p.client.patch(
        "/api/v1/payment-channels/" + chan["id"],
        headers=headers(p.root, "rotate", 1),
        json={
            "configuration": {"api_key": NEW_API_KEY, "webhook_secret": NEW_SIGNING_KEY},
            "verification_grace_seconds": 3600,
        },
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["active_config_id"] != old_config
    p.provider.keys.pop(API_KEY)
    p.provider.paid(item["merchant_order_ref"])
    # Historical orders are queried using the new valid credential, and accept its signature.
    assert (
        await notify(p, chan, p.provider.notice(item["merchant_order_ref"]), secret=NEW_SIGNING_KEY)
    ).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert p.provider.used_keys[-1][1] == NEW_API_KEY
    metadata = await p.client.get(
        "/api/v1/payment-channels/" + chan["id"] + "/versions", headers=auth(p.root)
    )
    assert metadata.status_code == 200
    assert all(
        secret not in metadata.text + caplog.text
        for secret in (API_KEY, SIGNING_KEY, NEW_API_KEY, NEW_SIGNING_KEY)
    )
    values = admin.execute(
        "SELECT ciphertext,public_configuration FROM billing.payment_channel_versions"
    ).fetchall()
    assert all(
        API_KEY.encode() not in bytes(blob) and NEW_API_KEY.encode() not in bytes(blob)
        for blob, _ in values
    )
    assert all("api_key" not in config and "webhook_secret" not in config for _, config in values)
    # The old verification key works only until explicit revocation/its grace window ends.
    old_notice = p.provider.notice(item["merchant_order_ref"])
    assert (await notify(p, chan, old_notice)).status_code == 200
    response = await p.client.post(
        f"/api/v1/payment-channels/{chan['id']}/versions/{old_config}/revoke",
        headers=auth(p.root, changed["version"]),
    )
    assert response.status_code == 200, response.text
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 400
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    assert (
        admin.execute(
            "SELECT count(DISTINCT receipt_namespace) FROM billing.payment_receipts"
        ).fetchone()[0]
        == 1
    )


async def test_callback_before_create_response_does_not_regress_credit(payments):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    p.provider.create_barrier = asyncio.Event()
    task = asyncio.create_task(p.app.state.payment_processor.process(1))
    await asyncio.wait_for(p.provider.create_started.wait(), 2)
    p.provider.paid(item["merchant_order_ref"])
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    p.provider.create_barrier.set()
    await task
    result = await read_order(p, org, item)
    assert result["credit_state"] == "credited" and result["checkout_state"] == "closed"
    assert result["checkout"] is None
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"


async def test_expired_worker_lease_queries_before_recovering_create(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    p.provider.create_barrier = asyncio.Event()
    first = asyncio.create_task(p.app.state.payment_processor.process(1))
    await asyncio.wait_for(p.provider.create_started.wait(), 2)
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE kind='create' AND order_id=%s",
        (item["id"],),
    )
    await p.app.state.payment_processor.process(1)
    assert p.provider.query_calls >= 1 and p.provider.create_calls == 1
    p.provider.create_barrier.set()
    await first
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["checkout_state"] == "ready"
    assert len(p.provider.objects) == 1


async def test_stale_not_found_query_cannot_override_new_creation(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET next_attempt_at=clock_timestamp()-interval '5 seconds' "
        "WHERE kind='query' AND order_id=%s",
        (item["id"],),
    )
    p.provider.query_barrier = asyncio.Event()
    old_query = asyncio.create_task(p.app.state.payment_processor.process(1))
    await asyncio.wait_for(p.provider.query_started.wait(), 2)
    await p.app.state.payment_processor.process(1)
    assert (await read_order(p, org, item))["checkout_state"] == "ready"
    p.provider.query_barrier.set()
    await old_query
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["checkout_state"] == "ready" and result["credit_state"] == "uncredited"
    assert p.provider.create_calls == 1
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_observations WHERE disposition='stale'"
        ).fetchone()[0]
        >= 1
    )


async def test_two_channels_cannot_credit_one_actual_transaction_twice(payments, admin):
    p = payments
    first_org = await enterprise(p)
    second_org = await enterprise(p)
    first_channel = await channel(p)
    second_channel = await channel(p, name="Second channel same merchant")
    first = await order(p, first_org, first_channel)
    second = await order(p, second_org, second_channel)
    await p.app.state.payment_processor.process()
    p.provider.paid(first["merchant_order_ref"], transaction_id="shared-actual-transaction")
    p.provider.paid(second["merchant_order_ref"], transaction_id="shared-actual-transaction")
    assert (
        await notify(p, first_channel, p.provider.notice(first["merchant_order_ref"]))
    ).status_code == 200
    assert (
        await notify(p, second_channel, p.provider.notice(second["merchant_order_ref"]))
    ).status_code == 200
    await asyncio.gather(*(p.app.state.payment_processor.process(1) for _ in range(8)))
    balances = [
        Decimal((await balance(p.client, p.root, org))["balance"])
        for org in (first_org, second_org)
    ]
    assert sorted(balances) == [Decimal(0), Decimal(10)]
    assert admin.execute("SELECT count(*) FROM billing.payment_receipts").fetchone()[0] == 1
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='funding_credit'"
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_cases WHERE reason='receipt_conflict'"
        ).fetchone()[0]
        == 1
    )


async def test_additional_capture_and_its_refund_do_not_change_original_credit(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    extra = p.provider.extra_payment(item["merchant_order_ref"])
    assert (
        await notify(p, chan, p.provider.notice(extra["provider_payment_ref"]))
    ).status_code == 200
    await p.app.state.payment_processor.process()
    assert admin.execute("SELECT count(*) FROM billing.payment_receipts").fetchone()[0] == 2
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    extra.update(
        state="reversed",
        reversed_minor=1000,
        reversal_state="succeeded",
        reversal_reference="extra-reversal",
    )
    assert (
        await notify(p, chan, p.provider.notice(extra["provider_payment_ref"]))
    ).status_code == 200
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["credit_state"] == "credited" and result["payment_state"] == "succeeded"
    assert result["review_required"] is True
    assert (
        admin.execute(
            "SELECT reversed_amount FROM billing.payment_orders WHERE id=%s", (item["id"],)
        ).fetchone()[0]
        == 0
    )
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='funding_credit'"
        ).fetchone()[0]
        == 1
    )


async def test_external_partial_reversal_and_dispute_preserve_ledger_and_block_spending(
    payments, admin
):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    obj = p.provider.fact(item["merchant_order_ref"])
    obj.update(
        state="partially_reversed",
        reversed_minor=300,
        reversal_state="succeeded",
        reversal_reference="partial-refund-1",
    )
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    assert (await balance(p.client, p.root, org))["status"] == "frozen"
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["payment_state"] == "partially_reversed" and result["credit_state"] == "credited"
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    obj.update(disputed_minor=200, dispute_state="open", dispute_reference="dispute-1")
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT reversed_amount,disputed_amount FROM billing.payment_orders WHERE id=%s",
        (item["id"],),
    ).fetchone() == (Decimal(3), Decimal(2))
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_cases "
            "WHERE reason IN ('payment_reversed','payment_disputed')"
        ).fetchone()[0]
        == 2
    )
    assert admin.execute("SELECT count(*) FROM billing.ledger_entries").fetchone()[0] == 1


async def test_expired_order_and_disabled_channel_still_accept_a_late_verified_payment(
    payments, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from nexus.modules.billing.persistence.payments import PaymentRepository

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    response = await p.client.patch(
        "/api/v1/payment-channels/" + chan["id"],
        headers=headers(p.root, "disable", 1),
        json={"status": "disabled"},
    )
    assert response.status_code == 200
    future = datetime.now(UTC) + timedelta(hours=1)

    async def future_now(self):
        return future

    monkeypatch.setattr(PaymentRepository, "now", future_now)
    assert (await read_order(p, org, item))["checkout"] is None
    p.provider.paid(item["merchant_order_ref"])
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders",
        headers=headers(p.root, uuid4().hex),
        json={"channel_id": chan["id"], "amount": "10", "currency": "USD"},
    )
    assert response.status_code == 409


async def test_form_notifications_preserve_duplicates_and_timestamp_authentication(payments, admin):
    import hashlib
    import hmac
    from datetime import UTC, datetime

    p = payments
    org = await enterprise(p)
    chan = await channel(
        p,
        configuration={
            "api_key": API_KEY,
            "webhook_secret": SIGNING_KEY,
            "notification_mode": "form",
        },
    )
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    values = p.provider.notice(item["merchant_order_ref"])
    raw, signed = p.provider.signed_notice(values, SIGNING_KEY, mode="form")
    duplicate = raw + b"&amount_minor=999"
    timestamp = int(datetime.now(UTC).timestamp())
    signature = hmac.new(
        SIGNING_KEY.encode(), str(timestamp).encode() + b"." + duplicate, hashlib.sha256
    ).hexdigest()
    invalid = await p.client.post(
        "/webhooks/payments/" + chan["id"],
        headers={**signed, "X-Mock-Signature": f"t={timestamp},v1={signature}"},
        content=duplicate,
    )
    assert invalid.status_code == 400
    stale, stale_headers = p.provider.signed_notice(
        values, SIGNING_KEY, mode="form", timestamp=timestamp - 1000
    )
    assert (
        await p.client.post(
            "/webhooks/payments/" + chan["id"], headers=stale_headers, content=stale
        )
    ).status_code == 400
    assert admin.execute("SELECT count(*) FROM billing.payment_events").fetchone()[0] == 0
    response = await p.client.get(
        "/webhooks/payments/" + chan["id"] + "?" + raw.decode(), headers=signed
    )
    assert response.status_code == 200 and response.text == "success"
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert (
        admin.execute("SELECT processing_state FROM billing.payment_events").fetchone()[0]
        == "processed"
    )


async def test_funding_failure_and_post_commit_cancellation_are_replay_safe(
    payments, admin, monkeypatch
):
    from contextlib import asynccontextmanager

    from nexus.modules.billing.persistence.payments import PaymentRepository

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    original_audit = PaymentRepository.audit

    async def crash_before_commit(self, actor, action, *args, **kwargs):
        if action == "payment_order.credited":
            raise RuntimeError("test crash before financial commit")
        await original_audit(self, actor, action, *args, **kwargs)

    monkeypatch.setattr(PaymentRepository, "audit", crash_before_commit)
    with pytest.raises(RuntimeError):
        await p.app.state.payment_processor.process(1)
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"
    assert (await read_order(p, org, item))["credit_state"] == "uncredited"
    assert admin.execute("SELECT count(*) FROM billing.funding_records").fetchone()[0] == 0
    assert admin.execute("SELECT count(*) FROM billing.payment_receipts").fetchone()[0] == 0
    monkeypatch.setattr(PaymentRepository, "audit", original_audit)
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE state='running'"
    )

    # Lose the response only after the actual PostgreSQL commit succeeds.
    async def flag_commit(self, actor, action, *args, **kwargs):
        await original_audit(self, actor, action, *args, **kwargs)
        if action == "payment_order.credited":
            self.committed_credit = True

    monkeypatch.setattr(PaymentRepository, "audit", flag_commit)
    processor = p.app.state.payment_processor
    original_transactions = processor._transactions

    @asynccontextmanager
    async def lose_commit_response():
        async with original_transactions() as repo:
            yield repo
        if getattr(repo, "committed_credit", False):
            raise asyncio.CancelledError()

    monkeypatch.setattr(processor, "_transactions", lose_commit_response)
    with pytest.raises(asyncio.CancelledError):
        await processor.process(1)
    monkeypatch.setattr(processor, "_transactions", original_transactions)
    monkeypatch.setattr(PaymentRepository, "audit", original_audit)
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await processor.process()
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    assert admin.execute("SELECT count(*) FROM billing.funding_records").fetchone()[0] == 1
    assert admin.execute("SELECT count(*) FROM billing.ledger_entries").fetchone()[0] == 1
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.outbox WHERE topic='billing.funding_posted'"
        ).fetchone()[0]
        == 1
    )


async def test_notification_storage_failure_never_returns_success(payments, admin, monkeypatch):
    from nexus.modules.billing.persistence.payments import PaymentRepository
    from nexus.shared.errors import ServiceError

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    original = PaymentRepository.create

    async def fail_event(self, kind, values):
        if kind == "payment_event":
            raise ServiceError("storage_unavailable", "test storage interruption", 503)
        return await original(self, kind, values)

    monkeypatch.setattr(PaymentRepository, "create", fail_event)
    notice = p.provider.notice(item["merchant_order_ref"])
    response = await notify(p, chan, notice)
    assert response.status_code == 503
    assert admin.execute("SELECT count(*) FROM billing.payment_events").fetchone()[0] == 0
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"
    monkeypatch.setattr(PaymentRepository, "create", original)
    assert (await notify(p, chan, notice)).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"


async def test_original_idempotency_window_cannot_be_extended_by_new_capabilities(
    payments, admin, monkeypatch
):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from nexus.modules.billing.persistence.payments import PaymentRepository

    p = payments
    registry = p.app.state.payment_processor._registry
    old = registry._definitions["mock"]

    def short_window(config):
        gateway = old.factory(config)
        gateway.capabilities = gateway.capabilities.model_copy(update={"idempotency_seconds": 1})
        return gateway

    registry._definitions["mock"] = replace(old, factory=short_window)
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    p.provider.drop_create_responses = 1
    await p.app.state.payment_processor.process(1)
    assert p.provider.create_calls == 1
    # Recovery now sees a longer advertised window, but the persisted first intent wins.
    registry._definitions["mock"] = old
    future = datetime.now(UTC) + timedelta(seconds=5)

    async def future_now(self):
        return future

    monkeypatch.setattr(PaymentRepository, "now", future_now)
    p.provider.query_override = lambda value: {
        "lookup_result": "not_found",
        "merchant_id": "merchant-a",
        "environment": "test",
        "merchant_order_ref": item["merchant_order_ref"],
    }
    await p.app.state.payment_processor.process()
    assert p.provider.create_calls == 1
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_cases WHERE reason='create_unknown'"
        ).fetchone()[0]
        == 1
    )
    result = await read_order(p, org, item)
    assert result["review_required"] and result["credit_state"] == "uncredited"


async def test_provider_calls_do_not_hold_a_database_connection_or_account_lock(payments):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    await order(p, org, chan)
    single = create_app(p.settings.model_copy(update={"db_pool_size": 1, "db_max_overflow": 0}))
    async with (
        single.router.lifespan_context(single),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=single, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        p.provider.create_barrier = asyncio.Event()
        task = asyncio.create_task(single.state.payment_processor.process(1))
        try:
            await asyncio.wait_for(p.provider.create_started.wait(), 2)
            response = await asyncio.wait_for(
                client.post(
                    f"/api/v1/organizations/{org['id']}/budget-policies",
                    headers=auth(p.root),
                    json={"limit_amount": "10", "currency": "USD", "period": "day"},
                ),
                1,
            )
            assert response.status_code == 201, response.text
        finally:
            p.provider.create_barrier.set()
            await task


async def test_payment_tenant_roles_and_client_paid_flags_cannot_write_ledger(payments, admin):
    from test_identity_api import create_user, login

    p = payments
    owner = await create_user(p.client, p.root, "payment-owner")
    await create_user(p.client, p.root, "payment-outsider")
    org = await create_org(p.client, p.root, "Role-scoped customer", owner["user"]["id"])
    await p.app.state.process_billing()
    owner_token = await login(p.client, "payment-owner")
    outsider_token = await login(p.client, "payment-outsider")
    chan = await channel(p)
    item = await order(p, org, chan, token=owner_token)
    base = f"/api/v1/organizations/{org['id']}/payment-orders/{item['id']}"
    assert (await p.client.get(base, headers=auth(outsider_token))).status_code == 404
    assert (
        await p.client.post(base + "/reconcile", headers=auth(owner_token), json={"paid": True})
    ).status_code == 403
    assert (await p.client.post(base + "/reconcile", headers=auth(p.root))).status_code == 403
    assert (
        await p.client.get("/api/v1/payment-channels", headers=auth(owner_token))
    ).status_code == 403
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders",
        headers=headers(owner_token, "client-forgery"),
        json={
            "channel_id": chan["id"],
            "amount": "10",
            "currency": "USD",
            "paid": True,
            "callback_url": "https://attacker.example/receive",
        },
    )
    assert response.status_code == 422
    assert admin.execute("SELECT count(*) FROM billing.ledger_entries").fetchone()[0] == 0
    await finance_role(p.client, p.root)
    response = await p.client.post(
        base + "/reconcile", headers=auth(p.root), json={"paid": True, "amount": "999"}
    )
    assert response.status_code == 202
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "uncredited"


async def test_failed_refund_is_not_treated_as_a_successful_reversal(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    obj = p.provider.paid(item["merchant_order_ref"])
    obj.update(reversal_state="failed", reversed_minor=0, reversal_reference="failed-refund")
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
    assert admin.execute("SELECT reversed_amount FROM billing.payment_orders").fetchone()[0] == 0


async def test_worker_process_recovers_payment_without_a_callback(
    payments, service_directory, admin
):
    import os
    import sys

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(service_directory / ".env.worker"),
        "worker",
        env=dict(
            os.environ,
            NEXUS_ENVIRONMENT="test",
            NEXUS_MOCK_PAYMENT_URL=p.provider.url,
            NEXUS_PAYMENT_PUBLIC_BASE_URL="https://control.nexus.test",
            NEXUS_PAYMENT_RETURN_URL="https://billing.nexus.test/payments",
            NEXUS_WORKER_ID="payment-integration-worker",
        ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            result = await read_order(p, org, item)
            if result["credit_state"] == "credited":
                break
            await asyncio.sleep(0.05)
        assert result["credit_state"] == "credited", result
        assert (await balance(p.client, p.root, org))["balance"] == "10.000000000000"
        process.terminate()
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_id": "wrong-merchant"},
        {"environment": "live"},
        {"currency": "CNY"},
        {"order_id": str(uuid4())},
        {"organization_id": str(uuid4())},
        {"provider_payment_ref": "wrong-payment-object"},
        {"merchant_order_ref": "wrong-order"},
        {"state": "processing"},
    ],
)
async def test_only_a_fully_bound_capture_can_fund_an_order(payments, changes):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    p.provider.query_override = lambda values: {**values, **changes}
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "uncredited"
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"


async def test_ignored_notices_body_limits_and_rate_limits_are_explicit(payments, admin):
    p = payments
    chan = await channel(p)
    irrelevant = {
        "merchant_id": "merchant-a",
        "environment": "test",
        "relevant": False,
        "event_id": "unrelated-event",
    }
    response = await notify(p, chan, irrelevant)
    assert response.status_code == 200
    assert (
        admin.execute("SELECT processing_state FROM billing.payment_events").fetchone()[0]
        == "ignored"
    )
    assert admin.execute("SELECT count(*) FROM billing.payment_operations").fetchone()[0] == 0
    # The protocol limit is checked even if an oversized raw body has a valid signature.
    import hashlib
    import hmac
    from datetime import UTC, datetime

    raw = b" " * 65537
    timestamp = int(datetime.now(UTC).timestamp())
    signature = hmac.new(
        SIGNING_KEY.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    response = await p.client.post(
        "/webhooks/payments/" + chan["id"],
        content=raw,
        headers={"X-Mock-Signature": f"t={timestamp},v1={signature}"},
    )
    assert response.status_code == 400
    response = await p.client.post("/webhooks/payments/" + chan["id"], content=b"x" * (1048576 + 1))
    assert response.status_code == 413
    p.app.state.payment_service._limits._ip_limit = 1
    response = await notify(p, chan, {**irrelevant, "event_id": "rate-limited"})
    assert response.status_code == 429
    assert admin.execute("SELECT count(*) FROM billing.payment_events").fetchone()[0] == 1


async def test_posted_payment_is_polled_for_reversals_without_notifications(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    p.provider.fact(item["merchant_order_ref"]).update(
        state="partially_reversed",
        reversed_minor=200,
        reversal_state="succeeded",
        reversal_reference="missed-refund-notice",
    )
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["payment_state"] == "partially_reversed"
    wallet = await balance(p.client, p.root, org)
    assert wallet["status"] == "frozen" and wallet["balance"] == "10.000000000000"


async def test_gateway_reversal_cannot_consume_inflight_funds(payments, admin):
    from test_billing import adjustment, setup_model

    from nexus.modules.billing import Settlement

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    _, _, _, _, admission = await setup_model(p.http, org)
    await p.app.state.reservations.reserve(admission)
    p.provider.fact(item["merchant_order_ref"]).update(
        state="partially_reversed",
        reversed_minor=300,
        reversal_state="succeeded",
        reversal_reference="partial-refund-held",
    )
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    await finance_role(p.client, p.root)
    paid = await read_order(p, org, item)
    correction = await adjustment(p.client, p.root, org, paid["ledger_entry_id"], "3")
    path = f"/api/v1/organizations/{org['id']}/adjustments/{correction['id']}/post"
    result = await p.client.post(path, headers=headers(p.root, "reverse-held-payment", 1))
    assert result.status_code == 409
    wallet = await balance(p.client, p.root, org)
    assert wallet["balance"] == "10.000000000000" and wallet["held_amount"] == "10.000000000000"
    assert (
        admin.execute(
            "SELECT amount_at_risk FROM billing.payment_cases WHERE reason='payment_reversed'"
        ).fetchone()[0]
        == 3
    )
    await p.app.state.reservations.settle(
        admission.organization_id,
        admission.request_id,
        Settlement(
            outcome="not_started",
            evidence_reference="never-dispatched",
            normalization_version="test.v1",
        ),
    )
    assert (
        await p.client.post(path, headers=headers(p.root, "reverse-held-payment", 1))
    ).status_code == 200
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    await p.app.state.payment_processor.process()
    assert (await balance(p.client, p.root, org))["balance"] == "7.000000000000"
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert (
        admin.execute(
            "SELECT amount_at_risk FROM billing.payment_cases WHERE reason='payment_reversed'"
        ).fetchone()[0]
        == 0
    )


async def test_account_capacity_failure_keeps_verified_payment_for_reconciliation(payments, admin):
    from test_billing import adjustment, funding, post_funding, receiving

    from nexus.modules.billing.domain.money import MAX_AMOUNT

    p = payments
    org = await enterprise(p)
    await finance_role(p.client, p.root)
    receiver = await receiving(p.client, p.root)
    registered = await funding(p.client, p.root, org, receiver, str(MAX_AMOUNT))
    await post_funding(p.client, p.root, org, registered)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    waiting = await read_order(p, org, item)
    assert waiting["payment_state"] == "succeeded" and waiting["credit_state"] == "uncredited"
    assert waiting["review_reason"] == "wallet_capacity_unavailable"
    assert admin.execute("SELECT count(*) FROM billing.payment_receipts").fetchone()[0] == 1
    source = admin.execute(
        "SELECT id FROM billing.ledger_entries WHERE kind='funding_credit'"
    ).fetchone()[0]
    correction = await adjustment(p.client, p.root, org, str(source), "10")
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/adjustments/{correction['id']}/post",
        headers=headers(p.root, "free-capacity", 1),
    )
    assert response.status_code == 200, response.text
    await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders/{item['id']}/reconcile",
        headers=auth(p.root),
    )
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["credit_state"] == "credited"
    assert Decimal((await balance(p.client, p.root, org))["balance"]) == MAX_AMOUNT


async def test_finance_can_inspect_payment_evidence_without_secrets_or_checkout_tokens(payments):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    base = f"/api/v1/organizations/{org['id']}/payment-orders/{item['id']}"
    assert (await p.client.get(base + "/observations", headers=auth(p.root))).status_code == 403
    await finance_role(p.client, p.root)
    evidence = await p.client.get(base + "/observations", headers=auth(p.root))
    receipts = await p.client.get(base + "/receipts", headers=auth(p.root))
    assert evidence.status_code == receipts.status_code == 200
    assert evidence.json()["items"] and len(receipts.json()["items"]) == 1
    assert all("checkout" not in item["facts"] for item in evidence.json()["items"])
    assert (
        API_KEY not in evidence.text + receipts.text
        and SIGNING_KEY not in evidence.text + receipts.text
    )


async def test_lost_create_response_recovers_an_unpaid_checkout(payments):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    p.provider.drop_create_responses = 1
    await p.app.state.payment_processor.process(1)
    assert (await read_order(p, org, item))["checkout_state"] == "unknown"
    await p.app.state.payment_processor.process()
    result = await read_order(p, org, item)
    assert result["checkout_state"] == "ready" and result["checkout"] is not None
    assert result["review_required"] is False and result["credit_state"] == "uncredited"
    assert p.provider.create_calls == 1
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"


async def test_unregistered_gateway_does_not_block_wallet_reads_or_fabricate_notifications(
    payments,
):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    registry = p.app.state.payment_service._registry
    registry._definitions.pop("mock")
    assert (await read_order(p, org, item))["credit_state"] == "uncredited"
    assert (await balance(p.client, p.root, org))["balance"] == "0.000000000000"
    response = await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))
    assert response.status_code == 503
    response = await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders",
        headers=headers(p.root, uuid4().hex),
        json={"channel_id": chan["id"], "amount": "10", "currency": "USD"},
    )
    assert response.status_code == 422


async def test_payment_migration_cannot_erase_retained_remote_intents(
    payments, migration_settings, admin
):
    from alembic import command
    from alembic.config import Config

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    with pytest.raises(Exception, match="cannot downgrade while payment orders are retained"):
        await asyncio.to_thread(command.downgrade, config, "0006_pricing_reservations")
    assert (
        admin.execute("SELECT version_num FROM nexus_meta.alembic_version").fetchone()[0]
        == SCHEMA_REVISION
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_operations WHERE order_id=%s", (item["id"],)
        ).fetchone()[0]
        == 2
    )
    assert (await read_order(p, org, item))["credit_state"] == "uncredited"


async def test_stale_invalid_query_does_not_replace_a_newer_verified_credit(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await create_remote(p, org, item)
    p.provider.paid(item["merchant_order_ref"])
    p.provider.query_override = lambda values: {**values, "currency": "CNY"}
    barrier = asyncio.Event()
    p.provider.query_barrier = barrier
    old = asyncio.create_task(p.app.state.payment_processor.process(1))
    try:
        await asyncio.wait_for(p.provider.query_started.wait(), 2)
        p.provider.query_override = None
        p.provider.query_barrier = None
        admin.execute(
            "UPDATE billing.payment_operations "
            "SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE kind='query' AND state='running'"
        )
        await p.app.state.payment_processor.process(1)
        assert (await read_order(p, org, item))["credit_state"] == "credited"
    finally:
        barrier.set()
        await old
    result = await read_order(p, org, item)
    assert result["credit_state"] == "credited" and result["review_required"] is False
    wallet = await balance(p.client, p.root, org)
    assert wallet["status"] == "active" and wallet["balance"] == "10.000000000000"
    assert admin.execute("SELECT count(*) FROM billing.payment_cases").fetchone()[0] == 0
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_observations WHERE disposition='invalid_binding'"
        ).fetchone()[0]
        == 1
    )


async def test_worker_rate_limit_is_shared_by_channels_and_workers(payments, admin):
    from nexus.modules.billing.application.payments.processor import PaymentProcessor

    p = payments
    org = await enterprise(p)
    first = await channel(p)
    second = await channel(p, name="Same merchant second channel")
    await order(p, org, first)
    await order(p, org, second)
    primary = p.app.state.payment_processor
    primary._requests_per_minute = 1
    other = PaymentProcessor(
        primary._transactions, primary._cipher, primary._registry, requests_per_minute=1
    )
    await asyncio.gather(primary.process(1), other.process(1))
    assert p.provider.create_calls + p.provider.query_calls == 1
    assert admin.execute(
        "SELECT count(*),sum(request_count) FROM billing.payment_rate_buckets"
    ).fetchone() == (1, 1)
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_operations WHERE last_error='rate_limited'"
        ).fetchone()[0]
        == 1
    )
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_operations WHERE last_error='rate_limited'"
            " AND attempts=0"
        ).fetchone()[0]
        == 1
    )


async def test_rate_limited_recovery_reuses_a_verified_query_before_safe_retry(payments, admin):
    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    processor = p.app.state.payment_processor
    original_transport = processor._registry._definitions["mock"]
    from dataclasses import replace

    from nexus.modules.billing.application.payments.registry import GatewayFailure

    def before_remote_create(config):
        gateway = original_transport.factory(config)

        async def lost_before_provider(expectation, key):
            raise GatewayFailure("unconfirmed")

        gateway.create_payment = lost_before_provider
        return gateway

    processor._registry._definitions["mock"] = replace(
        original_transport, factory=before_remote_create
    )
    await processor.process(1)
    processor._registry._definitions["mock"] = original_transport
    processor._requests_per_minute = 1
    admin.execute(
        "UPDATE billing.payment_rate_buckets "
        "SET window_start=clock_timestamp()-interval '61 seconds'"
    )
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET next_attempt_at=clock_timestamp()-interval '2 seconds' WHERE kind='create'"
    )
    await processor.process(1)
    assert p.provider.query_calls == 1 and p.provider.create_calls == 0
    assert (
        admin.execute(
            "SELECT retry_observation_id IS NOT NULL FROM billing.payment_operations "
            "WHERE kind='create'"
        ).fetchone()[0]
        is True
    )
    admin.execute(
        "UPDATE billing.payment_rate_buckets "
        "SET window_start=clock_timestamp()-interval '61 seconds'"
    )
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET next_attempt_at=clock_timestamp()-interval '2 seconds' WHERE kind='create'"
    )
    await processor.process(1)
    assert p.provider.query_calls == 1 and p.provider.create_calls == 1
    await processor.process(1)
    # The unpaid checkout is reconciled once the normal query gets its next rate slot.
    admin.execute(
        "UPDATE billing.payment_rate_buckets "
        "SET window_start=clock_timestamp()-interval '61 seconds'"
    )
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    await processor.process(1)
    assert (await read_order(p, org, item))["checkout_state"] == "ready"


async def test_not_found_from_wrong_merchant_cannot_authorize_a_create_retry(payments, admin):
    from dataclasses import replace

    from nexus.modules.billing.application.payments.registry import GatewayFailure

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    processor = p.app.state.payment_processor
    original = processor._registry._definitions["mock"]

    def no_remote(config):
        gateway = original.factory(config)

        async def unknown(expectation, key):
            raise GatewayFailure("unconfirmed")

        gateway.create_payment = unknown
        return gateway

    processor._registry._definitions["mock"] = replace(original, factory=no_remote)
    await processor.process(1)
    processor._registry._definitions["mock"] = original
    p.provider.query_override = lambda values: {**values, "merchant_id": "wrong-merchant"}
    admin.execute(
        "UPDATE billing.payment_operations "
        "SET next_attempt_at=clock_timestamp() WHERE kind='create'"
    )
    await processor.process()
    assert p.provider.create_calls == 0
    assert (await read_order(p, org, item))["review_required"] is True
    assert (
        admin.execute(
            "SELECT count(*) FROM billing.payment_cases WHERE reason='payment_binding_conflict'"
        ).fetchone()[0]
        == 1
    )


async def test_resolved_refund_replay_and_independent_unfreeze(payments, admin):
    from test_billing import adjustment

    p = payments
    org = await enterprise(p)
    chan = await channel(p)
    item = await order(p, org, chan)
    await settle_paid(p, org, item, chan)
    obj = p.provider.fact(item["merchant_order_ref"])
    obj.update(
        state="partially_reversed",
        reversed_minor=300,
        reversal_state="succeeded",
        reversal_reference="reversal-001",
    )
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    await finance_role(p.client, p.root)
    base = f"/api/v1/organizations/{org['id']}"
    cases = (await p.client.get("/api/v1/reconciliation-cases", headers=auth(p.root))).json()[
        "items"
    ]
    obs = admin.execute(
        "SELECT verified_observation_id FROM billing.payment_orders WHERE id=%s", (item["id"],)
    ).fetchone()[0]
    body = {
        "action": "verify_payment",
        "reason": "Reversal verified",
        "evidence": "ops-bank-ref",
        "observation_id": str(obs),
    }

    async def resolve(case):
        return await p.client.post(
            f"/api/v1/reconciliation-cases/{case['id']}/resolve",
            headers=headers(p.root, "resolve-" + case["id"], case["version"]),
            json=body,
        )

    assert (await resolve(cases[0])).status_code == 409
    funding = (await read_order(p, org, item))["ledger_entry_id"]
    draft = await adjustment(p.client, p.root, org, funding, "3")
    assert (
        await p.client.post(
            base + f"/adjustments/{draft['id']}/post", headers=headers(p.root, "book-refund", 1)
        )
    ).status_code == 200
    for case in cases:
        reply = await resolve(case)
        assert reply.status_code == 200, reply.text
    wallet = await balance(p.client, p.root, org)
    reply = await p.client.post(
        base + "/account/unfreeze",
        headers=headers(p.root, "close-payment-freeze", wallet["version"]),
        json={"reason": "Verified funding reversal", "evidence": "ops-bank-ref"},
    )
    assert reply.status_code == 200, reply.text
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    await p.app.state.payment_processor.process()
    wallet = await balance(p.client, p.root, org)
    assert wallet["status"] == "active" and Decimal(wallet["balance"]) == 7
    obj.update(reversed_minor=400, reversal_reference="reversal-002")
    assert (await notify(p, chan, p.provider.notice(item["merchant_order_ref"]))).status_code == 200
    assert (await balance(p.client, p.root, org))["status"] == "frozen"


@pytest.mark.capacity
async def test_confirmed_payment_posting_latency(payments, admin, service_directory):
    from redis.asyncio import Redis
    from test_control_plane_acceptance import worker

    p = payments
    org, chan = await enterprise(p), await channel(p)
    records = [await order(p, org, chan) for _ in range(24)]
    # Dispatch every creation; query compensation can run between two create operations.
    async with asyncio.timeout(20):
        while admin.execute(
            "SELECT count(*) FROM billing.payment_orders WHERE checkout_state='ready'"
        ).fetchone()[0] != len(records):
            if not await p.app.state.payment_processor.process():
                await asyncio.sleep(0.05)
    ids = [row["id"] for row in records]
    async with (
        Redis.from_url(p.settings.redis_url.get_secret_value()) as redis,
        worker(service_directory, redis, p.settings.redis_namespace, mock_url=p.provider.url),
    ):
        # Pace confirmations; create, query and credit use the actual local mock HTTP provider.
        for item in records:
            ref = item["merchant_order_ref"]
            p.provider.paid(ref)
            response = await notify(p, chan, p.provider.notice(ref))
            assert response.status_code == 200, response.text
            await asyncio.sleep(0.1)
        for _ in range(200):
            if admin.execute(
                "SELECT count(*) FROM billing.payment_orders WHERE credit_state='credited'"
            ).fetchone()[0] == len(ids):
                break
            await asyncio.sleep(0.05)
        result = admin.execute(
            """
            SELECT count(l.id),count(*) FILTER (WHERE o.review_required),
             percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM
                 pg_xact_commit_timestamp(l.xmin)-pg_xact_commit_timestamp(v.xmin))),
             percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM
                 pg_xact_commit_timestamp(l.xmin)-e.created_at))
             FROM billing.payment_orders o
             LEFT JOIN billing.ledger_entries l ON l.id=o.ledger_entry_id
             LEFT JOIN billing.funding_records f ON f.id=o.funding_record_id
             LEFT JOIN billing.payment_receipts p ON p.id=f.payment_receipt_id
             LEFT JOIN billing.payment_observations v ON v.id=p.observation_id
             LEFT JOIN billing.payment_events e ON e.order_id=o.id
             WHERE o.id=ANY(%s::uuid[])
        """,
            (ids,),
        ).fetchone()
        posted, review, confirmed_p95, webhook_p95 = result
        print(
            "payment",
            {
                "orders": len(ids),
                "unfinished": len(ids) - posted,
                "review": review,
                "confirmation_commit_p95_s": str(confirmed_p95),
                "notice_to_credit_p95_s": str(webhook_p95),
            },
        )
        assert posted == len(ids) and review == 0 and confirmed_p95 <= 2
        assert account_amounts(admin, org) == (
            240,
            0,
        )
