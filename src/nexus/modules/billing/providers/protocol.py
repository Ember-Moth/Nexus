"""Wire decoding and cryptography shared by hosted payment adapters."""

import base64
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nexus.shared.errors import ServiceError

from ..application.payments.registry import GatewayFailure
from ..application.payments.schemas import NotificationResponse, NotificationStyle


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parameter")
        result[key] = value
    return result


def document(raw):
    result = json.loads(raw, object_pairs_hook=unique, parse_float=Decimal)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def one_header(headers, key):
    values = [v for k, v in headers if k.lower() == key.lower()]
    if len(values) != 1:
        raise ValueError("missing or duplicate header")
    return values[0]


def require(condition):
    if not condition:
        raise ValueError("invalid provider fields")


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value))
    return value


def cents(value):
    require(type(value) in {int, str} and re.fullmatch(r"[0-9]{1,18}", str(value)))
    return Decimal(value).scaleb(-2)


def rsa_key(value, private):
    try:
        data = value.encode("ascii")
        if data.startswith(b"-----BEGIN "):
            key = (
                serialization.load_pem_private_key(data, None)
                if private
                else serialization.load_pem_public_key(data)
            )
        else:
            data = base64.b64decode(data, validate=True)
            key = (
                serialization.load_der_private_key(data, None)
                if private
                else serialization.load_der_public_key(data)
            )
        require(
            isinstance(key, rsa.RSAPrivateKey if private else rsa.RSAPublicKey)
            and key.key_size >= 2048
        )
        return key
    except ValueError, TypeError, UnsupportedAlgorithm:
        raise ValueError("invalid RSA key material") from None


def rsa_sign(key, payload):
    return base64.b64encode(key.sign(payload, padding.PKCS1v15(), hashes.SHA256())).decode()


def rsa_verify(key, payload, signature):
    try:
        key.verify(
            base64.b64decode(signature, validate=True), payload, padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature:
        raise ValueError("invalid signature") from None


class HostedGateway:
    notification_style = NotificationStyle(methods=frozenset({"POST"}), mode="raw_json")
    acknowledgement = b"ok"
    content_type = "text/plain"

    def __init__(self, config, transport):
        self.config = config
        self.transport = transport

    def validate_channel(self, merchant, environment):
        if self.merchant != merchant or self.environment != environment:
            raise ServiceError(
                "gateway_binding_mismatch", "Gateway merchant or environment differs", 422
            )

    def check_binding(self, expectation, fact):
        if (
            fact.merchant_order_ref is not None
            and fact.merchant_order_ref != expectation.merchant_order_ref
        ):
            raise GatewayFailure("manual_review")

    def parse_notification(self, envelope):
        try:
            require(
                envelope.method in self.notification_style.methods
                and len(envelope.body) + len(envelope.query) <= self.notification_style.max_bytes
            )
            return self.notice(envelope)
        except ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OverflowError:
            raise GatewayFailure("invalid_notification") from None

    def build_notification_response(self, disposition):
        if disposition in {"accepted", "ignored"}:
            return NotificationResponse(200, self.acknowledgement, self.content_type)
        status = {"rejected": 400, "temporary_failure": 503, "rate_limited": 429}[disposition]
        return NotificationResponse(status, b"fail", "text/plain")

    async def close_payment(self, expectation, provider_payment_ref):
        raise GatewayFailure("manual_review")

    async def create_payment(self, expectation, idempotency_key):
        try:
            return await self.create(expectation, idempotency_key)
        except ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OverflowError:
            raise GatewayFailure("manual_review") from None

    async def query_payment(self, expectation, provider_payment_ref):
        try:
            return await self.query(expectation, provider_payment_ref)
        except ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OverflowError:
            raise GatewayFailure("manual_review") from None

    @staticmethod
    def now():
        return datetime.now(UTC)
