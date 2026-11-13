"""Management adapters. Request admission/attempt/settlement are deliberately not HTTP routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value, expected_version, idempotency_key

from .budgets import BudgetService
from .pricing import PricingService
from .schemas import (
    AccountView,
    AdjustmentCreate,
    AdjustmentView,
    BudgetCreate,
    BudgetPatch,
    BudgetPeriodView,
    BudgetView,
    CostCreate,
    DiscountCreate,
    DiscountView,
    EffectivePricing,
    FundingCreate,
    FundingView,
    LedgerView,
    Page,
    Prices,
    PriceView,
    Publish,
    ReceivingCreate,
    ReceivingPatch,
    ReceivingView,
    RequestView,
    SaleCreate,
)
from .service import BillingService


def create_router(get_billing, get_pricing, get_budgets):
    router = APIRouter(prefix="/api/v1", tags=["billing"])
    Billing = Annotated[BillingService, Depends(get_billing)]
    Pricing = Annotated[PricingService, Depends(get_pricing)]
    Budgets = Annotated[BudgetService, Depends(get_budgets)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Idempotency = Annotated[str, Depends(idempotency_key)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def tagged(response, row):
        if hasattr(row, "version"):
            response.headers["ETag"] = f'"{row.version}"'
        return row

    @router.get("/organizations/{organization_id}/account", response_model=AccountView)
    async def account(organization_id: UUID, token: Token, service: Billing, response: Response):
        return tagged(response, await service.account(token, organization_id))

    @router.get("/receiving-accounts", response_model=Page[ReceivingView])
    async def receiving_accounts(
        token: Token, service: Billing, limit: Limit = 50, cursor: Cursor = None
    ):
        return await service.list(token, "receiving", None, limit, cursor_value(cursor))

    @router.post("/receiving-accounts", response_model=ReceivingView, status_code=201)
    async def create_receiving(
        body: ReceivingCreate, token: Token, service: Billing, key: Idempotency, response: Response
    ):
        return tagged(response, await service.create_receiving(token, body, key))

    @router.patch("/receiving-accounts/{resource_id}", response_model=ReceivingView)
    async def update_receiving(
        resource_id: UUID,
        body: ReceivingPatch,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(response, await service.update_receiving(token, resource_id, body, version))

    def add_org_reads(path, kind, View):
        @router.get("/organizations/{organization_id}/" + path, response_model=Page[View])
        async def list_resources(
            organization_id: UUID,
            token: Token,
            service: Billing,
            limit: Limit = 50,
            cursor: Cursor = None,
        ):
            return await service.list(token, kind, organization_id, limit, cursor_value(cursor))

        @router.get(
            "/organizations/{organization_id}/" + path + "/{resource_id}", response_model=View
        )
        async def get_resource(
            organization_id: UUID,
            resource_id: UUID,
            token: Token,
            service: Billing,
            response: Response,
        ):
            return tagged(response, await service.get(token, kind, organization_id, resource_id))

    for path, kind, View in [
        ("funding-records", "funding", FundingView),
        ("adjustments", "adjustment", AdjustmentView),
        ("ledger", "ledger", LedgerView),
        ("budget-policies", "budget", BudgetView),
        ("requests", "request", RequestView),
    ]:
        add_org_reads(path, kind, View)

    @router.post(
        "/organizations/{organization_id}/funding-records",
        response_model=FundingView,
        status_code=201,
    )
    async def create_funding(
        organization_id: UUID,
        body: FundingCreate,
        token: Token,
        service: Billing,
        key: Idempotency,
        response: Response,
    ):
        return tagged(response, await service.create_funding(token, organization_id, body, key))

    @router.patch(
        "/organizations/{organization_id}/funding-records/{resource_id}", response_model=FundingView
    )
    async def update_funding(
        organization_id: UUID,
        resource_id: UUID,
        body: FundingCreate,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response,
            await service.update_funding(token, organization_id, resource_id, body, version),
        )

    @router.post(
        "/organizations/{organization_id}/funding-records/{resource_id}/post",
        response_model=FundingView,
    )
    async def post_funding(
        organization_id: UUID,
        resource_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response, await service.post_funding(token, organization_id, resource_id, version, key)
        )

    @router.post(
        "/organizations/{organization_id}/funding-records/{resource_id}/cancel",
        response_model=FundingView,
    )
    async def cancel_funding(
        organization_id: UUID,
        resource_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response, await service.cancel_funding(token, organization_id, resource_id, version)
        )

    @router.post(
        "/organizations/{organization_id}/adjustments",
        response_model=AdjustmentView,
        status_code=201,
    )
    async def create_adjustment(
        organization_id: UUID,
        body: AdjustmentCreate,
        token: Token,
        service: Billing,
        key: Idempotency,
        response: Response,
    ):
        return tagged(response, await service.create_adjustment(token, organization_id, body, key))

    @router.post(
        "/organizations/{organization_id}/adjustments/{resource_id}/post",
        response_model=AdjustmentView,
    )
    async def post_adjustment(
        organization_id: UUID,
        resource_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response,
            await service.post_adjustment(token, organization_id, resource_id, version, key),
        )

    def add_prices(kind, path, Body):
        @router.get("/" + path, response_model=Page[PriceView])
        async def prices(
            token: Token,
            service: Pricing,
            limit: Limit = 50,
            cursor: Cursor = None,
            scope_id: UUID | None = None,
        ):
            return await service.list_prices(token, kind, limit, cursor_value(cursor), scope_id)

        @router.get("/" + path + "/{resource_id}", response_model=PriceView)
        async def price(resource_id: UUID, token: Token, service: Pricing, response: Response):
            return tagged(response, await service.get_price(token, kind, resource_id))

        @router.post("/" + path, response_model=PriceView, status_code=201)
        async def create_price(
            body: Body, token: Token, service: Pricing, key: Idempotency, response: Response
        ):
            return tagged(response, await service.create_price(token, kind, body, key))

        @router.patch("/" + path + "/{resource_id}", response_model=PriceView)
        async def edit_price(
            resource_id: UUID,
            body: Prices,
            token: Token,
            service: Pricing,
            version: Version,
            response: Response,
        ):
            return tagged(
                response, await service.edit_price(token, kind, resource_id, body, version)
            )

        @router.post("/" + path + "/{resource_id}/publish", response_model=PriceView)
        async def publish_price(
            resource_id: UUID,
            body: Publish,
            token: Token,
            service: Pricing,
            version: Version,
            key: Idempotency,
            response: Response,
        ):
            return tagged(
                response, await service.publish_price(token, kind, resource_id, body, version, key)
            )

    add_prices("sale", "sale-price-versions", SaleCreate)
    add_prices("cost", "cost-price-versions", CostCreate)

    @router.get(
        "/organizations/{organization_id}/discount-versions", response_model=Page[DiscountView]
    )
    async def discounts(
        organization_id: UUID,
        token: Token,
        service: Pricing,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_discounts(token, organization_id, limit, cursor_value(cursor))

    @router.post(
        "/organizations/{organization_id}/discount-versions",
        response_model=DiscountView,
        status_code=201,
    )
    async def create_discount(
        organization_id: UUID,
        body: DiscountCreate,
        token: Token,
        service: Pricing,
        key: Idempotency,
        response: Response,
    ):
        return tagged(response, await service.create_discount(token, organization_id, body, key))

    @router.patch(
        "/organizations/{organization_id}/discount-versions/{resource_id}",
        response_model=DiscountView,
    )
    async def edit_discount(
        organization_id: UUID,
        resource_id: UUID,
        body: DiscountCreate,
        token: Token,
        service: Pricing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response,
            await service.edit_discount(token, organization_id, resource_id, body, version),
        )

    @router.post(
        "/organizations/{organization_id}/discount-versions/{resource_id}/publish",
        response_model=DiscountView,
    )
    async def publish_discount(
        organization_id: UUID,
        resource_id: UUID,
        body: Publish,
        token: Token,
        service: Pricing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response,
            await service.publish_discount(token, organization_id, resource_id, body, version, key),
        )

    @router.get("/organizations/{organization_id}/pricing", response_model=EffectivePricing)
    async def pricing(
        organization_id: UUID,
        model_id: UUID,
        token: Token,
        service: Pricing,
        project_id: UUID | None = None,
        key_id: UUID | None = None,
    ):
        return await service.effective_pricing(token, organization_id, model_id, project_id, key_id)

    @router.post(
        "/organizations/{organization_id}/budget-policies",
        response_model=BudgetView,
        status_code=201,
    )
    async def create_budget(
        organization_id: UUID,
        body: BudgetCreate,
        token: Token,
        service: Budgets,
        response: Response,
    ):
        return tagged(response, await service.create(token, organization_id, body))

    @router.patch(
        "/organizations/{organization_id}/budget-policies/{resource_id}", response_model=BudgetView
    )
    async def update_budget(
        organization_id: UUID,
        resource_id: UUID,
        body: BudgetPatch,
        token: Token,
        service: Budgets,
        version: Version,
        response: Response,
    ):
        return tagged(
            response, await service.update(token, organization_id, resource_id, body, version)
        )

    @router.get(
        "/organizations/{organization_id}/budget-policies/{resource_id}/periods",
        response_model=Page[BudgetPeriodView],
    )
    async def budget_periods(
        organization_id: UUID,
        resource_id: UUID,
        token: Token,
        service: Budgets,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.periods(
            token, organization_id, resource_id, limit, cursor_value(cursor)
        )

    @router.post("/organizations/{organization_id}/account/reconcile")
    async def reconcile(organization_id: UUID, token: Token, service: Billing):
        return await service.reconcile(token, organization_id)

    return router
