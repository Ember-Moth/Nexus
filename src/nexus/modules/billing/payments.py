"""Payment management and authenticated notification inbox; remote work is durable."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from nexus.shared.errors import ServiceError

from .common import digest, found, replay, versioned, view
from .gateways import GatewayFailure, public_values, storage_values, validate_amount
from .payment_schemas import (
    ChannelVersionView,
    ChannelView,
    NotificationResponse,
    PaymentCaseView,
    PaymentMethod,
    PaymentObservationView,
    PaymentOrderView,
    PaymentReceiptView,
)
from .payment_support import expectation, open_gateway, order_view
from .schemas import Page


class PaymentService:
    def __init__(
        self,
        transactions,
        access,
        cipher,
        registry,
        limits,
        *,
        currency,
        public_base_url,
        return_url,
    ):
        self._transactions, self._access, self._cipher, self._registry, self._limits = (
            transactions,
            access,
            cipher,
            registry,
            limits,
        )
        self._currency, self._public_base_url, self._return_url = (
            currency,
            public_base_url,
            return_url,
        )

    async def gateway_types(self, token):
        await self._access.authorize(token, operation="payment_admin")
        return {"items": self._registry.descriptions()}

    async def channels(self, token, limit, cursor):
        await self._access.authorize(token, operation="payment_admin")
        async with self._transactions() as repo:
            rows, cursor = await repo.page("payment_channel", None, limit, cursor)
            return Page[ChannelView](
                items=[view(ChannelView, row) for row in rows], next_cursor=cursor
            )

    async def channel(self, token, channel_id):
        await self._access.authorize(token, operation="payment_admin")
        async with self._transactions() as repo:
            return view(ChannelView, found(await repo.get("payment_channel", channel_id)))

    async def versions(self, token, channel_id, limit, cursor):
        await self._access.authorize(token, operation="payment_admin")
        async with self._transactions() as repo:
            found(await repo.get("payment_channel", channel_id))
            rows, cursor = await repo.page(
                "payment_version", None, limit, cursor, channel_id=channel_id
            )
            return Page[ChannelVersionView](
                items=[view(ChannelVersionView, row) for row in rows], next_cursor=cursor
            )

    def _validate_channel(self, body, gateway):
        if body.currency != self._currency:
            raise ServiceError("currency_mismatch", "Channel must use the billing currency", 422)
        self._registry.validate_environment(body.adapter_type, body.environment)
        for amount in (body.min_amount, body.max_amount):
            validate_amount(amount, body.currency, gateway.capabilities)
            self._registry.quote(gateway, amount, body.currency)

    async def create_channel(self, token, body, key):
        actor = await self._access.authorize(token, operation="payment_admin")
        config, gateway = self._registry.normalize(body.adapter_type, body.configuration)
        self._validate_channel(body, gateway)
        self._registry.validate_channel(gateway, body.stable_merchant_id, body.environment)
        values = body.model_dump(mode="json")
        values["configuration"] = storage_values(config)
        async with self._transactions() as repo:
            old, kd, rd = await replay(repo, actor, "payment-channel:create", key, values)
            if old:
                return view(ChannelView, found(await repo.get("payment_channel", old.resource_id)))
            channel_id, config_id = uuid4(), uuid4()
            namespace = (
                "gateway:"
                + body.adapter_type
                + ":"
                + digest(
                    self._registry.receipt_scope(gateway, body.stable_merchant_id, body.environment)
                )
            )
            row = await repo.create(
                "payment_channel",
                {
                    "id": channel_id,
                    **body.model_dump(exclude={"configuration"}),
                    "receipt_namespace": namespace,
                    "active_config_id": config_id,
                },
            )
            key_version, ciphertext = self._cipher.encrypt(
                json.dumps(storage_values(config), sort_keys=True, separators=(",", ":")),
                channel_id,
                config_id,
            )
            await repo.create(
                "payment_version",
                {
                    "id": config_id,
                    "channel_id": channel_id,
                    "version": 1,
                    "key_version": key_version,
                    "ciphertext": ciphertext,
                    "public_configuration": public_values(config),
                    "capabilities": gateway.capabilities.model_dump(mode="json"),
                },
            )
            await repo.remember(actor, "payment-channel:create", kd, rd, "payment_channel", row.id)
            await repo.audit(
                actor,
                "payment_channel.created",
                row.id,
                details={"adapter_type": row.adapter_type, "config_version_id": str(config_id)},
            )
            return view(ChannelView, row)

    async def update_channel(self, token, channel_id, body, version, key):
        actor = await self._access.authorize(token, operation="payment_admin")
        async with self._transactions() as repo:
            action = "payment-channel:update:" + str(channel_id)
            old, kd, rd = await replay(
                repo,
                actor,
                action,
                key,
                {"version": version, **body.model_dump(mode="json", exclude_unset=True)},
            )
            if old:
                return view(ChannelView, found(await repo.get("payment_channel", old.resource_id)))
            row = found(await repo.get("payment_channel", channel_id, lock=True))
            versioned(row, version)
            current = found(await repo.get("payment_version", row.active_config_id))
            changes = body.model_dump(
                exclude_unset=True, exclude={"configuration", "verification_grace_seconds"}
            )
            minimum = changes.get("min_amount", row.min_amount)
            maximum = changes.get("max_amount", row.max_amount)
            if minimum > maximum:
                raise ServiceError(
                    "invalid_payment_bounds", "Minimum amount exceeds maximum amount", 422
                )
            if body.configuration is not None:
                config, gateway = self._registry.normalize(row.adapter_type, body.configuration)
                self._registry.validate_channel(gateway, row.stable_merchant_id, row.environment)
                if hasattr(gateway, "validate_rotation"):
                    gateway.validate_rotation(current.public_configuration)
                _, verification = await repo.verification_versions(channel_id)
                old_count = sum(item.id != current.id for item in verification)
                if (
                    old_count
                    + 1
                    + int(body.verification_grace_seconds > 0 and current.revoked_at is None)
                    > 8
                ):
                    raise ServiceError(
                        "verification_window_full",
                        "At most eight verification versions may overlap",
                        409,
                    )
                config_id = uuid4()
                key_version, ciphertext = self._cipher.encrypt(
                    json.dumps(storage_values(config), sort_keys=True, separators=(",", ":")),
                    channel_id,
                    config_id,
                )
                await repo.create(
                    "payment_version",
                    {
                        "id": config_id,
                        "channel_id": channel_id,
                        "version": row.version + 1,
                        "key_version": key_version,
                        "ciphertext": ciphertext,
                        "public_configuration": public_values(config),
                        "capabilities": gateway.capabilities.model_dump(mode="json"),
                    },
                )
                current.verification_valid_until = await repo.now() + timedelta(
                    seconds=body.verification_grace_seconds
                )
                changes["active_config_id"] = config_id
                capabilities = gateway.capabilities
            else:
                from .payment_schemas import GatewayCapabilities

                capabilities = GatewayCapabilities.model_validate(current.capabilities)
            validate_amount(minimum, row.currency, capabilities)
            validate_amount(maximum, row.currency, capabilities)
            await repo.edit(row, changes)
            await repo.remember(actor, action, kd, rd, "payment_channel", row.id)
            await repo.audit(
                actor,
                "payment_channel.updated",
                row.id,
                details={"config_version_id": str(row.active_config_id), "status": row.status},
            )
            return view(ChannelView, row)

    async def revoke_version(self, token, channel_id, config_id, version):
        actor = await self._access.authorize(token, operation="payment_admin")
        async with self._transactions() as repo:
            channel = found(await repo.get("payment_channel", channel_id, lock=True))
            versioned(channel, version)
            config = found(await repo.get("payment_version", config_id))
            if config.channel_id != channel.id:
                found(None)
            if config.id == channel.active_config_id and channel.status != "disabled":
                raise ServiceError(
                    "disable_channel_first",
                    "Disable the channel before revoking its active credentials",
                    409,
                )
            if config.revoked_at is None:
                config.revoked_at = await repo.now()
                await repo.edit(channel, {})
                await repo.audit(
                    actor,
                    "payment_credentials.revoked",
                    config_id,
                    details={"channel_id": str(channel.id)},
                )
            return view(ChannelView, channel)

    async def methods(self, token, org_id, limit=50, cursor=None):
        await self._access.authorize(token, org_id, operation="payment")
        async with self._transactions() as repo:
            await repo.account(org_id, lock=False)
            rows, cursor = await repo.page("payment_channel", None, limit, cursor, status="active")
            result = []
            for row in rows:
                config = found(await repo.get("payment_version", row.active_config_id))
                try:
                    gateway = open_gateway(self._registry, self._cipher, row, config)
                except ServiceError:
                    continue
                result.append(
                    PaymentMethod(
                        channel_id=row.id,
                        name=row.name,
                        currency=row.currency,
                        min_amount=row.min_amount,
                        max_amount=row.max_amount,
                        decimal_places=gateway.capabilities.currencies[row.currency],
                        environment=row.environment,
                    )
                )
            return Page[PaymentMethod](items=result, next_cursor=cursor)

    async def create_order(self, token, org_id, body, key):
        scope = await self._access.authorize(token, org_id, operation="payment")
        if not self._public_base_url or not self._return_url:
            raise ServiceError(
                "payment_endpoints_unconfigured", "Server payment endpoints must be configured", 503
            )
        async with self._transactions() as repo:
            action = f"{org_id}:payment-order:create"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, body.model_dump(mode="json")
            )
            if old:
                return order_view(
                    found(await repo.get("payment_order", old.resource_id, org_id)),
                    await repo.now(),
                )
            account = await repo.account(org_id)
            channel = found(await repo.get("payment_channel", body.channel_id, lock=True))
            config = found(await repo.get("payment_version", channel.active_config_id))
            gateway = open_gateway(self._registry, self._cipher, channel, config)
            if channel.status != "active":
                raise ServiceError("payment_channel_disabled", "Payment channel is disabled", 409)
            if body.currency != account.currency or body.currency != channel.currency:
                raise ServiceError(
                    "currency_mismatch", "Payment currency must match the account", 422
                )
            validate_amount(body.amount, body.currency, gateway.capabilities)
            if not channel.min_amount <= body.amount <= channel.max_amount:
                raise ServiceError(
                    "payment_amount_outside_limits", "Payment amount is outside channel limits", 422
                )
            collection_amount, collection_currency, rate = self._registry.quote(
                gateway, body.amount, body.currency
            )
            order_id = uuid4()
            callback_url = self._public_base_url + "/webhooks/payments/" + str(channel.id)
            if channel.adapter_type == "antom":
                callback_url += f"/orders/{order_id}"
            row = await repo.create(
                "payment_order",
                {
                    "id": order_id,
                    "organization_id": org_id,
                    "account_id": account.id,
                    "channel_id": channel.id,
                    "config_version_id": config.id,
                    "merchant_order_ref": "nxp_" + order_id.hex,
                    "stable_merchant_id": channel.stable_merchant_id,
                    "environment": channel.environment,
                    "receipt_namespace": channel.receipt_namespace,
                    "amount": body.amount,
                    "credit_amount": body.amount,
                    "collection_amount": collection_amount,
                    "collection_currency": collection_currency,
                    "exchange_rate": rate,
                    "currency": body.currency,
                    "created_by": scope.actor_id,
                    "callback_url": callback_url,
                    "return_url": self._return_url,
                    "expires_at": await repo.now() + timedelta(minutes=30),
                },
            )
            await repo.add_operations(row)
            await repo.remember(scope.actor_id, action, kd, rd, "payment_order", row.id)
            await repo.audit(
                scope.actor_id,
                "payment_order.created",
                row.id,
                org_id,
                {
                    "amount": str(row.amount),
                    "currency": row.currency,
                    "channel_id": str(channel.id),
                },
            )
            return order_view(row, await repo.now())

    async def orders(self, token, org_id, limit, cursor):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            rows, cursor = await repo.page("payment_order", org_id, limit, cursor)
            now = await repo.now()
            return Page[PaymentOrderView](
                items=[order_view(row, now) for row in rows], next_cursor=cursor
            )

    async def order(self, token, org_id, order_id):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            return order_view(
                found(await repo.get("payment_order", order_id, org_id)), await repo.now()
            )

    async def reconcile_order(self, token, org_id, order_id):
        scope = await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            await repo.account(org_id)
            row = found(await repo.get("payment_order", order_id, org_id, lock=True))
            await repo.wake_query(row, actor_id=scope.actor_id)
            await repo.audit(scope.actor_id, "payment_order.reconcile_requested", row.id, org_id)
            return order_view(row, await repo.now())

    async def cases(self, token, org_id, limit, cursor):
        await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            rows, cursor = await repo.page("payment_case", org_id, limit, cursor)
            return Page[PaymentCaseView](
                items=[view(PaymentCaseView, row) for row in rows], next_cursor=cursor
            )

    async def evidence(self, token, org_id, order_id, kind, limit, cursor):
        await self._access.authorize(token, org_id, operation="finance")
        async with self._transactions() as repo:
            found(await repo.get("payment_order", order_id, org_id))
            rows, cursor = await repo.page(kind, None, limit, cursor, order_id=order_id)
            if kind == "payment_observation":
                items = [
                    view(
                        PaymentObservationView,
                        row,
                        facts={key: value for key, value in row.facts.items() if key != "checkout"},
                    )
                    for row in rows
                ]
                return Page[PaymentObservationView](items=items, next_cursor=cursor)
            return Page[PaymentReceiptView](
                items=[view(PaymentReceiptView, row) for row in rows], next_cursor=cursor
            )

    async def receive_notification(self, channel_id, envelope, peer):
        gateway = None
        try:
            allowed = await self._limits.allow("payment:" + str(channel_id), "payment:" + peer)
            async with self._transactions() as repo:
                channel, versions = await repo.verification_versions(channel_id)
            if not versions:
                return NotificationResponse(503, b"retry", "text/plain")
            verified = None
            selected = None
            for config in versions:
                candidate = open_gateway(self._registry, self._cipher, channel, config)
                gateway = gateway or candidate
                if not allowed:
                    return gateway.build_notification_response("rate_limited")
                try:
                    verified = candidate.parse_notification(envelope)
                    gateway, selected = candidate, config
                    break
                except GatewayFailure:
                    continue
            if verified is None:
                return gateway.build_notification_response("rejected")
            if (
                verified.merchant_id != channel.stable_merchant_id
                or verified.environment != channel.environment
            ):
                return gateway.build_notification_response("rejected")
            fields = verified.model_dump(mode="json")
            fingerprint = digest(fields)
            event_key = "id:" + verified.event_id if verified.event_id else "digest:" + fingerprint
            async with self._transactions() as repo:
                # Serialize by immutable business event key before taking account/order locks.
                await repo.lock("payment-event:" + digest([channel.receipt_namespace, event_key]))
                if not await repo.configuration_usable(channel, selected.id, verification=True):
                    return gateway.build_notification_response("rejected")
                old, conflict = await repo.notice(channel.receipt_namespace, event_key, fingerprint)
                if old:
                    await repo.count_duplicate()
                    return gateway.build_notification_response(
                        "ignored" if old.processing_state == "ignored" else "accepted"
                    )
                order = (
                    await repo.order_by_reference(
                        channel.receipt_namespace, verified.merchant_order_ref
                    )
                    if verified.merchant_order_ref
                    else None
                )
                if order is None and verified.relevant and not verified.merchant_order_ref:
                    order = await repo.order_by_provider(
                        channel.receipt_namespace,
                        verified.provider_payment_ref,
                        verified.transaction_id,
                    )
                    if order is None and (verified.provider_payment_ref or verified.transaction_id):
                        return gateway.build_notification_response("temporary_failure")
                ignored = order is None or not verified.relevant
                if order is not None:
                    account, order = await repo.payment_lock(order.id)
                    try:
                        gateway.check_binding(expectation(order), verified)
                    except GatewayFailure:
                        conflict = True
                    if (
                        verified.amount is not None
                        and verified.amount != order.collection_amount
                        or verified.currency is not None
                        and verified.currency != order.collection_currency
                    ):
                        conflict = True
                operation = None
                if not ignored:
                    order.verified_observation_id = None
                    order.version += 1
                    operation = await repo.wake_query(
                        order, target_ref=verified.provider_payment_ref
                    )
                event = await repo.create(
                    "payment_event",
                    {
                        "channel_id": channel.id,
                        "verification_version_id": selected.id,
                        "receipt_namespace": channel.receipt_namespace,
                        "order_id": order.id if order else None,
                        "operation_id": operation.id if operation else None,
                        "wake_version": operation.wake_version if operation else 0,
                        "event_key": event_key,
                        "event_fingerprint": fingerprint,
                        "body_digest": hashlib.sha256(envelope.query + envelope.body).hexdigest(),
                        "verified_fields": fields,
                        "processing_state": "ignored"
                        if ignored
                        else "conflict"
                        if conflict
                        else "received",
                    },
                )
                if not ignored:
                    if conflict:
                        await repo.open_case(order, "notice_conflict", {"event_id": str(event.id)})
                        order.block_credit = True
                    reported_risk = (
                        verified.reversal_state == "succeeded" and verified.reversed_amount > 0
                    ) or verified.dispute_state in {"open", "lost"}
                    if reported_risk and not await repo.verified_risk_decision(order, verified):
                        order.block_credit = True
                        await repo.open_case(
                            order, "reported_reversal_or_dispute", {"event_id": str(event.id)}
                        )
                        if order.credit_state == "credited":
                            await repo.restrict(account, "payment", order.id)
                    await repo.audit(
                        None,
                        "payment_notice.received",
                        event.id,
                        order.organization_id,
                        {
                            "order_id": str(order.id),
                            "verification_version_id": str(selected.id),
                            "conflict": conflict,
                        },
                    )
            return gateway.build_notification_response("ignored" if ignored else "accepted")
        except ServiceError as error:
            if error.code == "not_found":
                return NotificationResponse(404, b"not found", "text/plain")
            return (
                gateway.build_notification_response("temporary_failure")
                if gateway
                else NotificationResponse(503, b"retry", "text/plain")
            )
        except Exception:
            # Unexpected parser/storage/limiter errors never acknowledge a financial event.
            return (
                gateway.build_notification_response("temporary_failure")
                if gateway
                else NotificationResponse(503, b"retry", "text/plain")
            )
