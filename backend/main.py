"""FastAPI application factory.

Routers are discovered automatically: any module in ``backend/api/routes/`` exposing a module-level
``router`` (an ``APIRouter``) is mounted under ``/api``.
"""

from __future__ import annotations

import importlib
import pkgutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.core.config import get_settings
from backend.core.exceptions import PerfPilotError
from backend.core.logging import configure_logging, get_logger
from backend.models.database import init_engine

log = get_logger(__name__)


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _include_routers(app: FastAPI) -> None:
    import backend.api.routes as routes_pkg

    for info in sorted(pkgutil.iter_modules(routes_pkg.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{routes_pkg.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router, prefix="/api")


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        init_engine(settings)
        yield

    app = FastAPI(
        title="PerfPilot AI",
        version=__version__,
        description=(
            "Repository performance audits and evidence-backed Python optimization. "
            "Every result is labeled MEASURED, STATIC, or AI_HYPOTHESIS."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def timing(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        return response

    @app.exception_handler(PerfPilotError)
    async def _perfpilot_error(_: Request, exc: PerfPilotError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return _error(422, "request_validation_error", "invalid request", {"errors": errors})

    _include_routers(app)
    return app


app = create_app()
