"""Exercise hosted adapters through API, durable Worker tasks, signed hooks and ledger."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from test_billing import adjustment, balance, finance_role, headers
from test_identity_api import auth
from test_payments import channel, enterprise, order, read_order
from test_payments import payments as provider_setup  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def keys():
    merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    platform = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        merchant,
        platform,
        merchant.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        platform.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


def sign(key, data):
    return base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA256())).decode()


def canonical(params):
    return "&".join(
        f"{k}={params[k]}"
        for k in sorted(params)
        if k not in {"sign", "sign_type"} and params[k] != ""
    ).encode()


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_epay_freezes_fx_and_checks_signed_receipts(version, keys, provider_setup, admin):  # noqa: F811
    p = provider_setup
    merchant, platform, private_pem, public_pem = keys
    ref = None
    secret = "test-only-epay-key"
    wire = {"money": "72.50", "status": "1"}

    def handler(req):
        if version == "v2":
            form = dict(parse_qsl(req.content.decode()))
            assert req.url.path == "/api/pay/query" and form["pid"] == "1001"
            merchant.public_key().verify(
                base64.b64decode(form["sign"]), canonical(form), padding.PKCS1v15(), hashes.SHA256()
            )
        else:
            assert req.url.path == "/api.php" and req.url.params["key"] == secret
        result = {
            "pid": "1001",
            "out_trade_no": ref,
            "trade_no": "2026123401",
            "type": "alipay",
            "code": "0" if version == "v2" else "1",
            **wire,
        }
        if version == "v2":
            result.update(timestamp=str(int(time.time())))
            result.update(sign=sign(platform, canonical(result)), sign_type="RSA")
        return httpx.Response(200, json=result)

    # The app already exists when pytest constructs the payments fixture.
    registry = p.app.state.payment_service._registry
    cfg = {
        "version": version,
        "base_url": "https://epay.example",
        "pid": "1001",
        "environment": "test",
        "payment_type": "alipay",
        "cny_per_usd": "7.25",
        **(
            {"merchant_key": secret}
            if version == "v1"
            else {"private_key": private_pem, "platform_public_key": public_pem}
        ),
    }
    _, gateway = registry.normalize("epay", cfg)
    gateway.transport.transport = httpx.MockTransport(handler)
    org = await enterprise(p)
    chan = await channel(p, adapter_type="epay", stable_merchant_id="1001", configuration=cfg)
    item = await order(p, org, chan)
    ref = item["merchant_order_ref"]
    assert (
        Decimal(item["collection_amount"]) == Decimal("72.5")
        and item["collection_currency"] == "CNY"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE billing.payment_orders SET collection_amount=80,exchange_rate=8 WHERE id=%s",
            (item["id"],),
        )
    changed = await p.client.patch(
        "/api/v1/payment-channels/" + chan["id"],
        headers=headers(p.root, "rotate", chan["version"]),
        json={"configuration": {**cfg, "cny_per_usd": "8.00"}},
    )
    assert changed.status_code == 200, changed.text
    assert await p.app.state.payment_processor.process(1) == 1
    item = await read_order(p, org, item)
    form = dict(parse_qsl(urlsplit(item["checkout"]["destination"]).query))
    assert form["money"] == "72.50" and form["out_trade_no"] == ref
    if version == "v2":
        merchant.public_key().verify(
            base64.b64decode(form["sign"]), canonical(form), padding.PKCS1v15(), hashes.SHA256()
        )
    else:
        assert (
            hashlib.md5(canonical(form) + secret.encode(), usedforsecurity=False).hexdigest()
            == form["sign"]
        )
    notice = {
        "pid": "1001",
        "money": "72.50",
        "out_trade_no": ref,
        "trade_status": "TRADE_SUCCESS",
        "trade_no": "2026123401",
        "type": "alipay",
    }
    if version == "v2":
        notice.update(timestamp=str(int(time.time())))
        notice["sign"] = sign(platform, canonical(notice))
    else:
        notice["sign"] = hashlib.md5(
            canonical(notice) + secret.encode(), usedforsecurity=False
        ).hexdigest()
    url = "/webhooks/payments/" + chan["id"]
    assert (await p.client.get(url, params={**notice, "money": "99"})).status_code == 400
    assert (await p.client.get(url, params=[*notice.items(), ("pid", "1001")])).status_code == 400
    for _ in range(2):
        assert (await p.client.get(url, params=notice)).text == "success"
        await p.app.state.payment_processor.process()
    assert admin.execute("SELECT amount,currency FROM billing.payment_receipts").fetchall() == [
        (Decimal("72.50"), "CNY")
    ]
    assert admin.execute(
        "SELECT balance,currency FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (10, "USD")
    assert admin.execute("SELECT count(*) FROM billing.ledger_entries").fetchone() == (1,)
    await finance_role(p.client, p.root)
    base = f"/api/v1/organizations/{org['id']}"
    assert (await p.client.post(base + "/account/reconcile", headers=auth(p.root))).json()[
        "consistent"
    ]
    wire["refundmoney"] = "36.25"
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    await p.app.state.payment_processor.process()
    assert (await balance(p.client, p.root, org))["status"] == "frozen"
    assert admin.execute(
        "SELECT amount_at_risk FROM billing.payment_cases WHERE reason='payment_reversed'"
    ).fetchone() == (5,)
    draft = await adjustment(
        p.client, p.root, org, (await read_order(p, org, item))["ledger_entry_id"], "5"
    )
    assert (
        await p.client.post(
            base + f"/adjustments/{draft['id']}/post", headers=headers(p.root, "reverse", 1)
        )
    ).status_code == 200
    case = admin.execute(
        "SELECT id,version FROM billing.payment_cases WHERE reason='payment_reversed'"
    ).fetchone()
    obs = admin.execute(
        "SELECT verified_observation_id FROM billing.payment_orders WHERE id=%s", (item["id"],)
    ).fetchone()[0]
    resolved = await p.client.post(
        f"/api/v1/reconciliation-cases/{case[0]}/resolve",
        headers=headers(p.root, "resolve", case[1]),
        json={
            "action": "verify_payment",
            "observation_id": str(obs),
            "reason": "Matched reversal",
            "evidence": "bank-test",
        },
    )
    assert resolved.status_code == 200, resolved.text


async def test_stripe_requires_captured_charge(provider_setup, admin):  # noqa: F811
    p = provider_setup
    cfg = {
        "account_id": "acct_test123",
        "secret_key": "sk_test_protocol",
        "webhook_secret": "whsec_testprotocol",
    }
    captured = False
    refunded = 0
    fields = {}

    def handler(req):
        assert (
            req.url.host == "api.stripe.com"
            and req.headers["authorization"] == "Bearer sk_test_protocol"
        )
        if req.url.path == "/v1/account":
            return httpx.Response(200, json={"id": "acct_test123", "object": "account"})
        if req.method == "POST":
            assert req.headers["idempotency-key"]
            fields.update(parse_qsl(req.content.decode()))
        meta = {"nexus_order": fields["client_reference_id"], "nexus_channel": chan["id"]}
        charge = {
            "object": "charge",
            "id": "ch_s",
            "payment_intent": "pi_s",
            "paid": True,
            "captured": captured,
            "status": "succeeded",
            "amount_captured": 1000,
            "amount_refunded": refunded,
            "disputed": False,
            "currency": "usd",
            "livemode": False,
            "created": int(time.time()),
        }
        obj = {
            "id": "cs_test_s",
            "object": "checkout.session",
            "mode": "payment",
            "metadata": meta,
            "client_reference_id": meta["nexus_order"],
            "amount_total": 1000,
            "currency": "usd",
            "livemode": False,
            "url": "https://checkout.stripe.com/c/pay/cs_test_s",
            "expires_at": int(time.time()) + 3600,
            "status": "open" if req.method == "POST" else "complete",
            "payment_status": "paid",
            "payment_intent": {
                "object": "payment_intent",
                "id": "pi_s",
                "status": "succeeded",
                "amount_received": 1000,
                "currency": "usd",
                "metadata": meta,
                "latest_charge": charge,
            },
        }
        return httpx.Response(200, json=obj)

    p.app.state.payment_service._registry.normalize("stripe", cfg)[
        1
    ].transport.transport = httpx.MockTransport(handler)
    org = await enterprise(p)
    chan = await channel(
        p, adapter_type="stripe", stable_merchant_id=cfg["account_id"], configuration=cfg
    )
    item = await order(p, org, chan)
    await p.app.state.payment_processor.process(1)
    assert (await read_order(p, org, item))["checkout"]["destination"].startswith(
        "https://checkout.stripe.com/"
    )
    event = json.dumps(
        {
            "object": "event",
            "id": "evt_one",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "object": "checkout.session",
                    "id": "cs_test_s",
                    "client_reference_id": item["merchant_order_ref"],
                    "amount_total": 1000,
                    "currency": "usd",
                }
            },
        }
    ).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(
        cfg["webhook_secret"].encode(), stamp.encode() + b"." + event, hashlib.sha256
    ).hexdigest()
    url = "/webhooks/payments/" + chan["id"]
    h = {"Stripe-Signature": f"t={stamp},v1={sig}"}
    assert (await p.client.post(url, headers=h, content=event + b" ")).status_code == 400
    assert (await p.client.post(url, headers=h, content=event)).status_code == 200
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (0,)
    captured = True
    await finance_role(p.client, p.root)
    retried = await p.client.post(
        f"/api/v1/organizations/{org['id']}/payment-orders/{item['id']}/reconcile",
        headers=headers(p.root, "lookup"),
    )
    assert retried.status_code == 202, retried.text
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (10,)
    assert admin.execute("SELECT transaction_id FROM billing.payment_receipts").fetchall() == [
        ("ch_s",)
    ]

    refunded = 200
    refund = json.dumps(
        {
            "object": "event",
            "id": "evt_refunded",
            "type": "charge.refunded",
            "livemode": False,
            "data": {
                "object": {
                    "object": "charge",
                    "id": "ch_s",
                    "metadata": {},
                    "currency": "usd",
                    "amount_refunded": refunded,
                }
            },
        }
    ).encode()
    # The charge has no local order metadata; bind it through the original receipt.
    h = {
        "Stripe-Signature": f"t={stamp},v1="
        + hmac.new(
            cfg["webhook_secret"].encode(), stamp.encode() + b"." + refund, hashlib.sha256
        ).hexdigest()
    }
    assert (await p.client.post(url, headers=h, content=refund)).status_code == 200
    assert (await balance(p.client, p.root, org))["status"] == "frozen"
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT amount_at_risk FROM billing.payment_cases WHERE reason='payment_reversed'"
    ).fetchone() == (2,)


async def test_antom_verifies_response_and_waits_for_capture(provider_setup, admin, keys):  # noqa: F811
    p = provider_setup
    merchant, platform, private_pem, public_pem = keys
    cfg = {
        "client_id": "SANDBOX_app",
        "region": "asia",
        "environment": "test",
        "private_key": private_pem,
        "platform_public_key": public_pem,
    }
    submitted = False
    capture = False
    tamper = False
    ref = None

    def signed(path, raw, header):
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        signature = sign(platform, f"POST {path}\nSANDBOX_app.{stamp}.".encode() + raw)
        return {
            header: stamp,
            "Client-Id": "SANDBOX_app",
            "Signature": "algorithm=RSA256,keyVersion=1,signature=" + quote(signature, safe=""),
        }

    def handler(req):
        path = req.url.path
        assert req.url.host == "open-sea-global.alipay.com" and path.startswith(
            "/ams/sandbox/api/v1/payments/"
        )
        content = f"POST {path}\nSANDBOX_app.{req.headers['request-time']}.".encode() + req.content
        merchant.public_key().verify(
            base64.b64decode(unquote(req.headers["signature"].split("signature=")[1])),
            content,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        args = json.loads(req.content)
        result = {"result": {"resultCode": "SUCCESS", "resultStatus": "S"}}
        if path.endswith("createPaymentSession"):
            assert args["paymentNotifyUrl"].endswith("/orders/" + item["id"])
            assert args["paymentRequestId"] == ref and args["paymentAmount"] == {
                "currency": "USD",
                "value": "1000",
            }
            result.update(
                normalUrl="https://checkout.antom.com/session/example",
                paymentSessionExpiryTime=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            )
        else:
            assert args in ({"paymentRequestId": ref}, {"paymentId": "payment_one"})
            amount = {"currency": "USD", "value": "1000"}
            result.update(
                paymentStatus="SUCCESS",
                paymentRequestId=ref,
                paymentId="payment_one",
                paymentAmount=amount,
                transactions=[
                    {
                        "transactionType": "CAPTURE",
                        "transactionAmount": amount,
                        "transactionResult": {"resultStatus": "S"},
                    }
                ]
                if capture
                else [],
            )
        if path.endswith("inquiryPayment") and not submitted:
            result = {"result": {"resultCode": "ORDER_NOT_EXIST", "resultStatus": "F"}}
        raw = json.dumps(result).encode()
        return httpx.Response(
            200, headers=signed(path, raw, "Response-Time"), content=raw + (b" " if tamper else b"")
        )

    p.app.state.payment_service._registry.normalize("antom", cfg)[
        1
    ].transport.transport = httpx.MockTransport(handler)
    org = await enterprise(p)
    chan = await channel(
        p, adapter_type="antom", stable_merchant_id=cfg["client_id"], configuration=cfg
    )
    item = await order(p, org, chan)
    ref = item["merchant_order_ref"]
    await p.app.state.payment_processor.process()
    assert (await read_order(p, org, item))["checkout"]["destination"].startswith(
        "https://checkout.antom.com/"
    )
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (0,)
    submitted = True
    url = "/webhooks/payments/" + chan["id"] + "/orders/" + item["id"]
    raw = json.dumps(
        {
            "notifyType": "CAPTURE_RESULT",
            "paymentId": "payment_one",
            "captureId": "capture_one",
            "result": {"resultStatus": "S"},
        }
    ).encode()
    h = signed(url, raw, "Request-Time")
    assert (await p.client.post(url, content=raw + b" ", headers=h)).status_code == 400
    assert (await p.client.post(url, content=raw, headers=h)).json()["result"][
        "resultStatus"
    ] == "S"
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (0,)
    capture = tamper = True
    admin.execute(
        "UPDATE billing.payment_operations SET next_attempt_at=clock_timestamp() WHERE kind='query'"
    )
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (0,)
    tamper = False
    admin.execute(
        "UPDATE billing.payment_operations SET state='pending',next_attempt_at=clock_timestamp() "
        "WHERE kind='query'"
    )
    await p.app.state.payment_processor.process()
    assert admin.execute(
        "SELECT balance FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (10,)
    assert admin.execute("SELECT transaction_id FROM billing.payment_receipts").fetchall() == [
        ("payment_one",)
    ]
