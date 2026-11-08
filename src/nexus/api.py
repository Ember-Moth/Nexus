from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings

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
        try:
            yield
        finally:
            await application.close()

    return build_server(lifespan, development=settings.environment != "production")
