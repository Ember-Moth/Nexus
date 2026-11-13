import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings
from nexus.modules.identity import IdentityService, ProjectService
from nexus.modules.identity import api as identity_api
from nexus.shared.errors import ServiceError

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

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.trace_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception as error:
            # Handle before ServerErrorMiddleware re-raises to Uvicorn's traceback logger.
            response = await unexpected_error(request, error)
        response.headers["X-Request-ID"] = request.state.trace_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def error_response(request, status, code, message, **extra):
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

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        return error_response(request, error.status, error.code, str(error))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # FastAPI's default validation response can echo passwords and tokens in input/ctx.
        fields = [{"location": list(item["loc"]), "code": item["type"]} for item in error.errors()]
        return error_response(
            request, 422, "validation_error", "Invalid request parameters", fields=fields
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logging.getLogger("nexus.api").error(
            "Unhandled %s trace=%s", type(error).__name__, getattr(request.state, "trace_id", "")
        )
        return error_response(request, 500, "internal_error", "Internal server error")

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
        app.state.readiness = application.readiness
        app.state.identity_service = application.identity
        app.state.project_service = application.projects
        try:
            yield
        finally:
            await application.close()

    def get_identity(request: Request) -> IdentityService:
        return request.app.state.identity_service

    def get_projects(request: Request) -> ProjectService:
        return request.app.state.project_service

    app = build_server(lifespan, development=settings.environment != "production")
    app.include_router(identity_api.create_router(get_identity))
    app.include_router(identity_api.create_project_router(get_projects))
    return app
