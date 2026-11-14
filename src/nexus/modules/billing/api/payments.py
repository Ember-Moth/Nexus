"""Payment HTTP adapters; gateway selection, authentication and money stay in billing."""

from typing import Annotated
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from nexus.http import credentials, cursor_value, error_responses, expected_version, idempotency_key

from ..application.payments.schemas import (
    ChannelCreate,
    ChannelPatch,
    ChannelVersionView,
    ChannelView,
    NotificationEnvelope,
    PaymentCaseView,
    PaymentCreate,
    PaymentMethod,
    PaymentObservationView,
    PaymentOrderView,
    PaymentReceiptView,
)
from ..application.payments.service import PaymentService
from ..application.schemas import Page

CONFLICT = error_responses(409)


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["payments"])
    Service = Annotated[PaymentService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Idempotency = Annotated[str, Depends(idempotency_key)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def tagged(response, row):
        response.headers["ETag"] = f'"{row.version}"'
        return row

    @router.get("/payment-gateway-types")
    async def types(token: Token, service: Service):
        return await service.gateway_types(token)

    @router.get("/payment-channels", response_model=Page[ChannelView])
    async def channels(token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None):
        return await service.channels(token, limit, cursor_value(cursor))

    @router.post("/payment-channels", response_model=ChannelView, status_code=201)
    async def create_channel(
        body: ChannelCreate, token: Token, service: Service, key: Idempotency, response: Response
    ):
        return tagged(response, await service.create_channel(token, body, key))

    @router.get("/payment-channels/{channel_id}", response_model=ChannelView)
    async def channel(channel_id: UUID, token: Token, service: Service, response: Response):
        return tagged(response, await service.channel(token, channel_id))

    @router.patch("/payment-channels/{channel_id}", response_model=ChannelView)
    async def update_channel(
        channel_id: UUID,
        body: ChannelPatch,
        token: Token,
        service: Service,
        key: Idempotency,
        version: Version,
        response: Response,
    ):
        return tagged(response, await service.update_channel(token, channel_id, body, version, key))

    @router.get("/payment-channels/{channel_id}/versions", response_model=Page[ChannelVersionView])
    async def versions(
        channel_id: UUID, token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None
    ):
        return await service.versions(token, channel_id, limit, cursor_value(cursor))

    @router.post(
        "/payment-channels/{channel_id}/versions/{config_id}/revoke",
        response_model=ChannelView,
        responses=CONFLICT,
    )
    async def revoke(
        channel_id: UUID,
        config_id: UUID,
        token: Token,
        service: Service,
        version: Version,
        response: Response,
    ):
        return tagged(response, await service.revoke_version(token, channel_id, config_id, version))

    @router.get(
        "/organizations/{organization_id}/payment-methods", response_model=Page[PaymentMethod]
    )
    async def methods(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.methods(token, organization_id, limit, cursor_value(cursor))

    @router.post(
        "/organizations/{organization_id}/payment-orders",
        response_model=PaymentOrderView,
        status_code=202,
    )
    async def create_order(
        organization_id: UUID,
        body: PaymentCreate,
        token: Token,
        service: Service,
        key: Idempotency,
        response: Response,
    ):
        return tagged(response, await service.create_order(token, organization_id, body, key))

    @router.get(
        "/organizations/{organization_id}/payment-orders", response_model=Page[PaymentOrderView]
    )
    async def orders(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.orders(token, organization_id, limit, cursor_value(cursor))

    @router.get(
        "/organizations/{organization_id}/payment-orders/{order_id}",
        response_model=PaymentOrderView,
    )
    async def order(
        organization_id: UUID, order_id: UUID, token: Token, service: Service, response: Response
    ):
        return tagged(response, await service.order(token, organization_id, order_id))

    @router.post(
        "/organizations/{organization_id}/payment-orders/{order_id}/reconcile",
        response_model=PaymentOrderView,
        status_code=202,
    )
    async def reconcile(
        organization_id: UUID, order_id: UUID, token: Token, service: Service, response: Response
    ):
        return tagged(response, await service.reconcile_order(token, organization_id, order_id))

    @router.get(
        "/organizations/{organization_id}/payment-cases", response_model=Page[PaymentCaseView]
    )
    async def cases(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.cases(token, organization_id, limit, cursor_value(cursor))

    @router.get(
        "/organizations/{organization_id}/payment-orders/{order_id}/observations",
        response_model=Page[PaymentObservationView],
    )
    async def observations(
        organization_id: UUID,
        order_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.evidence(
            token, organization_id, order_id, "payment_observation", limit, cursor_value(cursor)
        )

    @router.get(
        "/organizations/{organization_id}/payment-orders/{order_id}/receipts",
        response_model=Page[PaymentReceiptView],
    )
    async def receipts(
        organization_id: UUID,
        order_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.evidence(
            token, organization_id, order_id, "payment_receipt", limit, cursor_value(cursor)
        )

    return router


def create_webhook_router(get_service):
    router = APIRouter(tags=["payment notifications"])
    Service = Annotated[PaymentService, Depends(get_service)]

    @router.post("/webhooks/payments/{channel_id}/orders/{order_id}", include_in_schema=False)
    @router.api_route(
        "/webhooks/payments/{channel_id}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def notification(channel_id: UUID, request: Request, service: Service):
        size = len(request.scope.get("query_string", b""))
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1048576:
                return Response(b"invalid", status_code=413, headers={"Cache-Control": "no-store"})
            chunks.append(chunk)
        body = b"".join(chunks)
        form = ()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/x-www-form-urlencoded" and body:
            try:
                form = tuple(
                    parse_qsl(
                        body.decode("utf-8"),
                        keep_blank_values=True,
                        strict_parsing=True,
                        max_num_fields=64,
                        errors="strict",
                    )
                )
            except ValueError, UnicodeError:
                return Response(b"invalid", status_code=400, headers={"Cache-Control": "no-store"})
        envelope = NotificationEnvelope(
            request.method,
            request.url.path,
            request.scope.get("query_string", b""),
            tuple(
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in request.scope["headers"]
            ),
            body,
            form,
        )
        result = await service.receive_notification(
            channel_id, envelope, request.client.host if request.client else "unknown"
        )
        return Response(
            result.body,
            status_code=result.status,
            headers={"Content-Type": result.content_type, "Cache-Control": "no-store"},
        )

    return router
