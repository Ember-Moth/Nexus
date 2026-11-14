import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.infrastructure.credential_cipher import CredentialCipher
from nexus.modules.billing.gateways import (
    GatewayDefinition,
    GatewayFailure,
    GatewayRegistry,
    validate_checkout,
)
from nexus.modules.billing.mock_gateway import MockConfig, definition
from nexus.modules.billing.payment_schemas import (
    CheckoutData,
    NotificationEnvelope,
)
from nexus.shared.errors import ServiceError
from scripts.mock_payment_provider import MockPaymentProvider

KEY = "mock-contract-test-credential-0000000001"


def gateway():
    return definition(None).factory(MockConfig(api_key=KEY, webhook_secret=KEY))


def test_registry_rejects_unknown_implementations_and_production_test_channels():
    registry = GatewayRegistry(production=True)
    registry.register("mock", definition(None))
    with pytest.raises(ServiceError):
        registry.open("mock", {"api_key": KEY, "webhook_secret": KEY})
    with pytest.raises(ServiceError):
        registry.open("unknown_module", {})
    # The environment rule also applies to a normal adapter that supports a vendor sandbox.
    registry.register("vendor", GatewayDefinition(MockConfig, definition(None).factory))
    with pytest.raises(ServiceError) as rejected:
        registry.validate_environment("vendor", "test")
    assert rejected.value.code == "gateway_environment_mismatch"
    registry.validate_environment("vendor", "live")


def test_gateway_without_authenticated_query_cannot_be_enabled():
    original = definition(None)

    def factory(config):
        item = original.factory(config)
        item.capabilities = item.capabilities.model_copy(update={"supports_query": False})
        return item

    registry = GatewayRegistry()
    registry.register("unqueryable", GatewayDefinition(MockConfig, factory))
    with pytest.raises(ServiceError) as rejected:
        registry.normalize("unqueryable", {"api_key": KEY, "webhook_secret": KEY})
    assert rejected.value.code == "query_required"


async def test_unsupported_close_never_pretends_to_close_a_payment():
    item = gateway()
    assert item.capabilities.supports_close is False
    with pytest.raises(GatewayFailure) as rejected:
        await item.close_payment(None, "payment-reference")
    assert rejected.value.code == "manual_review"


def test_signature_verification_keeps_raw_json_and_duplicate_headers():
    item = gateway()
    fields = {"merchant_id": "merchant", "environment": "test", "relevant": False}
    raw, headers = MockPaymentProvider.signed_notice(fields, KEY)
    envelope = NotificationEnvelope("POST", "/webhook", b"", tuple(headers.items()), raw)
    assert item.parse_notification(envelope).relevant is False
    altered = NotificationEnvelope("POST", "/webhook", b"", tuple(headers.items()), raw + b" ")
    with pytest.raises(GatewayFailure):
        item.parse_notification(altered)
    duplicate = NotificationEnvelope(
        "POST",
        "/webhook",
        b"",
        tuple(headers.items()) + (("X-Mock-Signature", headers["X-Mock-Signature"]),),
        raw,
    )
    with pytest.raises(GatewayFailure):
        item.parse_notification(duplicate)
    duplicate_json = b'{"merchant_id":"merchant","merchant_id":"second","environment":"test"}'
    import hashlib
    import hmac

    timestamp = int(datetime.now(UTC).timestamp())
    signature = hmac.new(
        KEY.encode(), str(timestamp).encode() + b"." + duplicate_json, hashlib.sha256
    ).hexdigest()
    with pytest.raises(GatewayFailure):
        item.parse_notification(
            NotificationEnvelope(
                "POST",
                "/webhook",
                b"",
                (("X-Mock-Signature", f"t={timestamp},v1={signature}"),),
                duplicate_json,
            )
        )


def test_checkout_destinations_are_constrained_by_the_registered_gateway():
    from datetime import timedelta

    item = gateway()
    expires = datetime.now(UTC) + timedelta(minutes=5)
    for destination in [
        "https://attacker.example/pay",
        "javascript:alert(1)",
        "https://user:password@checkout.mock.nexus.test/pay",
        "https://checkout.mock.nexus.test:8443/pay",
    ]:
        with pytest.raises(GatewayFailure):
            validate_checkout(
                CheckoutData(kind="redirect", destination=destination, expires_at=expires),
                item.capabilities,
                datetime.now(UTC),
            )


def test_payment_configuration_cipher_is_separate_from_catalog_credentials():
    key = base64.urlsafe_b64decode(base64.urlsafe_b64encode(bytes(range(32))))
    payment = CredentialCipher(1, {1: key}, purpose="payment")
    catalog = CredentialCipher(1, {1: key})
    channel_id, version_id = uuid4(), uuid4()
    version, blob = payment.encrypt("test-payment-configuration", channel_id, version_id)
    assert payment.decrypt(blob, channel_id, version_id, version) == "test-payment-configuration"
    with pytest.raises(ServiceError):
        catalog.decrypt(blob, channel_id, version_id, version)
    with pytest.raises(ServiceError):
        payment.decrypt(blob, uuid4(), version_id, version)
    with pytest.raises(ServiceError):
        payment.decrypt(blob, channel_id, uuid4(), version)


def test_runtime_refuses_mock_provider_in_production(settings_factory):
    with pytest.raises(ValidationError, match="mock payment gateway"):
        settings_factory(environment="production", mock_payment_url="http://127.0.0.1:59090")


def test_qr_checkout_requires_an_explicit_adapter_capability():
    from datetime import timedelta

    item = gateway()
    now = datetime.now(UTC)
    code = CheckoutData(
        kind="qr_code",
        destination="mockpay:payment-reference",
        expires_at=now + timedelta(minutes=5),
    )
    with pytest.raises(GatewayFailure):
        validate_checkout(code, item.capabilities, now)
    supported = item.capabilities.model_copy(
        update={"checkout_kinds": frozenset({"qr_code"}), "qr_code_prefixes": ("mockpay:",)}
    )
    assert validate_checkout(code, supported, now) == code
    with pytest.raises(GatewayFailure):
        validate_checkout(
            code.model_copy(update={"destination": "javascript:alert(1)"}), supported, now
        )
