"""Billing's public services; all persistence remains inside this module."""

from nexus.infrastructure.audit_reader import audit_reader
from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.storage import ModuleName

from .application.accounts import BillingService
from .application.budgets import BudgetService
from .application.operations import Operations
from .application.payments.processor import PaymentProcessor
from .application.payments.registry import GatewayDefinition, GatewayRegistry
from .application.payments.service import PaymentService
from .application.ports import DeploymentInfo, ModelInfo, Scope
from .application.pricing import PricingService
from .application.reservations import ReservationService
from .application.schemas import Admission, Settlement
from .application.settings import BillingSettings
from .application.usage import UsageProcessor
from .domain.events import (
    ACCOUNT_READY,
    ACCOUNT_READY_VERSION,
    FUNDING_POSTED,
    FUNDING_POSTED_VERSION,
)
from .persistence.operations import OperationsRepository
from .persistence.payments import PaymentRepository
from .persistence.reports import BillingReports
from .persistence.repository import transaction
from .persistence.snapshots import create_price_source
from .providers.antom import AntomConfig, AntomGateway
from .providers.epay import EPayConfig, EPayGateway
from .providers.mock import definition as mock_definition
from .providers.stripe import StripeConfig, StripeGateway

__all__ = [
    "create_reports",
    "create_operations",
    "operations_api",
    "create_audit_reader",
    "create_price_source",
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
    "create_usage_processor",
    "create_services",
    "create_settings",
    "BillingSettings",
    "api",
]


def __getattr__(name):
    """Load the public HTTP adapters only when the API process requests them."""
    if name == "operations_api":
        from .api import operations

        return operations
    if name == "payment_api":
        from .api import payments

        return payments
    raise AttributeError(name)


def create_settings(database: ModuleDatabase, access):
    database.require_module(ModuleName.BILLING)
    return BillingSettings(lambda: transaction(database), access)


def create_services(database: ModuleDatabase, access, catalog):
    database.require_module(ModuleName.BILLING)

    def transactions():
        return transaction(database, PaymentRepository)

    return (
        BillingService(transactions, access),
        PricingService(transactions, access, catalog),
        BudgetService(transactions, access),
        ReservationService(transactions, catalog),
    )


def create_payment_services(
    database,
    access,
    cipher,
    transport,
    limits,
    *,
    policy,
    environment,
    provider_transport,
    public_base_url,
    return_url,
):
    database.require_module(ModuleName.BILLING)
    registry = GatewayRegistry(production=environment == "production")
    registry.register(
        "stripe",
        GatewayDefinition(StripeConfig, lambda cfg: StripeGateway(cfg, provider_transport)),
    )
    registry.register(
        "epay", GatewayDefinition(EPayConfig, lambda cfg: EPayGateway(cfg, provider_transport))
    )
    registry.register(
        "antom", GatewayDefinition(AntomConfig, lambda cfg: AntomGateway(cfg, provider_transport))
    )
    if transport is not None and environment != "production":
        registry.register("mock", mock_definition(transport))

    def transactions():
        return transaction(database, PaymentRepository)

    service = PaymentService(
        transactions,
        access,
        cipher,
        registry,
        limits,
        policy=policy,
        public_base_url=public_base_url,
        return_url=return_url,
    )
    processor = PaymentProcessor(transactions, cipher, registry)
    return service, processor


def create_usage_processor(database, reservations):
    database.require_module(ModuleName.BILLING)
    return UsageProcessor(lambda: transaction(database), reservations)


def create_audit_reader(database):
    return audit_reader(database, ModuleName.BILLING)


def create_reports(database):
    database.require_module(ModuleName.BILLING)
    return BillingReports(database)


def create_operations(database, access, reservations):
    database.require_module(ModuleName.BILLING)
    return Operations(
        lambda **options: transaction(database, OperationsRepository, **options),
        access,
        reservations,
    )
