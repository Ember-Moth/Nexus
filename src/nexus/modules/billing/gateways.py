"""Typed registry: the only selection point for installed payment adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, SecretStr, ValidationError

from nexus.shared.errors import ServiceError

from .payment_schemas import (
    CheckoutResult,
    GatewayCapabilities,
    NotificationDisposition,
    NotificationEnvelope,
    NotificationResponse,
    NotificationStyle,
    PaymentExpectation,
    PaymentObservation,
    VerifiedNotice,
)


class GatewayFailure(Exception):
    def __init__(self, code="retryable_failure"):
        if code not in {
            "invalid_notification",
            "pending",
            "unconfirmed",
            "retryable_failure",
            "manual_review",
        }:
            raise ValueError("unsupported gateway error")
        self.code = code
        super().__init__(code)


class PaymentTransport(Protocol):
    async def request(
        self, method: str, path: str, *, headers: dict[str, str], payload: dict | None = None
    ) -> dict: ...


class PaymentGateway(Protocol):
    capabilities: GatewayCapabilities
    notification_style: NotificationStyle

    async def create_payment(
        self, expectation: PaymentExpectation, idempotency_key: str
    ) -> CheckoutResult: ...
    async def query_payment(
        self, expectation: PaymentExpectation, provider_payment_ref: str | None
    ) -> PaymentObservation: ...
    def parse_notification(self, envelope: NotificationEnvelope) -> VerifiedNotice: ...
    def check_binding(self, expectation: PaymentExpectation, fact: VerifiedNotice) -> None: ...
    def build_notification_response(
        self, disposition: NotificationDisposition
    ) -> NotificationResponse: ...
    async def close_payment(
        self, expectation: PaymentExpectation, provider_payment_ref: str
    ) -> PaymentObservation: ...


@dataclass(frozen=True)
class GatewayDefinition:
    config_model: type[BaseModel]
    factory: Callable[[BaseModel], PaymentGateway]
    test_only: bool = False


def storage_values(value):
    """Unwrap only for authenticated encryption, never for a response or log."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return {key: storage_values(getattr(value, key)) for key in type(value).model_fields}
    if isinstance(value, dict):
        return {key: storage_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [storage_values(item) for item in value]
    return value


def public_values(config):
    return {
        key: value
        for key, value in config.model_dump(mode="json").items()
        if not isinstance(getattr(config, key), SecretStr)
    }


def validate_amount(amount: Decimal, currency: str, capabilities: GatewayCapabilities):
    places = capabilities.currencies.get(currency)
    if places is None:
        raise ServiceError(
            "unsupported_payment_currency", "Gateway does not support this currency", 422
        )
    with localcontext() as context:
        context.prec = 60
        minor = amount * (Decimal(10) ** places)
        if amount <= 0 or minor != minor.to_integral_value():
            raise ServiceError(
                "payment_precision", "Amount cannot be represented exactly by the gateway", 422
            )
        return int(minor)


def validate_checkout(result, capabilities, now):
    if result is None:
        return None
    if result.expires_at.tzinfo is None or result.expires_at <= now:
        return None
    if result.kind not in capabilities.checkout_kinds:
        raise GatewayFailure("manual_review")
    if result.kind == "qr_code" and not any(
        prefix and result.destination.startswith(prefix) for prefix in capabilities.qr_code_prefixes
    ):
        raise GatewayFailure("manual_review")
    if result.kind == "redirect":
        try:
            url = urlsplit(result.destination)
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or url.fragment
                or url.hostname not in capabilities.checkout_hosts
                or url.port not in {None, 443}
            ):
                raise ValueError
        except ValueError:
            raise GatewayFailure("manual_review") from None
    return result


class GatewayRegistry:
    def __init__(self, *, production=False):
        self._definitions = {}
        self._production = production

    def register(self, name: str, definition: GatewayDefinition):
        if name in self._definitions:
            raise ValueError("duplicate gateway registration")
        self._definitions[name] = definition

    def _definition(self, name):
        definition = self._definitions.get(name)
        if definition is None or (definition.test_only and self._production):
            raise ServiceError(
                "gateway_unavailable", "Payment gateway is not installed for this environment", 422
            )
        return definition

    def normalize(self, name, values):
        definition = self._definition(name)
        try:
            config = definition.config_model.model_validate(values)
        except ValidationError:
            raise ServiceError(
                "invalid_gateway_configuration", "Invalid gateway configuration", 422
            ) from None
        gateway = definition.factory(config)
        if not gateway.capabilities.supports_query:
            raise ServiceError(
                "query_required",
                "Automatic funding requires authenticated server-side queries",
                422,
            )
        return config, gateway

    def validate_environment(self, name, environment):
        definition = self._definition(name)
        if self._production and environment != "live":
            raise ServiceError(
                "gateway_environment_mismatch",
                "Production accounts require live payment channels",
                422,
            )
        if definition.test_only and environment != "test":
            raise ServiceError(
                "gateway_environment_mismatch", "Test gateways require the test environment", 422
            )

    def open(self, name, values):
        return self.normalize(name, values)[1]

    def descriptions(self):
        return [
            {
                "adapter_type": name,
                "configuration_schema": definition.config_model.model_json_schema(),
                "test_only": definition.test_only,
            }
            for name, definition in sorted(self._definitions.items())
            if not (definition.test_only and self._production)
        ]
