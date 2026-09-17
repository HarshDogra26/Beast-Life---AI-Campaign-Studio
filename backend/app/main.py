"""FastAPI application: startup, shutdown, health and error handling.
"""

from __future__ import annotations
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .api import routes_assets, routes_campaigns, routes_events
from .api.deps import ProvidersDep, SettingsDep
from .api.schemas import HealthResponse
from .config import get_settings
from .graph.builder import concurrency_summary
from .logging import configure_logging, get_logger
from .providers.base import ProviderError
from .providers.registry import build_providers
from .storage.db import dispose_engine, init_db, ping
from .worker.runner import build_worker

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    log.info("startup.begin", mode=settings.provider_mode)
    await init_db()
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)

    providers = build_providers(settings)
    app.state.providers = providers

    worker = build_worker(providers)
    app.state.worker = worker

    swept = await worker.sweep_orphans()
    worker.start()

    if shutil.which(settings.ffmpeg_bin) is None:
        log.error("startup.ffmpeg_missing", binary=settings.ffmpeg_bin)

    log.info(
        "startup.ready",
        size_strategy=providers.size_strategy,
        image_model=providers.image_model,
        swept=swept,
    )
    try:
        yield
    finally:
        log.info("shutdown.begin")
        await worker.stop()
        await providers.aclose()
        await dispose_engine()
        log.info("shutdown.complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Campaign Creative Studio",
        version="1.0.0",
        description=(
            "Product brief to research-backed creative angles to two coordinated "
            "image ads and a short video."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes_campaigns.router)
    app.include_router(routes_assets.router)
    app.include_router(routes_events.router)

    _install_error_handlers(app)

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request, settings: SettingsDep) -> HealthResponse:
        providers = request.app.state.providers
        worker = request.app.state.worker
        return HealthResponse(
            status="ok",
            provider_mode=providers.mode.value,
            image_model=providers.image_model,
            image_model_family=providers.capabilities.family,
            size_strategy=providers.size_strategy.value,
            ffmpeg=(
                "available" if shutil.which(settings.ffmpeg_bin) else "MISSING"
            ),
            database="ok" if await ping() else "unreachable",
            mcp_url=settings.mcp_research_url,
            worker_epoch=worker.epoch,
            notes=providers.notes,
            failure_injection=providers.injector.stages(),
        )

    @app.get("/api/workflow", tags=["system"])
    async def workflow(providers: ProvidersDep) -> dict[str, Any]:
        """Describes the graph's stages and where concurrency happens."""
        return {
            "concurrency": concurrency_summary(),
            "size_strategy": providers.size_strategy.value,
            "image_model_family": providers.capabilities.family,
            "capability_notes": providers.notes,
        }

    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Server-side rejection of invalid input, in a stable shape."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "The request failed validation.",
                "code": "validation_error",
                "detail": [
                    {
                        "field": ".".join(str(p) for p in err["loc"][1:]) or "body",
                        "message": err["msg"],
                        "type": err["type"],
                    }
                    for err in exc.errors()[:20]
                ],
            },
        )

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, exc: ProviderError) -> JSONResponse:
        """Provider faults become an understandable status, not a 500.
        """
        code = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if exc.kind == "rate_limit"
            else status.HTTP_504_GATEWAY_TIMEOUT
            if exc.kind == "timeout"
            else status.HTTP_502_BAD_GATEWAY
        )
        log.warning("api.provider_error", kind=exc.kind, error=str(exc)[:300])
        return JSONResponse(
            status_code=code,
            content={"error": str(exc), "code": exc.kind, "detail": {"retryable": exc.retryable}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Log the detail; return a generic message rather than leaking internals.
        log.exception("api.unhandled_error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "An unexpected internal error occurred.",
                "code": "internal_error",
            },
        )


app = create_app()
