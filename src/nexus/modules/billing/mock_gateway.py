"""Test-only adapter for the Nexus mock payment protocol, never a real payment provider."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qsl

from pydantic import Field, SecretStr, ValidationError

from .gateways import GatewayDefinition, GatewayFailure, validate_amount
from .payment_schemas import (
    CheckoutResult,
    GatewayCapabilities,
    NotificationResponse,
    NotificationStyle,
    PaymentObservation,
    VerifiedNotice,
)
from .schemas import Schema


class MockConfig(Schema):
    api_key: SecretStr = Field(min_length=32, max_length=256, repr=False)
    webhook_secret: SecretStr = Field(min_length=32, max_length=256, repr=False)
    notification_mode: str = Field(default="raw_json", pattern=r"^(raw_json|form)$")
    tolerance_seconds: int = Field(default=300, ge=60, le=900, strict=True)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parameter")
        result[key] = value
    return result


def minor_money(value):
    if type(value) is not int or not 0 <= value <= 999999999999999999:
        raise ValueError("invalid mock monetary units")
    return Decimal(value) / 100


class MockGateway:
    def __init__(self, config, transport, clock):
        self._config, self._transport, self._clock = config, transport, clock
        self.capabilities = GatewayCapabilities(
            currencies={"USD": 2},
            supports_query=True,
            idempotency="native",
            idempotency_seconds=86400,
            supports_close=False,
            checkout_hosts=frozenset({"checkout.mock.nexus.test"}),
        )
        self.notification_style = NotificationStyle(
            methods=frozenset(
                {"POST"} if config.notification_mode == "raw_json" else {"GET", "POST"}
            ),
            mode=config.notification_mode,
        )

    @property
    def _headers(self):
        return {"Authorization": "Bearer " + self._config.api_key.get_secret_value()}

    async def create_payment(self, expectation, idempotency_key):
        payload = expectation.model_dump(mode="json")
        payload["amount_minor"] = validate_amount(
            expectation.amount, expectation.currency, self.capabilities
        )
        payload["idempotency_key"] = idempotency_key
        payload.pop("amount")
        result = await self._transport.request(
            "POST", "/payments/create", headers=self._headers, payload=payload
        )
        try:
            return CheckoutResult.model_validate(result)
        except ValidationError:
            raise GatewayFailure("manual_review") from None

    def _fact(self, values):
        if not isinstance(values, dict):
            raise ValueError("invalid payment fact")
        result = {
            key: value
            for key, value in values.items()
            if key
            not in {"amount_minor", "reversed_minor", "disputed_minor", "fee_minor", "net_minor"}
        }
        for source, target in [
            ("amount_minor", "amount"),
            ("reversed_minor", "reversed_amount"),
            ("disputed_minor", "disputed_amount"),
            ("fee_minor", "fee_amount"),
            ("net_minor", "net_amount"),
        ]:
            if source in values:
                result[target] = minor_money(values[source])
        # The mock wire protocol never accepts decimal-major-unit alternatives.
        if any(
            key in values
            for key in ("amount", "reversed_amount", "disputed_amount", "fee_amount", "net_amount")
        ):
            raise ValueError("invalid amount representation")
        return result

    async def query_payment(self, expectation, provider_payment_ref):
        values = await self._transport.request(
            "POST",
            "/payments/query",
            headers=self._headers,
            payload={
                "merchant_order_ref": expectation.merchant_order_ref,
                "provider_payment_ref": provider_payment_ref,
                "merchant_id": expectation.stable_merchant_id,
                "environment": expectation.environment,
            },
        )
        try:
            values = {**values, "observed_at": self._clock()}
            return PaymentObservation.model_validate(self._fact(values))
        except ValidationError, ValueError, AttributeError:
            raise GatewayFailure("manual_review") from None

    def parse_notification(self, envelope):
        try:
            style = self.notification_style
            if (
                envelope.method not in style.methods
                or len(envelope.body) + len(envelope.query) > style.max_bytes
            ):
                raise ValueError
            signatures = [
                value for name, value in envelope.headers if name.lower() == "x-mock-signature"
            ]
            if len(signatures) != 1:
                raise ValueError
            parts = unique_object(part.split("=", 1) for part in signatures[0].split(","))
            if set(parts) != {"t", "v1"} or not parts["t"].isdigit():
                raise ValueError
            timestamp = int(parts["t"])
            if abs(self._clock().timestamp() - timestamp) > self._config.tolerance_seconds:
                raise ValueError
            if envelope.method == "GET":
                if envelope.body or style.mode != "form":
                    raise ValueError
                raw = envelope.query
            else:
                if envelope.query:
                    raise ValueError
                raw = envelope.body
            expected = hmac.new(
                self._config.webhook_secret.get_secret_value().encode(),
                str(timestamp).encode() + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, parts["v1"]):
                raise ValueError
            if style.mode == "raw_json":
                values = json.loads(raw, object_pairs_hook=unique_object)
            else:
                pairs = parse_qsl(
                    raw.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=64,
                    errors="strict",
                )
                values = unique_object(pairs)
                for name in ("amount_minor", "reversed_minor", "disputed_minor"):
                    if name in values:
                        if not values[name].isdigit():
                            raise ValueError
                        values[name] = int(values[name])
                if "relevant" in values:
                    if values["relevant"] not in {"true", "false"}:
                        raise ValueError
                    values["relevant"] = values["relevant"] == "true"
            return VerifiedNotice.model_validate(self._fact(values))
        except ValueError, TypeError, KeyError, UnicodeError, ValidationError:
            raise GatewayFailure("invalid_notification") from None

    def check_binding(self, expectation, fact):
        if fact.merchant_order_ref != expectation.merchant_order_ref:
            raise GatewayFailure("manual_review")
        for name in ("order_id", "organization_id", "account_id"):
            value = getattr(fact, name)
            if value is not None and value != getattr(expectation, name):
                raise GatewayFailure("manual_review")

    def build_notification_response(self, disposition):
        if disposition in {"accepted", "ignored"}:
            return NotificationResponse(
                200,
                b"ok" if self.notification_style.mode == "raw_json" else b"success",
                "text/plain; charset=utf-8",
            )
        return NotificationResponse(
            {"rejected": 400, "temporary_failure": 503, "rate_limited": 429}[disposition],
            b"invalid" if disposition == "rejected" else b"retry",
            "text/plain; charset=utf-8",
        )

    async def close_payment(self, expectation, provider_payment_ref):
        raise GatewayFailure("manual_review")


def definition(transport, clock=lambda: datetime.now(UTC)):
    return GatewayDefinition(
        MockConfig, lambda config: MockGateway(config, transport, clock), test_only=True
    )
