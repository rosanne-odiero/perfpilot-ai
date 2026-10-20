"""Report endpoints: audit report requests, plus metadata, download and preview for every report
(audit and experiment reports share the ``reports`` table and artifact layout)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.deps import AppSettings, DbSession
from backend.core.exceptions import ValidationFailedError
from backend.infrastructure.artifact_store import ArtifactStore
from backend.models.enums import JobStatus, ReportFormat
from backend.models.orm.audit import ReportRecord
from backend.reporting import service

router = APIRouter(tags=["reports"])

PREVIEWABLE = {ReportFormat.HTML, ReportFormat.MARKDOWN}


class ReportRequest(BaseModel):
    formats: list[ReportFormat] = Field(
        default_factory=lambda: [ReportFormat.HTML], min_length=1, max_length=5
    )


class ReportJobAccepted(BaseModel):
    report_id: str
    job_id: str
    format: ReportFormat
    status: JobStatus
    report_status: str


class ReportsAccepted(BaseModel):
    audit_id: str
    reports: list[ReportJobAccepted]


class ReportFileOut(BaseModel):
    role: str
    artifact_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


class ReportOut(BaseModel):
    id: str
    audit_id: str | None
    subject_type: str = "audit"
    subject_id: str | None = None
    format: ReportFormat
    status: str
    error: str | None
    job_id: str | None
    artifact_id: str | None
    created_at: datetime
    updated_at: datetime
    files: list[ReportFileOut]
    meta: dict[str, Any]


def _report_out(session: Session, record: ReportRecord) -> ReportOut:
    artifacts = service.report_artifacts(session, record)
    primary = next((a for a in artifacts if a.meta.get("role") == service.PRIMARY_ROLE), None)
    meta = {k: v for k, v in (primary.meta if primary else {}).items() if k not in {"role"}}
    return ReportOut(
        id=record.id,
        audit_id=record.audit_id,
        subject_type=record.subject_type,
        subject_id=record.subject_id,
        format=ReportFormat(record.format),
        status=record.status,
        error=record.error,
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        files=[
            ReportFileOut(
                role=str(a.meta.get("role", "")),
                artifact_id=a.id,
                filename=a.filename,
                media_type=a.media_type,
                size_bytes=a.size_bytes,
                sha256=a.sha256,
            )
            for a in artifacts
        ],
        meta=meta,
    )


@router.post("/audits/{audit_id}/reports", status_code=202, response_model=ReportsAccepted)
def request_reports(audit_id: str, body: ReportRequest, session: DbSession) -> ReportsAccepted:
    """Start one ``report.generate`` job per format. Repeating the request for the same audit
    version returns the existing reports instead of creating duplicates."""
    pairs = service.request_reports(session, audit_id, body.formats)
    return ReportsAccepted(
        audit_id=audit_id,
        reports=[
            ReportJobAccepted(
                report_id=record.id,
                job_id=job.id,
                format=ReportFormat(record.format),
                status=JobStatus(job.status),
                report_status=record.status,
            )
            for record, job in pairs
        ],
    )


@router.get("/audits/{audit_id}/reports", response_model=list[ReportOut])
def list_reports(audit_id: str, session: DbSession) -> list[ReportOut]:
    return [_report_out(session, r) for r in service.list_reports(session, audit_id)]


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, session: DbSession) -> ReportOut:
    return _report_out(session, service.get_report(session, report_id))


@router.get("/reports/{report_id}/download", response_class=FileResponse)
def download_report(
    report_id: str,
    session: DbSession,
    settings: AppSettings,
    part: Literal["primary", "benchmark_samples", "validation_checks"] = Query("primary"),
) -> FileResponse:
    record = service.get_report(session, report_id)
    artifact = service.artifact_for_role(session, record, part)
    path = ArtifactStore(settings).path_for(artifact)
    return FileResponse(path, media_type=artifact.media_type, filename=artifact.filename)


@router.get("/reports/{report_id}/preview", response_class=PlainTextResponse)
def preview_report(report_id: str, session: DbSession, settings: AppSettings) -> PlainTextResponse:
    """The rendered HTML or Markdown as plain text (never served as active HTML)."""
    record = service.get_report(session, report_id)
    fmt = ReportFormat(record.format)
    if fmt not in PREVIEWABLE:
        raise ValidationFailedError(
            f"preview is only available for html and md reports, not {fmt.value}"
        )
    artifact = service.artifact_for_role(session, record, service.PRIMARY_ROLE)
    text = ArtifactStore(settings).read_bytes(artifact).decode("utf-8")
    return PlainTextResponse(
        text,
        headers={"X-Report-Format": fmt.value, "X-Content-Type-Options": "nosniff"},
    )
