"""Stripe Checkout Sessions backed by captured, server-verified Charges."""

import hashlib
import hmac
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field, SecretStr, model_validator

from ..application.payments.registry import GatewayFailure, validate_amount
from ..application.payments.schemas import (
    CheckoutData,
    CheckoutResult,
    GatewayCapabilities,
    PaymentObservation,
    VerifiedNotice,
)
from ..application.schemas import Schema
from .protocol import HostedGateway, cents, document, identifier, one_header, require


class StripeConfig(Schema):
    account_id: str = Field(pattern=r"^acct_[A-Za-z0-9]+$")
    secret_key: SecretStr = Field(repr=False)
    webhook_secret: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def keys(self):
        require(
            self.secret_key.get_secret_value().startswith(
                ("sk_live_", "sk_test_", "rk_live_", "rk_test_")
            )
        )
        require(self.webhook_secret.get_secret_value().startswith("whsec_"))
        return self


class StripeGateway(HostedGateway):
    capabilities = GatewayCapabilities(
        currencies={"USD": 2},
        supports_query=True,
        idempotency="native",
        idempotency_seconds=86400,
        checkout_hosts={"checkout.stripe.com"},
    )

    def __init__(self, config, transport):
        super().__init__(config, transport)
        self.merchant = config.account_id
        self.environment = (
            "live" if config.secret_key.get_secret_value().split("_")[1] == "live" else "test"
        )

    async def api(self, path, *, form=None, params=None, key=None):
        headers = {
            "Authorization": "Bearer " + self.config.secret_key.get_secret_value(),
            "Stripe-Version": "2024-06-20",
        }
        if key:
            headers["Idempotency-Key"] = key
        status, _, body = await self.transport.send(
            "GET" if form is None else "POST",
            "https://api.stripe.com/v1/" + path,
            headers=headers,
            form=form,
            params=params,
        )
        if status != 200:
            raise GatewayFailure(
                "retryable_failure" if status >= 500 or status == 429 else "manual_review"
            )
        return document(body)

    async def account(self):
        account = await self.api("account")
        require(account["object"] == "account" and account["id"] == self.merchant)
        return account["id"]

    def checkout(self, obj, e):
        if obj["status"] != "open" or not obj.get("url"):
            return None
        return CheckoutData(
            kind="redirect",
            destination=obj["url"],
            expires_at=min(e.expires_at, datetime.fromtimestamp(obj["expires_at"], UTC)),
        )

    async def create(self, e, key):
        await self.account()
        fields = {
            "mode": "payment",
            "payment_method_types[0]": "card",
            "success_url": e.return_url,
            "cancel_url": e.return_url,
            "client_reference_id": e.merchant_order_ref,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": e.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(
                validate_amount(e.amount, e.currency, self.capabilities)
            ),
            "line_items[0][price_data][product_data][name]": "Nexus wallet",
            "payment_intent_data[capture_method]": "automatic",
            # Stripe requires at least 30 minutes from creation. Omit expires_at and
            # hide expired local checkout; late captured payments still reconcile.
        }
        for prefix in ("metadata", "payment_intent_data[metadata]"):
            fields[prefix + "[nexus_order]"] = e.merchant_order_ref
            fields[prefix + "[nexus_channel]"] = str(e.channel_id)
        obj = await self.api("checkout/sessions", form=fields, key=key)
        self.session_binding(obj, e)
        return CheckoutResult(
            provider_payment_ref=identifier(obj["id"]), checkout=self.checkout(obj, e)
        )

    def session_binding(self, obj, e):
        require(
            obj["object"] == "checkout.session"
            and obj["mode"] == "payment"
            and obj["livemode"] is (self.environment == "live")
            and obj["client_reference_id"] == e.merchant_order_ref
            and obj["metadata"]["nexus_order"] == e.merchant_order_ref
            and obj["metadata"]["nexus_channel"] == str(e.channel_id)
        )
        require(cents(obj["amount_total"]) == e.amount and obj["currency"].upper() == e.currency)

    async def query(self, e, ref):
        merchant = await self.account()
        if ref is None:
            # Paginate the exact local order window. Never infer absence from one page.
            params = {"created[gte]": int(e.expires_at.timestamp()) - 3600, "limit": 100}
            for _ in range(20):
                page = await self.api("checkout/sessions", params=params)
                matches = [
                    s for s in page["data"] if s.get("client_reference_id") == e.merchant_order_ref
                ]
                if matches:
                    require(len(matches) == 1)
                    ref = identifier(matches[0]["id"])
                    break
                if page["has_more"] is False:
                    return PaymentObservation(
                        lookup_result="not_found",
                        merchant_id=merchant,
                        environment=self.environment,
                        merchant_order_ref=e.merchant_order_ref,
                        observed_at=self.now(),
                    )
                params["starting_after"] = identifier(page["data"][-1]["id"])
            if not ref:
                raise GatewayFailure("unconfirmed")
        obj = await self.api(
            "checkout/sessions/" + identifier(ref),
            params={"expand[]": "payment_intent.latest_charge"},
        )
        self.session_binding(obj, e)
        require(obj["id"] == ref)
        details = dict(
            merchant_id=merchant,
            environment=self.environment,
            merchant_order_ref=obj["client_reference_id"],
            provider_payment_ref=ref,
            amount=cents(obj["amount_total"]),
            currency=obj["currency"].upper(),
            observed_at=self.now(),
            checkout=self.checkout(obj, e),
        )
        if obj["payment_status"] != "paid":
            return PaymentObservation(
                **details, state="cancelled" if obj["status"] == "expired" else "pending"
            )
        pi = obj["payment_intent"]
        require(
            pi["object"] == "payment_intent"
            and pi["status"] == "succeeded"
            and pi["metadata"]["nexus_order"] == e.merchant_order_ref
            and pi["metadata"]["nexus_channel"] == str(e.channel_id)
            and cents(pi["amount_received"]) == e.amount
            and pi["currency"].upper() == e.currency
        )
        charge = pi["latest_charge"]
        require(
            charge["object"] == "charge"
            and charge["payment_intent"] == pi["id"]
            and charge["paid"] is True
            and charge["captured"] is True
            and charge["status"] == "succeeded"
            and charge["livemode"] is (self.environment == "live")
            and charge["currency"].upper() == e.currency
            and cents(charge["amount_captured"]) == e.amount
        )
        charge_id = identifier(charge["id"])
        reversed_amount = cents(charge["amount_refunded"])
        state = (
            "succeeded"
            if not reversed_amount
            else ("reversed" if reversed_amount == e.amount else "partially_reversed")
        )
        details.update(
            transaction_id=charge_id,
            state=state,
            reversed_amount=reversed_amount,
            reversal_state="succeeded" if reversed_amount else "none",
            received_at=datetime.fromtimestamp(charge["created"], UTC),
        )
        if charge["disputed"]:
            disputes = await self.api("disputes", params={"charge": charge_id, "limit": 2})
            require(len(disputes["data"]) == 1 and not disputes["has_more"])
            dispute = disputes["data"][0]
            require(dispute["charge"] == charge_id and dispute["currency"].upper() == e.currency)
            status = dispute["status"]
            won = status in {"won", "warning_closed"}
            details.update(
                dispute_reference=identifier(dispute["id"]),
                dispute_state="won" if won else "lost" if status == "lost" else "open",
                disputed_amount=Decimal(0) if won else cents(dispute["amount"]),
            )
        return PaymentObservation(**details)

    def notice(self, envelope):
        require(not envelope.query)
        pairs = [
            item.split("=", 1)
            for item in one_header(envelope.headers, "stripe-signature").split(",")
        ]
        times = [v for k, v in pairs if k == "t"]
        require(len(times) == 1 and abs(int(times[0]) - self.now().timestamp()) <= 300)
        signature = hmac.new(
            self.config.webhook_secret.get_secret_value().encode(),
            times[0].encode() + b"." + envelope.body,
            hashlib.sha256,
        ).hexdigest()
        require(any(hmac.compare_digest(signature, v) for k, v in pairs if k == "v1"))
        event = document(envelope.body)
        require(
            event["object"] == "event"
            and event["livemode"] is (self.environment == "live")
            and event.get("account", self.merchant) == self.merchant
        )
        obj = event["data"]["object"]
        common = dict(
            event_id=identifier(event["id"]),
            merchant_id=self.merchant,
            environment=self.environment,
        )
        if obj["object"] == "checkout.session" and event["type"].startswith("checkout.session."):
            return VerifiedNotice(
                **common,
                merchant_order_ref=obj.get("client_reference_id"),
                provider_payment_ref=identifier(obj["id"]),
                amount=cents(obj["amount_total"]),
                currency=obj["currency"].upper(),
            )
        if obj["object"] == "charge" and event["type"] == "charge.refunded":
            require(obj["currency"].upper() == "USD")
            return VerifiedNotice(
                **common,
                merchant_order_ref=obj.get("metadata", {}).get("nexus_order"),
                transaction_id=identifier(obj["id"]),
                state="unknown",
                reversed_amount=cents(obj["amount_refunded"]),
                reversal_state="succeeded",
            )
        if obj["object"] == "dispute" and event["type"].startswith("charge.dispute."):
            require(obj["currency"].upper() == "USD")
            return VerifiedNotice(
                **common,
                transaction_id=identifier(obj["charge"]),
                dispute_reference=identifier(obj["id"]),
                disputed_amount=cents(obj["amount"]),
                dispute_state={"won": "won", "lost": "lost"}.get(obj["status"], "open"),
            )
        return VerifiedNotice(**common, relevant=False)
