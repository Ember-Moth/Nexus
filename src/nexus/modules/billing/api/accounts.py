"""Management adapters. Request admission/attempt/settlement are deliberately not HTTP routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response

from nexus.http import credentials, cursor_value, error_responses, expected_version, idempotency_key

from ..application.accounts import BillingService
from ..application.budgets import BudgetService
from ..application.pricing import PricingService
from ..application.schemas import (
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
    ReconciliationRun,
    SaleCreate,
)

CONFLICT = error_responses(409)


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

    @router.patch(
        "/receiving-accounts/{receiving_account_id}",
        response_model=ReceivingView,
        responses=CONFLICT,
    )
    async def update_receiving(
        receiving_account_id: UUID,
        body: ReceivingPatch,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response, await service.update_receiving(token, receiving_account_id, body, version)
        )

    def add_org_reads(path, kind, View, id_name):
        ResourceId = Annotated[UUID, Path(alias=id_name)]

        @router.get("/organizations/{organization_id}/" + path, response_model=Page[View])
        async def list_resources(
            organization_id: UUID,
            token: Token,
            service: Billing,
            limit: Limit = 50,
            cursor: Cursor = None,
        ):
            return await service.list(token, kind, organization_id, limit, cursor_value(cursor))

        @router.get(f"/organizations/{{organization_id}}/{path}/{{{id_name}}}", response_model=View)
        async def get_resource(
            organization_id: UUID,
            resource_id: ResourceId,
            token: Token,
            service: Billing,
            response: Response,
        ):
            return tagged(response, await service.get(token, kind, organization_id, resource_id))

    for path, kind, View, id_name in [
        ("funding-records", "funding", FundingView, "funding_record_id"),
        ("adjustments", "adjustment", AdjustmentView, "adjustment_id"),
        ("budget-policies", "budget", BudgetView, "budget_policy_id"),
    ]:
        add_org_reads(path, kind, View, id_name)

    @router.get(
        "/organizations/{organization_id}/ledger/{ledger_entry_id}", response_model=LedgerView
    )
    async def ledger_entry(
        organization_id: UUID, ledger_entry_id: UUID, token: Token, service: Billing
    ):
        return await service.get(token, "ledger", organization_id, ledger_entry_id)

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
        "/organizations/{organization_id}/funding-records/{funding_record_id}",
        response_model=FundingView,
        responses=CONFLICT,
    )
    async def update_funding(
        organization_id: UUID,
        funding_record_id: UUID,
        body: FundingCreate,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response,
            await service.update_funding(token, organization_id, funding_record_id, body, version),
        )

    @router.post(
        "/organizations/{organization_id}/funding-records/{funding_record_id}/post",
        response_model=FundingView,
    )
    async def post_funding(
        organization_id: UUID,
        funding_record_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response,
            await service.post_funding(token, organization_id, funding_record_id, version, key),
        )

    @router.post(
        "/organizations/{organization_id}/funding-records/{funding_record_id}/cancel",
        response_model=FundingView,
        responses=CONFLICT,
    )
    async def cancel_funding(
        organization_id: UUID,
        funding_record_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response,
            await service.cancel_funding(token, organization_id, funding_record_id, version),
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
        "/organizations/{organization_id}/adjustments/{adjustment_id}/post",
        response_model=AdjustmentView,
    )
    async def post_adjustment(
        organization_id: UUID,
        adjustment_id: UUID,
        token: Token,
        service: Billing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response,
            await service.post_adjustment(token, organization_id, adjustment_id, version, key),
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

        @router.get("/" + path + "/{price_version_id}", response_model=PriceView)
        async def price(price_version_id: UUID, token: Token, service: Pricing, response: Response):
            return tagged(response, await service.get_price(token, kind, price_version_id))

        @router.post("/" + path, response_model=PriceView, status_code=201)
        async def create_price(
            body: Body, token: Token, service: Pricing, key: Idempotency, response: Response
        ):
            return tagged(response, await service.create_price(token, kind, body, key))

        @router.patch(
            "/" + path + "/{price_version_id}", response_model=PriceView, responses=CONFLICT
        )
        async def edit_price(
            price_version_id: UUID,
            body: Prices,
            token: Token,
            service: Pricing,
            version: Version,
            response: Response,
        ):
            return tagged(
                response, await service.edit_price(token, kind, price_version_id, body, version)
            )

        @router.post("/" + path + "/{price_version_id}/publish", response_model=PriceView)
        async def publish_price(
            price_version_id: UUID,
            body: Publish,
            token: Token,
            service: Pricing,
            version: Version,
            key: Idempotency,
            response: Response,
        ):
            return tagged(
                response,
                await service.publish_price(token, kind, price_version_id, body, version, key),
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
        "/organizations/{organization_id}/discount-versions/{discount_version_id}",
        response_model=DiscountView,
        responses=CONFLICT,
    )
    async def edit_discount(
        organization_id: UUID,
        discount_version_id: UUID,
        body: DiscountCreate,
        token: Token,
        service: Pricing,
        version: Version,
        response: Response,
    ):
        return tagged(
            response,
            await service.edit_discount(token, organization_id, discount_version_id, body, version),
        )

    @router.post(
        "/organizations/{organization_id}/discount-versions/{discount_version_id}/publish",
        response_model=DiscountView,
    )
    async def publish_discount(
        organization_id: UUID,
        discount_version_id: UUID,
        body: Publish,
        token: Token,
        service: Pricing,
        version: Version,
        key: Idempotency,
        response: Response,
    ):
        return tagged(
            response,
            await service.publish_discount(
                token, organization_id, discount_version_id, body, version, key
            ),
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
        responses=CONFLICT,
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
        "/organizations/{organization_id}/budget-policies/{budget_policy_id}",
        response_model=BudgetView,
        responses=CONFLICT,
    )
    async def update_budget(
        organization_id: UUID,
        budget_policy_id: UUID,
        body: BudgetPatch,
        token: Token,
        service: Budgets,
        version: Version,
        response: Response,
    ):
        return tagged(
            response, await service.update(token, organization_id, budget_policy_id, body, version)
        )

    @router.get(
        "/organizations/{organization_id}/budget-policies/{budget_policy_id}/periods",
        response_model=Page[BudgetPeriodView],
    )
    async def budget_periods(
        organization_id: UUID,
        budget_policy_id: UUID,
        token: Token,
        service: Budgets,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.periods(
            token, organization_id, budget_policy_id, limit, cursor_value(cursor)
        )

    @router.post(
        "/organizations/{organization_id}/account/reconcile",
        response_model=ReconciliationRun,
        # Each issue carries only the references that apply to its kind.
        response_model_exclude_unset=True,
    )
    async def reconcile(organization_id: UUID, token: Token, service: Billing):
        return await service.reconcile(token, organization_id)

    return router
