from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from backend import __version__
from backend.api.deps import AppSettings, DbSession
from backend.api.schemas import HealthOut
from backend.jobs.registry import registered_job_types

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(session: DbSession, settings: AppSettings) -> HealthOut:
    try:
        session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:  # pragma: no cover - surfaced in the response
        db_status = f"error: {type(exc).__name__}"
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        version=__version__,
        database=db_status,
        llm_configured=settings.llm_configured,
        llm_model=settings.llm_model if settings.llm_configured else None,
        sandbox_backend=settings.sandbox_backend,
        job_types=registered_job_types(),
    )
