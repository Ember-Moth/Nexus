"""Rainbow EPay signed page checkout and authenticated merchant order inquiry."""

import hashlib
import hmac
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import Field, SecretStr, model_validator

from nexus.shared.errors import ServiceError

from ..application.payments.registry import GatewayFailure
from ..application.payments.schemas import (
    CheckoutData,
    CheckoutResult,
    GatewayCapabilities,
    NotificationStyle,
    PaymentObservation,
    VerifiedNotice,
)
from ..application.schemas import Schema
from ..domain.money import MAX_AMOUNT
from .protocol import (
    HostedGateway,
    document,
    identifier,
    require,
    rsa_key,
    rsa_sign,
    rsa_verify,
    unique,
)


def endpoint_base(value):
    url = urlsplit(value)
    return "https://" + url.hostname + url.path.rstrip("/") + "/"


class EPayConfig(Schema):
    version: Literal["v1", "v2"]
    base_url: str
    pid: str = Field(pattern=r"^[0-9]{1,20}$")
    environment: Literal["test", "live"]
    payment_type: str = Field(pattern=r"^[a-zA-Z0-9_]{1,32}$")
    cny_per_usd: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?$")
    merchant_key: SecretStr | None = Field(default=None, repr=False)
    private_key: SecretStr | None = Field(default=None, repr=False)
    platform_public_key: str | None = None

    @model_validator(mode="after")
    def validate_credentials(self):
        url = urlsplit(self.base_url)
        require(
            url.scheme == "https"
            and url.hostname
            and url.port in {None, 443}
            and not (
                url.username or url.password or url.query or url.fragment or "\\" in self.base_url
            )
        )
        require(Decimal(self.cny_per_usd) > 0)
        if self.version == "v1":
            require(
                self.merchant_key
                and self.merchant_key.get_secret_value()
                and not self.private_key
                and not self.platform_public_key
            )
        else:
            require(not self.merchant_key and self.private_key and self.platform_public_key)
            rsa_key(self.private_key.get_secret_value(), True)
            rsa_key(self.platform_public_key, False)
        return self


