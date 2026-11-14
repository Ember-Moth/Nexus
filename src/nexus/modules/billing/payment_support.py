import json

from nexus.shared.errors import ServiceError

from .common import view
from .payment_schemas import CheckoutData, PaymentExpectation, PaymentOrderView


def open_gateway(registry, cipher, channel, version):
    registry.validate_environment(channel.adapter_type, channel.environment)
    if version.revoked_at is not None:
        raise ServiceError(
            "payment_credentials_revoked", "Payment credentials have been revoked", 503
        )
    clear = cipher.decrypt(version.ciphertext, channel.id, version.id, version.key_version)
    try:
        values = json.loads(clear)
    except ValueError:
        raise ServiceError(
            "payment_configuration_unavailable", "Payment configuration is unavailable", 503
        ) from None
    return registry.open(channel.adapter_type, values)


def expectation(order):
    return PaymentExpectation(
        **{
            name: getattr(order, "id" if name == "order_id" else name)
            for name in PaymentExpectation.model_fields
        }
    )


def order_view(order, now):
    checkout = CheckoutData.model_validate(order.checkout_result) if order.checkout_result else None
    if checkout and (
        checkout.expires_at <= now
        or order.expires_at <= now
        or order.checkout_state != "ready"
        or order.credit_state == "credited"
        or order.review_required
        or order.payment_state not in {"pending", "processing", "unknown"}
    ):
        checkout = None
    return view(PaymentOrderView, order, checkout=checkout)
