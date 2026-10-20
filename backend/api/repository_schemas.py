"""Response models for repository and snapshot endpoints."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from pydantic import BaseModel, Field

from backend.ingestion.dependencies import find_lock_files
from backend.ingestion.inventory import skipped_counts
from backend.models.audit_schemas import (
    DependencySpec,
    DetectedTechnology,
    GitMetadata,
    RepositoryInventory,
    SourceFileInfo,
)
from backend.models.enums import RepositorySourceType, SnapshotStatus
from backend.models.orm.audit import Repository, RepositorySnapshot


class SnapshotCounts(BaseModel):
    files_seen: int
    files_analyzed: int
    files_skipped: int
    directories_pruned: int
    python_files: int
    bytes_seen: int
    skipped_by_reason: dict[str, int]


class SnapshotSummary(BaseModel):
    id: str
    repository_id: str
    status: SnapshotStatus
    error: str | None
    job_id: str | None
    commit_sha: str | None
    branch: str | None
    git: GitMetadata
    content_hash: str | None
    root_name: str | None = None
    counts: SnapshotCounts | None = None
    languages: dict[str, int] = Field(default_factory=dict)
    technologies: list[DetectedTechnology] = Field(default_factory=list)
    dependencies: list[DependencySpec] = Field(default_factory=list)
    lock_files: list[str] = Field(default_factory=list)
    limits_hit: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_snapshot(cls, snapshot: RepositorySnapshot) -> SnapshotSummary:
        summary = cls(
            id=snapshot.id,
            repository_id=snapshot.repository_id,
            status=SnapshotStatus(snapshot.status),
            error=snapshot.error,
            job_id=snapshot.job_id,
            commit_sha=snapshot.commit_sha,
            branch=snapshot.branch,
            git=GitMetadata.model_validate(snapshot.git or {}),
            content_hash=snapshot.content_hash,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
        )
        if snapshot.inventory is None:
            return summary
        inventory = RepositoryInventory.model_validate(snapshot.inventory)
        files = [f for f in inventory.files if not f.path.endswith("/")]
        kept = Counter(f.skipped for f in files)
        summary.root_name = inventory.root_name
        summary.counts = SnapshotCounts(
            files_seen=inventory.total_files_seen,
            files_analyzed=kept[False],
            files_skipped=kept[True],
            directories_pruned=len(inventory.files) - len(files),
            python_files=len(inventory.python_files),
            bytes_seen=inventory.total_bytes_seen,
            skipped_by_reason=skipped_counts(inventory),
        )
        summary.languages = inventory.languages
        summary.technologies = inventory.technologies
        summary.dependencies = inventory.dependencies
        summary.lock_files = find_lock_files(f.path for f in files)
        summary.limits_hit = inventory.limits_hit
        return summary


class RepositoryOut(BaseModel):
    id: str
    name: str
    source_type: RepositorySourceType
    source_locator: str
    requested_ref: str | None
    created_at: datetime
    latest_snapshot_id: str | None = None
    latest_snapshot_status: SnapshotStatus | None = None

    @classmethod
    def from_repository(
        cls, repository: Repository, latest: RepositorySnapshot | None
    ) -> RepositoryOut:
        return cls(
            id=repository.id,
            name=repository.name,
            source_type=RepositorySourceType(repository.source_type),
            source_locator=repository.source_locator,
            requested_ref=repository.requested_ref,
            created_at=repository.created_at,
            latest_snapshot_id=latest.id if latest else None,
            latest_snapshot_status=SnapshotStatus(latest.status) if latest else None,
        )


class RepositoryDetailOut(RepositoryOut):
    latest_snapshot: SnapshotSummary | None = None


class FileListOut(BaseModel):
    snapshot_id: str
    total: int
    files: list[SourceFileInfo]


class FileContentOut(BaseModel):
    snapshot_id: str
    path: str
    language: str | None
    size_bytes: int
    sha256: str | None
    content: str
    redactions: int = Field(description="Number of secrets replaced with [REDACTED]")
    redaction_kinds: dict[str, int] = Field(default_factory=dict)
