"""Antom AMS hosted Checkout Page; signed responses and automatic card capture."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import quote, unquote
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from nexus.shared.errors import ServiceError

from ..application.payments.registry import GatewayFailure, validate_amount
from ..application.payments.schemas import (
    CheckoutData,
    CheckoutResult,
    GatewayCapabilities,
    PaymentObservation,
    VerifiedNotice,
)
from ..application.schemas import Schema
from .protocol import (
    HostedGateway,
    cents,
    document,
    identifier,
    one_header,
    require,
    rsa_key,
    rsa_sign,
    rsa_verify,
    unique,
)

ENDPOINTS = {
    "na": "open-na-global.alipay.com",
    "us": "open.antglobal-us.com",
    "asia": "open-sea-global.alipay.com",
    "eu": "open-de-global.alipay.com",
}


class AntomConfig(Schema):
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    region: Literal["na", "us", "asia", "eu"]
    environment: Literal["test", "live"]
    private_key: SecretStr = Field(repr=False)
    platform_public_key: str
    key_version: int = Field(default=1, ge=1)
    checkout_hosts: list[str] = Field(
        default_factory=lambda: ["checkout.antom.com"], min_length=1, max_length=16
    )

    @model_validator(mode="after")
    def keys(self):
        rsa_key(self.private_key.get_secret_value(), True)
        rsa_key(self.platform_public_key, False)
        require(all(h and all(c.isalnum() or c in ".-" for c in h) for h in self.checkout_hosts))
        return self


class AntomGateway(HostedGateway):
    acknowledgement = (
        b'{"result":{"resultCode":"SUCCESS","resultStatus":"S","resultMessage":"success"}}'
    )
    content_type = "application/json"

    def __init__(self, config, transport):
        super().__init__(config, transport)
        self.merchant, self.environment = config.client_id, config.environment
        self.capabilities = GatewayCapabilities(
            currencies={"USD": 2},
            supports_query=True,
            idempotency="none",
            idempotency_seconds=0,
            checkout_hosts=set(config.checkout_hosts),
        )

    def validate_rotation(self, previous):
        if self.config.region != previous["region"]:
            raise ServiceError(
                "gateway_binding_mismatch", "Create a new channel for this Antom region", 422
            )

    def signed_bytes(self, path, time, body):
        return f"POST {path}\n{self.merchant}.{time}.".encode() + body

    def verify(self, path, headers, body, time_name):
        require(one_header(headers, "client-id") == self.merchant)
        timestamp = one_header(headers, time_name)
        age = (
            self.now().timestamp() - int(timestamp) / 1000
            if timestamp.isdigit()
            else self.now().timestamp() - datetime.fromisoformat(timestamp).timestamp()
        )
        require(abs(age) <= 300)
        parts = unique(p.strip().split("=", 1) for p in one_header(headers, "signature").split(","))
        require(parts.get("algorithm") == "RSA256")
        rsa_verify(
            rsa_key(self.config.platform_public_key, False),
            self.signed_bytes(path, timestamp, body),
            unquote(parts["signature"]),
        )

    async def api(self, method, fields):
        prefix = "/ams/sandbox/api" if self.environment == "test" else "/ams/api"
        path = prefix + "/v1/payments/" + method
        raw = json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode()
        stamp = self.now().isoformat(timespec="milliseconds")
        sig = rsa_sign(
            rsa_key(self.config.private_key.get_secret_value(), True),
            self.signed_bytes(path, stamp, raw),
        )
        status, headers, body = await self.transport.send(
            "POST",
            "https://" + ENDPOINTS[self.config.region] + path,
            headers={
                "Content-Type": "application/json",
                "Client-Id": self.merchant,
                "Request-Time": stamp,
                "Signature": (
                    f"algorithm=RSA256,keyVersion={self.config.key_version},"
                    f"signature={quote(sig, safe='')}"
                ),
            },
            content=raw,
        )
        if status != 200:
            raise GatewayFailure("retryable_failure")
        self.verify(path, headers, body, "response-time")
        return document(body)

    async def create(self, e, key):
        amount = {
            "currency": e.currency,
            "value": str(validate_amount(e.amount, e.currency, self.capabilities)),
        }
        data = await self.api(
            "createPaymentSession",
            {
                "productCode": "CASHIER_PAYMENT",
                "productScene": "CHECKOUT_PAYMENT",
                "paymentRequestId": e.merchant_order_ref,
                "paymentAmount": amount,
                "paymentNotifyUrl": e.callback_url,
                "paymentRedirectUrl": e.return_url,
                "order": {
                    "referenceOrderId": e.merchant_order_ref,
                    "orderDescription": "Nexus wallet",
                    "orderAmount": amount,
                    "buyer": {"referenceBuyerId": str(e.organization_id)},
                },
            },
        )
        require(data["result"]["resultStatus"] == "S" and data["result"]["resultCode"] == "SUCCESS")
        return CheckoutResult(
            provider_payment_ref=None,
            checkout=CheckoutData(
                kind="redirect",
                destination=data["normalUrl"],
                expires_at=min(
                    e.expires_at, datetime.fromisoformat(data["paymentSessionExpiryTime"])
                ),
            ),
        )

    async def query(self, e, ref):
        data = await self.api(
            "inquiryPayment",
            {"paymentId": ref} if ref else {"paymentRequestId": e.merchant_order_ref},
        )
        base = dict(merchant_id=self.merchant, environment=self.environment, observed_at=self.now())
        if data["result"]["resultCode"] == "ORDER_NOT_EXIST":
            return PaymentObservation(
                lookup_result="awaiting_customer" if ref is None else "unconfirmed",
                **base,
                merchant_order_ref=e.merchant_order_ref,
            )
        require(data["result"]["resultStatus"] == "S" and data["result"]["resultCode"] == "SUCCESS")
        order = identifier(data["paymentRequestId"])
        pay = identifier(data["paymentId"])
        amount = cents(data["paymentAmount"]["value"])
        currency = data["paymentAmount"]["currency"]
        txs = data.get("transactions", [])
        require(isinstance(txs, list))
        captures = [
            tx
            for tx in txs
            if tx["transactionType"] == "CAPTURE" and tx["transactionResult"]["resultStatus"] == "S"
        ]
        status = data["paymentStatus"]
        state = {
            "SUCCESS": "processing",
            "PROCESSING": "processing",
            "FAIL": "failed",
            "CANCELLED": "cancelled",
        }.get(status, "unknown")
        if captures:
            # Only a single, final, full capture is eligible for this one-time wallet top-up.
            require(
                len(captures) == 1
                and captures[0]["transactionAmount"]["currency"] == currency
                and cents(captures[0]["transactionAmount"]["value"]) == amount
            )
            if status == "SUCCESS":
                state = "succeeded"
        elif status == "SUCCESS" and data.get("paymentMethodType") in {
            "ALIPAY_CN",
            "ALIPAY_HK",
            "GCASH",
            "DANA",
            "TNG",
            "TRUEMONEY",
        }:
            # Single-stage wallets settle on the final payment result.
            state = "succeeded"
        refunded = Decimal(0)
        refund_state = "none"
        for tx in txs:
            if tx["transactionType"] == "REFUND":
                require(tx["transactionAmount"]["currency"] == currency)
                outcome = tx["transactionResult"]["resultStatus"]
                if outcome == "S":
                    refunded += cents(tx["transactionAmount"]["value"])
                elif outcome == "U":
                    refund_state = "pending"
        if refunded:
            state = "reversed" if refunded == amount else "partially_reversed"
            refund_state = "succeeded"
        return PaymentObservation(
            **base,
            merchant_order_ref=order,
            provider_payment_ref=pay,
            transaction_id=pay,
            currency=currency,
            amount=amount,
            state=state,
            reversed_amount=refunded,
            reversal_state=refund_state,
        )

    def notice(self, envelope):
        require(not envelope.query)
        self.verify(envelope.path, envelope.headers, envelope.body, "request-time")
        data = document(envelope.body)
        kind = data.get("notifyType")
        relevant = (
            kind
            in {
                "PAYMENT_RESULT",
                "CAPTURE_RESULT",
                "REFUND_RESULT",
                "DISPUTE_CREATED",
                "DISPUTE_RESOLVED",
            }
            or "disputeId" in data
        )
        fact = dict(
            merchant_id=self.merchant,
            environment=self.environment,
            relevant=relevant,
            merchant_order_ref=data.get("paymentRequestId"),
            provider_payment_ref=data.get("paymentId"),
        )
        if "/orders/" in envelope.path:
            order_id = UUID(envelope.path.rsplit("/", 1)[1])
            bound_ref = "nxp_" + order_id.hex
            require(fact["merchant_order_ref"] in {None, bound_ref})
            fact.update(merchant_order_ref=bound_ref, order_id=order_id)
        if relevant and not fact["merchant_order_ref"] and not fact["provider_payment_ref"]:
            raise ValueError("unbound notification")
        if kind == "REFUND_RESULT" and data["result"]["resultStatus"] == "S":
            require(data["refundAmount"]["currency"] == "USD")
            fact.update(
                reversed_amount=cents(data["refundAmount"]["value"]),
                reversal_state="succeeded",
                reversal_reference=identifier(data["refundId"]),
            )
        if data.get("disputeId"):
            judgment = data.get("disputeJudgedResult")
            stage = data.get("disputeNotificationType")
            closed = stage == "DISPUTE_CANCELLED" or judgment == "ACCEPT_BY_CUSTOMER"
            lost = stage == "DISPUTE_ACCEPTED" or judgment == "ACCEPT_BY_MERCHANT"
            fact.update(
                dispute_reference=identifier(data["disputeId"]),
                dispute_state="won" if closed else "lost" if lost else "open",
            )
        return VerifiedNotice(**fact)
