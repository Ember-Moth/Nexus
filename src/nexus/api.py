import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as RoutingError

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings
from nexus.http import credentials, declare_error_responses
from nexus.infrastructure.metrics import PROMETHEUS_CONTENT_TYPE, prometheus_text
from nexus.modules.billing import api as billing_api
from nexus.modules.billing import operations_api, payment_api
from nexus.modules.catalog import CatalogService, routing_api
from nexus.modules.catalog import api as catalog_api
from nexus.modules.identity import IdentityService, ModelAccessService, ProjectService
from nexus.modules.identity import api as identity_api
from nexus.modules.publication import api as publication_api
from nexus.modules.reporting import api as reporting_api
from nexus.shared.error_catalog import ErrorGuidance, error_catalog
from nexus.shared.errors import ServiceError
from nexus.shared.telemetry import event, request_id

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
type Readiness = Callable[[], Awaitable[dict[str, str]]]


def build_server(lifespan: Lifespan, *, development: bool = False) -> FastAPI:
    app = FastAPI(
        title="Nexus Control Plane",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    generate_openapi = app.openapi

    def openapi() -> dict:
        if app.openapi_schema is None:
            app.openapi_schema = declare_error_responses(generate_openapi())
        return app.openapi_schema

    app.openapi = openapi

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        started = monotonic()
        trace = uuid4()
        request.state.trace_id = str(trace)
        context = request_id.set(trace)
        try:
            try:
                response = await call_next(request)
            except Exception as error:
                response = await unexpected_error(request, error)
            if metrics := getattr(app.state, "process_metrics", None):
                metrics.response(request.method, response.status_code, started)
            response.headers["X-Request-ID"] = str(trace)
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            route = getattr(request.scope.get("route"), "path", None)
            method = (
                request.method
                if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            level = logging.ERROR if response.status_code >= 500 else logging.INFO
            event(
                logging.getLogger("nexus.api"),
                level,
                "http.response",
                request_id=trace,
                route=route or "<unmatched>",
                method=method,
                status=response.status_code,
                error_code=getattr(request.state, "error_code", None),
                error_type=getattr(request.state, "error_type", None),
                elapsed_ms=round((monotonic() - started) * 1000, 2),
            )
            return response
        finally:
            request_id.reset(context)

    def error_response(request, status, code, message, **extra):
        request.state.error_code = code
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "trace_id": getattr(request.state, "trace_id", ""),
                    **extra,
                }
            },
        )

    @app.exception_handler(RoutingError)
    async def routing_error(request: Request, error: RoutingError):
        if not request.url.path.startswith("/api/") or error.status_code not in {404, 405}:
            return await http_exception_handler(request, error)
        code, message = (
            ("route_not_found", "Route not found")
            if error.status_code == 404
            else ("method_not_allowed", "Method not allowed")
        )
        response = error_response(request, error.status_code, code, message)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        response = error_response(request, error.status, error.code, str(error))
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # FastAPI's default validation response can echo passwords and tokens in input/ctx.
        fields = [{"location": list(item["loc"]), "code": item["type"]} for item in error.errors()]
        return error_response(
            request, 422, "validation_error", "Invalid request parameters", fields=fields
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        request.state.error_type = type(error).__name__
        return error_response(request, 500, "internal_error", "Internal server error")

    @app.get("/api/v1/error-codes", response_model=dict[str, list[ErrorGuidance]], tags=["errors"])
    async def codes():
        return error_catalog()

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        readiness: Readiness = request.app.state.readiness
        checks = await readiness()
        healthy = bool(checks) and all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "not_ready", "checks": checks},
        )

    return app


def create_app(settings: RuntimeSettings | None = None) -> FastAPI:
    settings = settings or RuntimeSettings(process_role="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        application = Application(settings)
        app.state.operational_metrics = application.metrics
        app.state.process_metrics = application.database.metrics
        app.state.reports = application.reports
        app.state.operations = application.operations
        app.state.readiness = application.readiness
        app.state.publication = application.publication
        app.state.upstream_resources = application.upstream_resources
        app.state.routing = application.routing
        app.state.usage = application.usage
        app.state.billing_service = application.billing
        app.state.payment_service = application.payments
        app.state.payment_processor = application.payment_processor
        app.state.pricing_service = application.pricing
        app.state.budget_service = application.budgets
        app.state.reservations = application.reservations
        app.state.process_billing = application.process_billing
        app.state.identity_service = application.identity
        app.state.project_service = application.projects
        app.state.catalog_service = application.catalog
        app.state.model_access_service = application.model_access
        try:
            yield
        finally:
            await application.close()

    def get_identity(request: Request) -> IdentityService:
        return request.app.state.identity_service

    def get_projects(request: Request) -> ProjectService:
        return request.app.state.project_service

    def get_catalog(request: Request) -> CatalogService:
        return request.app.state.catalog_service

    def get_model_access(request: Request) -> ModelAccessService:
        return request.app.state.model_access_service

    def get_payments(request: Request):
        return request.app.state.payment_service

    def get_billing(request: Request):
        return request.app.state.billing_service

    def get_pricing(request: Request):
        return request.app.state.pricing_service

    def get_budgets(request: Request):
        return request.app.state.budget_service

    def get_reports(request: Request):
        return request.app.state.reports

    def get_operations(request: Request):
        return request.app.state.operations

    def get_publication(request: Request):
        return request.app.state.publication

    def get_upstream_resources(request: Request):
        return request.app.state.upstream_resources

    def get_routing(request: Request):
        return request.app.state.routing

    app = build_server(lifespan, development=settings.environment != "production")

    @app.get("/internal/metrics", include_in_schema=False)
    async def metrics(request: Request, token: Annotated[str, Depends(credentials)]):
        return JSONResponse(
            content=jsonable_encoder(await request.app.state.operational_metrics(token)),
            headers={"Cache-Control": "no-store"},
        )

    # Same data and the same platform-admin bearer check, in Prometheus text format.
    @app.get("/internal/metrics/prometheus", include_in_schema=False)
    async def prometheus_metrics(request: Request, token: Annotated[str, Depends(credentials)]):
        return PlainTextResponse(
            prometheus_text(await request.app.state.operational_metrics(token)),
            media_type=PROMETHEUS_CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(reporting_api.create_router(get_reports))
    app.include_router(operations_api.create_router(get_operations))
    app.include_router(publication_api.create_router(get_publication))
    app.include_router(publication_api.create_resource_router(get_upstream_resources))
    app.include_router(routing_api.create_router(get_routing))
    app.include_router(
        identity_api.create_router(
            get_identity,
            auth_proxy_peers=frozenset(settings.auth_proxy_peers),
            auth_proxy_secret=settings.auth_proxy_secret,
        )
    )
    app.include_router(identity_api.create_project_router(get_projects))
    app.include_router(identity_api.create_policy_router(get_model_access))
    app.include_router(catalog_api.create_router(get_catalog))
    app.include_router(billing_api.create_router(get_billing, get_pricing, get_budgets))
    app.include_router(payment_api.create_router(get_payments))
    app.include_router(payment_api.create_webhook_router(get_payments))
    return app
