"""Billing's public services; all persistence remains inside this module."""

from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .budgets import BudgetService
from .events import ACCOUNT_READY, ACCOUNT_READY_VERSION, FUNDING_POSTED, FUNDING_POSTED_VERSION
from .gateways import GatewayRegistry
from .mock_gateway import definition as mock_definition
from .payment_processing import PaymentProcessor
from .payment_repository import PaymentRepository
from .payments import PaymentService
from .ports import DeploymentInfo, ModelInfo, Scope
from .pricing import PricingService
from .repository import transaction
from .reservations import ReservationService
from .schemas import Admission, Settlement
from .service import BillingService

__all__ = [
    "ACCOUNT_READY",
    "ACCOUNT_READY_VERSION",
    "FUNDING_POSTED",
    "FUNDING_POSTED_VERSION",
    "PaymentService",
    "PaymentProcessor",
    "create_payment_services",
    "payment_api",
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
        return transaction(database, currency, PaymentRepository)

    return (
        BillingService(transactions, access, currency),
        PricingService(transactions, access, catalog, currency),
        BudgetService(transactions, access, currency),
        ReservationService(transactions, catalog),
    )


def create_payment_services(
    database,
    access,
    cipher,
    transport,
    limits,
    *,
    currency,
    environment,
    public_base_url,
    return_url,
    requests_per_minute=120,
):
    database.require_module(ModuleName.BILLING)
    registry = GatewayRegistry(production=environment == "production")
    if transport is not None and environment != "production":
        registry.register("mock", mock_definition(transport))

    def transactions():
        return transaction(database, currency, PaymentRepository)

    service = PaymentService(
        transactions,
        access,
        cipher,
        registry,
        limits,
        currency=currency,
        public_base_url=public_base_url,
        return_url=return_url,
    )
    processor = PaymentProcessor(
        transactions, cipher, registry, requests_per_minute=requests_per_minute
    )
    return service, processor
