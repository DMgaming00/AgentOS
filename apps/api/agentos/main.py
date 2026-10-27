"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agentos.api import (
    agents,
    approvals,
    arena,
    artifacts,
    auth,
    autopilot,
    chaos,
    deployments,
    documents,
    evaluations,
    events,
    experiments,
    finops,
    flight,
    memory,
    metrics,
    releases,
    runs,
    security,
    tools,
    workers,
    workflows,
    workspace,
)
from agentos.api.workspace import VERSION
from agentos.observability.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from agentos_core.config import get_settings
from agentos_core.db.session import dispose_engine, get_sessionmaker
from agentos_core.domain.errors import AgentOSError
from agentos_core.domain.states import IllegalTransitionError
from agentos_core.engine.core import EngineError
from agentos_core.log import configure_logging, get_logger
from agentos_core.notify import get_notifier
from agentos_core.telemetry import configure_tracing

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await get_notifier().close()
    await dispose_engine()


def _error(
    status: int, code: str, message: str, details: Any = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}}, status_code=status, headers=headers
    )


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_safe()
    configure_logging(settings.log_level, settings.log_json)
    configure_tracing("api")
    app = FastAPI(
        title="AgentOS API",
        version=VERSION,
        lifespan=lifespan,
        description="Durable AI agent workflow execution: DAG scheduling with leases and fencing, tool "
        "permissions, human approvals, events and cost tracking.",
    )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return _error(
            exc.status_code,
            detail.get("code", "error"),
            detail.get("message", ""),
            detail.get("details"),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            "validation_error",
            "request validation failed",
            [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()],
        )

    @app.exception_handler(EngineError)
    async def engine_error(_: Request, exc: EngineError) -> JSONResponse:
        return _error(exc.status_code, exc.code, str(exc))

    @app.exception_handler(IllegalTransitionError)
    async def transition_error(_: Request, exc: IllegalTransitionError) -> JSONResponse:
        return _error(409, "illegal_transition", str(exc))

    @app.exception_handler(AgentOSError)
    async def domain_error(_: Request, exc: AgentOSError) -> JSONResponse:
        return _error(400, exc.error_type, exc.message)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["system"])
    async def ready() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
        checks["redis"] = "ok" if await get_notifier().ping() else "unavailable (degraded: polling)"
        ok = checks["database"] == "ok"
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
        )

    for module in (
        auth,
        agents,
        workflows,
        runs,
        events,
        approvals,
        artifacts,
        documents,
        tools,
        metrics,
        workspace,
        autopilot,
        evaluations,
        experiments,
        deployments,
        flight,
        chaos,
        security,
        arena,
        memory,
        finops,
        releases,
        workers,
    ):
        app.include_router(module.router)

    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.max_request_bytes,
        upload_limit=settings.max_upload_bytes,
        upload_prefix="/api/v1/documents",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Workspace-Id", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["X-Request-Id", "Idempotent-Replayed", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.otel_enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready")
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("agentos.main:app", host="0.0.0.0", port=8000)  # noqa: S104 - container entrypoint
