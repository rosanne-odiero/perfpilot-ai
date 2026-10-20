"""Repository import orchestration: records, jobs, and the ``repository.ingest`` pipeline."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import (
    ConflictError,
    LimitExceededError,
    NotConfiguredError,
    NotFoundError,
    PerfPilotError,
    ValidationFailedError,
)
from backend.core.logging import get_logger
from backend.core.redaction import RedactionResult, is_sensitive_path, redact_text
from backend.infrastructure.artifact_store import ArtifactStore, safe_filename
from backend.ingestion.errors import SensitiveFileError
from backend.ingestion.filters import SkipReason
from backend.ingestion.inventory import build_inventory, inventory_content_hash, read_capped
from backend.ingestion.providers import get_provider
from backend.ingestion.providers.github_private import NOT_CONFIGURED_MESSAGE
from backend.ingestion.providers.github_public import parse_github_url
from backend.ingestion.providers.local_dir import resolve_allowed_local_path
from backend.ingestion.providers.zip_upload import UPLOAD_ARTIFACT_KIND
from backend.ingestion.upload import StagedUpload
from backend.ingestion.workspace import (
    cleanup_workspace,
    create_workspace,
    get_workspace,
    make_read_only,
    normalize_rel_path,
    resolve_in_workspace,
)
from backend.jobs.service import enqueue_job
from backend.models.audit_schemas import RepositoryInventory, RepositoryRef, SourceFileInfo
from backend.models.database import session_scope
from backend.models.enums import JobStatus, RepositorySourceType, SnapshotStatus
from backend.models.orm.audit import Repository, RepositorySnapshot
from backend.models.orm.platform import Artifact, Job

log = get_logger(__name__)

INGEST_JOB_TYPE = "repository.ingest"
SNAPSHOT_SUBJECT = "snapshot"
_ZIP_LOCATOR_PREFIX = "zip:sha256:"

ProgressFn = Callable[[str, float, str | None], None]


@dataclass(frozen=True)
class ImportResult:
    repository: Repository
    snapshot: RepositorySnapshot
    job: Job
    reused: bool = False


@dataclass(frozen=True)
class _Location:
    name: str
    locator: str
    requested_ref: str | None


def _locate(ref: RepositoryRef, settings: Settings) -> _Location:
    """Validate a ref synchronously so bad input fails the request, not a background job."""
    match ref.source_type:
        case RepositorySourceType.GITHUB_PUBLIC:
            location = parse_github_url(ref.url or "", settings.github_allowed_hosts, ref.ref)
            return _Location(
                ref.display_name or location.full_name, location.canonical_url, location.ref
            )
        case RepositorySourceType.LOCAL_DIRECTORY:
            if ref.ref is not None:
                raise ValidationFailedError("'ref' is only supported for git sources")
            path = resolve_allowed_local_path(ref.local_path or "", settings)
            return _Location(ref.display_name or path.name, str(path), None)
        case RepositorySourceType.GITHUB_PRIVATE:
            raise NotConfiguredError(
                NOT_CONFIGURED_MESSAGE, details={"docs": "docs/private-github.md"}
            )
        case _:
            raise ValidationFailedError("upload ZIP archives with POST /api/repositories/upload")


def _get_or_create_repository(
    session: Session, source_type: RepositorySourceType, location: _Location
) -> Repository:
    stmt = select(Repository).where(
        Repository.source_type == source_type.value,
        Repository.source_locator == location.locator,
    )
    if location.requested_ref is None:
        stmt = stmt.where(Repository.requested_ref.is_(None))
    else:
        stmt = stmt.where(Repository.requested_ref == location.requested_ref)
    repository = session.scalars(stmt.order_by(Repository.created_at).limit(1)).first()
    if repository is None:
        repository = Repository(
            name=location.name[:255],
            source_type=source_type.value,
            source_locator=location.locator,
            requested_ref=location.requested_ref,
        )
        session.add(repository)
        session.flush()
    return repository


def _start_snapshot(session: Session, repository: Repository, settings: Settings) -> ImportResult:
    """Create a PENDING snapshot + ingest job, or return the one already in flight."""
    in_flight = session.scalars(
        select(RepositorySnapshot)
        .where(
            RepositorySnapshot.repository_id == repository.id,
            RepositorySnapshot.status.in_([SnapshotStatus.PENDING, SnapshotStatus.INGESTING]),
        )
        .order_by(RepositorySnapshot.created_at.desc())
        .limit(1)
    ).first()
    if in_flight is not None and in_flight.job_id is not None:
        job = session.get(Job, in_flight.job_id)
        if job is not None and not JobStatus(job.status).is_terminal:
            return ImportResult(repository, in_flight, job, reused=True)
    snapshot = RepositorySnapshot(repository_id=repository.id, status=SnapshotStatus.PENDING)
    session.add(snapshot)
    session.flush()
    job = enqueue_job(
        session,
        INGEST_JOB_TYPE,
        {"snapshot_id": snapshot.id},
        idempotency_key=f"{INGEST_JOB_TYPE}:{snapshot.id}",
        timeout_seconds=max(settings.job_default_timeout_seconds, settings.clone_timeout_seconds),
        subject_type=SNAPSHOT_SUBJECT,
        subject_id=snapshot.id,
    )
    snapshot.job_id = job.id
    session.flush()
    return ImportResult(repository, snapshot, job)


def import_repository(session: Session, ref: RepositoryRef, settings: Settings) -> ImportResult:
    """Register a GitHub/local repository and enqueue its ingestion."""
    location = _locate(ref, settings)
    repository = _get_or_create_repository(session, ref.source_type, location)
    return _start_snapshot(session, repository, settings)


def import_upload(
    session: Session, staged: StagedUpload, settings: Settings, display_name: str | None = None
) -> ImportResult:
    """Store an uploaded ZIP (deduplicated by sha256) and enqueue its ingestion."""
    store = ArtifactStore(settings)
    artifact = session.scalars(
        select(Artifact)
        .where(Artifact.kind == UPLOAD_ARTIFACT_KIND, Artifact.sha256 == staged.sha256)
        .order_by(Artifact.created_at.desc())
        .limit(1)
    ).first()
    if artifact is not None:
        try:
            store.path_for(artifact)
        except NotFoundError:
            artifact = None
    if artifact is None:
        artifact = store.save_file(
            session,
            kind=UPLOAD_ARTIFACT_KIND,
            source=staged.path,
            filename=staged.filename,
            media_type="application/zip",
        )
    name = display_name or PurePosixPath(safe_filename(staged.filename)).stem
    location = _Location(name, f"{_ZIP_LOCATOR_PREFIX}{staged.sha256}", None)
    repository = _get_or_create_repository(session, RepositorySourceType.ZIP_UPLOAD, location)
    if artifact.owner_id is None:
        artifact.owner_type = "repository"
        artifact.owner_id = repository.id
    return _start_snapshot(session, repository, settings)


def ref_for_repository(session: Session, repository: Repository) -> RepositoryRef:
    """Rebuild the provider input for a stored repository (used by the ingest job)."""
    source_type = RepositorySourceType(repository.source_type)
    if source_type is RepositorySourceType.ZIP_UPLOAD:
        sha = repository.source_locator.removeprefix(_ZIP_LOCATOR_PREFIX)
        artifact = session.scalars(
            select(Artifact)
            .where(Artifact.kind == UPLOAD_ARTIFACT_KIND, Artifact.sha256 == sha)
            .order_by(Artifact.created_at.desc())
            .limit(1)
        ).first()
        if artifact is None:
            raise NotFoundError("the uploaded archive for this repository no longer exists")
        return RepositoryRef(
            source_type=source_type, upload_artifact_id=artifact.id, display_name=repository.name
        )
    if source_type is RepositorySourceType.LOCAL_DIRECTORY:
        return RepositoryRef(
            source_type=source_type,
            local_path=repository.source_locator,
            display_name=repository.name,
        )
    return RepositoryRef(
        source_type=source_type,
        url=repository.source_locator,
        ref=repository.requested_ref,
        display_name=repository.name,
    )


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, PerfPilotError):
        return exc.message
    return redact_text(f"{type(exc).__name__}: {exc}").text[:1000]


def run_ingestion(snapshot_id: str, settings: Settings, progress: ProgressFn) -> dict[str, Any]:
    """Acquire -> inventory -> content hash -> READY. On failure the snapshot becomes FAILED."""
    with session_scope() as session:
        snapshot = session.get(RepositorySnapshot, snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"snapshot {snapshot_id} not found")
        if snapshot.status == SnapshotStatus.READY:
            return {"snapshot_id": snapshot_id, "status": SnapshotStatus.READY.value}
        repository = session.get(Repository, snapshot.repository_id)
        if repository is None:
            raise NotFoundError(f"repository {snapshot.repository_id} not found")
        snapshot.status = SnapshotStatus.INGESTING
        snapshot.error = None
        ref = ref_for_repository(session, repository)
    source_type = ref.source_type
    try:
        progress("acquire", 0.05, f"acquiring {source_type.value.lower()} source")
        workspace = create_workspace(settings, snapshot_id)
        acquired = get_provider(source_type).acquire(ref, workspace.src, settings)
        progress("inventory", 0.5, "classifying files, detecting technologies and dependencies")
        inventory = build_inventory(
            workspace.src,
            settings,
            prefiltered=acquired.skipped,
            limits_hit=acquired.limits_hit,
            root_name=acquired.display_name,
        )
        progress("finalize", 0.9, "hashing content and sealing the workspace")
        content_hash = inventory_content_hash(inventory)
        make_read_only(workspace.src)
    except Exception as exc:
        with session_scope() as session:
            failed = session.get(RepositorySnapshot, snapshot_id)
            if failed is not None:
                failed.status = SnapshotStatus.FAILED
                failed.error = _error_message(exc)
        with contextlib.suppress(OSError, PerfPilotError):
            cleanup_workspace(settings, snapshot_id)
        raise
    with session_scope() as session:
        ready = session.get(RepositorySnapshot, snapshot_id)
        if ready is None:
            raise NotFoundError(f"snapshot {snapshot_id} vanished during ingestion")
        ready.status = SnapshotStatus.READY
        ready.workspace_path = str(workspace.src)
        ready.commit_sha = acquired.git.commit_sha
        ready.branch = acquired.git.branch
        ready.git = acquired.git.model_dump(mode="json")
        ready.inventory = inventory.model_dump(mode="json")
        ready.content_hash = content_hash
        ready.error = None
    python_files = len(inventory.python_files)
    log.info(
        "repository ingested",
        extra={
            "snapshot_id": snapshot_id,
            "files_seen": inventory.total_files_seen,
            "python_files": python_files,
            "limits_hit": len(inventory.limits_hit),
        },
    )
    return {
        "snapshot_id": snapshot_id,
        "status": SnapshotStatus.READY.value,
        "files_seen": inventory.total_files_seen,
        "python_files": python_files,
        "content_hash": content_hash,
        "limits_hit": inventory.limits_hit,
    }


# --- Queries -------------------------------------------------------------------------------


def list_repositories(session: Session, *, limit: int = 100) -> list[Repository]:
    return list(
        session.scalars(select(Repository).order_by(Repository.created_at.desc()).limit(limit))
    )


def get_repository(session: Session, repository_id: str) -> Repository:
    repository = session.get(Repository, repository_id)
    if repository is None:
        raise NotFoundError(f"repository {repository_id} not found")
    return repository


def latest_snapshot(session: Session, repository_id: str) -> RepositorySnapshot | None:
    return session.scalars(
        select(RepositorySnapshot)
        .where(RepositorySnapshot.repository_id == repository_id)
        .order_by(RepositorySnapshot.created_at.desc())
        .limit(1)
    ).first()


def get_snapshot(session: Session, snapshot_id: str) -> RepositorySnapshot:
    snapshot = session.get(RepositorySnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(f"snapshot {snapshot_id} not found")
    return snapshot


def snapshot_inventory(snapshot: RepositorySnapshot) -> RepositoryInventory:
    if snapshot.status != SnapshotStatus.READY or snapshot.inventory is None:
        raise ConflictError(
            f"snapshot {snapshot.id} is {snapshot.status}; its inventory is not available",
            details={"status": snapshot.status},
        )
    return RepositoryInventory.model_validate(snapshot.inventory)


def filter_files(
    inventory: RepositoryInventory,
    *,
    language: str | None = None,
    python_only: bool = False,
    include_skipped: bool = True,
) -> list[SourceFileInfo]:
    files = inventory.files
    if not include_skipped:
        files = [f for f in files if not f.skipped]
    if python_only:
        files = [f for f in files if f.is_python]
    if language:
        wanted = language.lower()
        files = [f for f in files if (f.language or "").lower() == wanted]
    return files


@dataclass(frozen=True)
class FileContent:
    info: SourceFileInfo
    redaction: RedactionResult


def read_snapshot_file(
    settings: Settings, snapshot: RepositorySnapshot, raw_path: str
) -> FileContent:
    """Serve one analyzed file as redacted text. Sensitive and skipped files are refused."""
    rel = normalize_rel_path(raw_path)
    if is_sensitive_path(rel):
        raise SensitiveFileError("this file is classified as sensitive and is never served")
    inventory = snapshot_inventory(snapshot)
    info = next((f for f in inventory.files if f.path == rel), None)
    if info is None:
        raise NotFoundError(f"{rel} is not part of snapshot {snapshot.id}")
    if info.skipped:
        if info.skip_reason == SkipReason.SENSITIVE:
            raise SensitiveFileError("this file is classified as sensitive and is never served")
        raise ValidationFailedError(
            f"{rel} was skipped during ingestion ({info.skip_reason}); its content is not served",
            details={"skip_reason": info.skip_reason},
        )
    workspace = get_workspace(settings, snapshot.id)
    path = resolve_in_workspace(workspace.src, rel)
    try:
        data = read_capped(path, settings.max_file_bytes)
    except OSError as exc:
        raise NotFoundError(f"{rel} is missing from the snapshot workspace") from exc
    if data is None:
        raise LimitExceededError(f"{rel} exceeds the per-file size limit")
    if hashlib.sha256(data).hexdigest() != info.sha256:
        raise ConflictError(f"{rel} changed since the snapshot was ingested")
    return FileContent(info=info, redaction=redact_text(data.decode("utf-8", errors="replace")))
