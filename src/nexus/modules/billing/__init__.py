"""Billing's public services; all persistence remains inside this module."""

from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .budgets import BudgetService
from .events import ACCOUNT_READY, ACCOUNT_READY_VERSION
from .ports import DeploymentInfo, ModelInfo, Scope
from .pricing import PricingService
from .repository import transaction
from .reservations import ReservationService
from .schemas import Admission, Settlement
from .service import BillingService

__all__ = [
    "ACCOUNT_READY",
    "ACCOUNT_READY_VERSION",
    "BillingService",
    "PricingService",
    "BudgetService",
    "ReservationService",
    "Scope",
    "ModelInfo",
    "DeploymentInfo",
    "Admission",
    "Settlement",
    "create_services",
    "api",
]


def create_services(database: ModuleDatabase, access, catalog, currency):
    database.require_module(ModuleName.BILLING)

    def transactions():
        return transaction(database, currency)

    return (
        BillingService(transactions, access, currency),
        PricingService(transactions, access, catalog, currency),
        BudgetService(transactions, access, currency),
        ReservationService(transactions, catalog),
    )
