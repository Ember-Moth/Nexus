"""The platform billing policy."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from nexus.http import business_errors, credentials, expected_version

from ..application.schemas import BillingPolicy, BillingPolicyView
from ..application.settings import BillingSettings


def create_settings_router(get_settings):
    router = APIRouter(prefix="/api/v1", tags=["billing"])
    Settings = Annotated[BillingSettings, Depends(get_settings)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]

    @router.get("/settings/billing", response_model=BillingPolicyView)
    async def billing_policy(service: Settings, token: Token, response: Response):
        """Billing currency and limits; platform administrators only."""
        result = await service.get(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/billing",
        response_model=BillingPolicyView,
        responses=business_errors(
            "version_conflict", "policy_managed_externally", "currency_locked"
        ),
    )
    async def update_billing_policy(
        body: BillingPolicy, service: Settings, token: Token, version: Version, response: Response
    ):
        """Replace the billing policy. API and worker processes apply it within seconds."""
        result = await service.update(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    return router