class EPayGateway(HostedGateway):
    notification_style = NotificationStyle(methods=frozenset({"GET", "POST"}), mode="form")
    acknowledgement = b"success"

    def __init__(self, config, transport):
        super().__init__(config, transport)
        self.merchant, self.environment = config.pid, config.environment
        self.base = endpoint_base(config.base_url)
        self.receipt_host = self.base
        self.capabilities = GatewayCapabilities(
            currencies={"USD": 2},
            supports_query=True,
            idempotency="merchant_order_unique",
            idempotency_seconds=1800,
            checkout_hosts={urlsplit(self.base).hostname},
        )

    def validate_rotation(self, previous):
        if (self.base, self.config.version, self.config.payment_type) != (
            endpoint_base(previous["base_url"]),
            previous["version"],
            previous["payment_type"],
        ):
            raise ServiceError(
                "gateway_binding_mismatch",
                "Create a new channel for a different endpoint or payment method",
                422,
            )

    def quote(self, amount, currency):
        rate = Decimal(self.config.cny_per_usd)
        with localcontext() as ctx:
            ctx.prec = 60
            cny = (amount * rate).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if currency != "USD" or not 0 < cny <= MAX_AMOUNT:
            raise ServiceError("payment_precision", "Invalid EPay collection amount", 422)
        return cny, "CNY", rate

    @staticmethod
    def canonical(fields):
        parts = []
        for k, v in sorted(fields.items()):
            if k in {"sign", "sign_type"} or v is None or v == "":
                continue
            require(re.fullmatch(r"[A-Za-z0-9_]+", k) and type(v) in {int, str, Decimal})
            # EPay canonicalization is unescaped. Reject ambiguous field boundaries.
            require("&" not in str(v))
            parts.append(f"{k}={v}")
        return "&".join(parts).encode()

    def signed(self, fields):
        fields = {"pid": self.merchant, **fields}
        if self.config.version == "v2":
            fields["timestamp"] = str(int(self.now().timestamp()))
            return {
                **fields,
                "sign_type": "RSA",
                "sign": rsa_sign(
                    rsa_key(self.config.private_key.get_secret_value(), True),
                    self.canonical(fields),
                ),
            }
        return {**fields, "sign_type": "MD5", "sign": self.md5(fields)}

    def md5(self, fields):
        return hashlib.md5(
            self.canonical(fields) + self.config.merchant_key.get_secret_value().encode(),
            usedforsecurity=False,
        ).hexdigest()

    def verify(self, fields):
        if self.config.version == "v1":
            require(
                fields.get("sign_type", "MD5") == "MD5"
                and hmac.compare_digest(self.md5(fields), fields["sign"])
            )
        else:
            require(
                fields.get("sign_type", "RSA") == "RSA"
                and abs(int(fields["timestamp"]) - self.now().timestamp()) <= 300
            )
            rsa_verify(
                rsa_key(self.config.platform_public_key, False),
                self.canonical(fields),
                fields["sign"],
            )

    async def create(self, e, key):
        values = self.signed(
            {
                "out_trade_no": e.merchant_order_ref,
                "notify_url": e.callback_url,
                "return_url": e.return_url,
                "type": self.config.payment_type,
                "name": "Nexus wallet",
                "money": format(e.amount, ".2f"),
            }
        )
        path = "submit.php" if self.config.version == "v1" else "api/pay/submit"
        expiry = (
            e.expires_at
            if self.config.version == "v1"
            else min(e.expires_at, self.now() + timedelta(minutes=4))
        )
        return CheckoutResult(
            provider_payment_ref=e.merchant_order_ref,
            checkout=CheckoutData(
                kind="redirect",
                destination=self.base + path + "?" + urlencode(values),
                expires_at=expiry,
            ),
        )

    async def query(self, e, ref):
        v1 = self.config.version == "v1"
        if v1:
            status, _, raw = await self.transport.send(
                "GET",
                self.base + "api.php",
                params={
                    "act": "order",
                    "pid": self.merchant,
                    "key": self.config.merchant_key.get_secret_value(),
                    "out_trade_no": e.merchant_order_ref,
                },
            )
        else:
            status, _, raw = await self.transport.send(
                "POST",
                self.base + "api/pay/query",
                form=self.signed({"out_trade_no": e.merchant_order_ref}),
            )
        if status != 200:
            raise GatewayFailure("retryable_failure")
        data = document(raw)
        if str(data["code"]) != ("1" if v1 else "0"):
            if data.get("msg") in {
                "订单不存在",
                "订单号不存在",
                "订单不存在或未支付",
                "订单号不存在或未支付",
            }:
                checkout = (
                    (await self.create(e, "")).checkout if e.expires_at > self.now() else None
                )
                return PaymentObservation(
                    lookup_result="awaiting_customer",
                    merchant_id=self.merchant,
                    environment=self.environment,
                    merchant_order_ref=e.merchant_order_ref,
                    observed_at=self.now(),
                    checkout=checkout,
                )
            raise GatewayFailure("unconfirmed")
        if not v1:
            self.verify(data)
        require(str(data["pid"]) == self.merchant and data["type"] == self.config.payment_type)
        amount = Decimal(str(data["money"]))
        refunded = Decimal(str(data.get("refundmoney") or "0"))
        require(str(data.get("currency", "CNY")).upper() == "CNY")
        code = str(data["status"])
        require(0 <= refunded <= amount and amount > 0)
        state = {
            "0": "pending",
            "1": "succeeded",
            "2": "reversed",
            "3": "processing",
            "4": "processing",
        }.get(code, "unknown")
        if code == "2" and not refunded:
            refunded = amount
        if refunded:
            state = "reversed" if refunded == amount else "partially_reversed"
        if not refunded and code == "3":
            # The acquirer froze the collection. Keep the wallet restricted pending review.
            risk = {"dispute_state": "open", "disputed_amount": amount}
        else:
            risk = {}
        require(type(data["trade_no"]) in {str, int})
        trade = identifier(str(data["trade_no"]))
        checkout = (
            (await self.create(e, "")).checkout
            if state == "pending" and self.now() < e.expires_at
            else None
        )
        return PaymentObservation(
            **risk,
            merchant_id=str(data["pid"]),
            environment=self.environment,
            merchant_order_ref=data["out_trade_no"],
            provider_payment_ref=data["out_trade_no"],
            transaction_id=trade,
            amount=amount,
            currency="CNY",
            state=state,
            reversed_amount=refunded,
            reversal_state="succeeded" if refunded else "none",
            observed_at=self.now(),
            checkout=checkout,
        )

    def notice(self, envelope):
        raw = envelope.query if envelope.method == "GET" else envelope.body
        require(not (envelope.body if envelope.method == "GET" else envelope.query))
        fields = unique(
            parse_qsl(
                raw.decode(),
                keep_blank_values=True,
                strict_parsing=True,
                errors="strict",
                max_num_fields=64,
            )
        )
        self.verify(fields)
        require(
            fields["type"] == self.config.payment_type and fields["trade_status"] == "TRADE_SUCCESS"
        )
        return VerifiedNotice(
            merchant_id=fields["pid"],
            environment=self.environment,
            merchant_order_ref=identifier(fields["out_trade_no"]),
            transaction_id=identifier(fields["trade_no"]),
            provider_payment_ref=fields["out_trade_no"],
            amount=fields["money"],
            currency="CNY",
            state="succeeded",
        )
