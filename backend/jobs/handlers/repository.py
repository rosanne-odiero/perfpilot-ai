"""``repository.ingest``: acquire a repository snapshot and build its inventory."""

from __future__ import annotations

from typing import Any

from backend.core.exceptions import ValidationFailedError
from backend.jobs.registry import JobContext, job_handler
from backend.services.repository_service import INGEST_JOB_TYPE, run_ingestion


@job_handler(INGEST_JOB_TYPE)
def ingest_repository(ctx: JobContext) -> dict[str, Any]:
    snapshot_id = ctx.payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValidationFailedError("payload must contain a 'snapshot_id'")
    return run_ingestion(snapshot_id, ctx.settings, ctx.progress)
