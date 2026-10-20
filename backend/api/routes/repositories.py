"""Repository import and snapshot inventory endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status
from starlette.concurrency import run_in_threadpool

from backend.api.deps import AppSettings, DbSession
from backend.api.repository_schemas import (
    FileContentOut,
    FileListOut,
    RepositoryDetailOut,
    RepositoryOut,
    SnapshotSummary,
)
from backend.api.schemas import JobAccepted
from backend.core.config import Settings
from backend.ingestion.upload import StagedUpload, receive_zip_upload
from backend.models.audit_schemas import RepositoryRef
from backend.models.database import session_scope
from backend.models.enums import JobStatus
from backend.services import repository_service as service

router = APIRouter(tags=["repositories"])


def _accepted(result: service.ImportResult) -> JobAccepted:
    return JobAccepted(
        job_id=result.job.id,
        status=JobStatus(result.job.status),
        subject_type=service.SNAPSHOT_SUBJECT,
        subject_id=result.snapshot.id,
    )


@router.post(
    "/repositories",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Import a public GitHub repository or an allowlisted local directory",
)
def import_repository(ref: RepositoryRef, session: DbSession, settings: AppSettings) -> JobAccepted:
    return _accepted(service.import_repository(session, ref, settings))


def _register_upload(staged: StagedUpload, settings: Settings) -> JobAccepted:
    with session_scope() as session:
        result = service.import_upload(
            session, staged, settings, display_name=staged.fields.get("display_name") or None
        )
        return _accepted(result)


@router.post(
    "/repositories/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Import a repository from a ZIP archive (multipart field 'file')",
)
async def upload_repository(request: Request, settings: AppSettings) -> JobAccepted:
    staged = await receive_zip_upload(request, settings)
    try:
        return await run_in_threadpool(_register_upload, staged, settings)
    finally:
        staged.discard()


@router.get("/repositories", response_model=list[RepositoryOut])
def list_repositories(
    session: DbSession, limit: int = Query(100, ge=1, le=500)
) -> list[RepositoryOut]:
    return [
        RepositoryOut.from_repository(repo, service.latest_snapshot(session, repo.id))
        for repo in service.list_repositories(session, limit=limit)
    ]


@router.get("/repositories/{repository_id}", response_model=RepositoryDetailOut)
def get_repository(repository_id: str, session: DbSession) -> RepositoryDetailOut:
    repository = service.get_repository(session, repository_id)
    latest = service.latest_snapshot(session, repository.id)
    base = RepositoryOut.from_repository(repository, latest)
    return RepositoryDetailOut(
        **base.model_dump(),
        latest_snapshot=SnapshotSummary.from_snapshot(latest) if latest else None,
    )


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotSummary)
def get_snapshot(snapshot_id: str, session: DbSession) -> SnapshotSummary:
    return SnapshotSummary.from_snapshot(service.get_snapshot(session, snapshot_id))


@router.get("/snapshots/{snapshot_id}/files", response_model=FileListOut)
def list_snapshot_files(
    snapshot_id: str,
    session: DbSession,
    language: str | None = Query(None, max_length=64),
    python_only: bool = False,
    include_skipped: bool = True,
) -> FileListOut:
    inventory = service.snapshot_inventory(service.get_snapshot(session, snapshot_id))
    files = service.filter_files(
        inventory, language=language, python_only=python_only, include_skipped=include_skipped
    )
    return FileListOut(snapshot_id=snapshot_id, total=len(files), files=files)


@router.get("/snapshots/{snapshot_id}/files/content", response_model=FileContentOut)
def get_snapshot_file_content(
    snapshot_id: str,
    session: DbSession,
    settings: AppSettings,
    path: str = Query(..., min_length=1, max_length=1024),
) -> FileContentOut:
    snapshot = service.get_snapshot(session, snapshot_id)
    content = service.read_snapshot_file(settings, snapshot, path)
    return FileContentOut(
        snapshot_id=snapshot_id,
        path=content.info.path,
        language=content.info.language,
        size_bytes=content.info.size_bytes,
        sha256=content.info.sha256,
        content=content.redaction.text,
        redactions=content.redaction.count,
        redaction_kinds=content.redaction.kinds,
    )
