"""Runtime Audit persistence and queries shared by the API routes and the worker jobs.

Nothing in this module talks to a sandbox backend: the API uses it, and per ADR-005 the API never
touches Docker. Availability comes from the status the worker last recorded.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.audit.correlation import (
    CorrelationHotspot,
    CorrelationResult,
    FindingCorrelation,
    RuntimeOnlyHotspot,
    correlate,
)
from backend.core.config import Settings
from backend.core.exceptions import ConflictError, NotFoundError, UnavailableError
from backend.jobs.service import enqueue_job
from backend.models.audit_schemas import Finding
from backend.models.enums import AuditMode, AuditStage, AuditStatus
from backend.models.orm.audit import FindingRecord
from backend.models.orm.platform import Job
from backend.models.orm.runtime import RuntimeCommand
from backend.models.orm.runtime_profiles import (
    RuntimeCorrelationRecord,
    RuntimeHotspotRecord,
    RuntimeProfileRun,
    RuntimeRunStatus,
)
from backend.runtime.backends import latest_backend_status
from backend.runtime.schemas import (
    ApprovalRequest,
    HotspotOut,
    LimitsOverride,
    MemoryOut,
    RunDetailOut,
    RunSummaryOut,
    RuntimeAuditOut,
    RuntimeCommandOut,
)
from backend.sandbox.approvals import approve_command
from backend.sandbox.backend import SandboxLimits
from backend.sandbox.command_discovery import CandidateCommand
from backend.services import audits as audit_service

RUNTIME_PROFILE_JOB = "runtime.profile"
BACKEND_CHECK_JOB = "runtime.check_backend"
JOB_TIMEOUT_MARGIN_SECONDS = 300
OVERVIEW_HOTSPOTS = 25
DETAIL_HOTSPOTS = 100


# ----------------------------------------------------------------------------- commands


def list_commands(session: Session, audit_id: str) -> list[RuntimeCommandOut]:
    audit_service.get_audit(session, audit_id)
    rows = session.scalars(
        select(RuntimeCommand)
        .where(RuntimeCommand.audit_id == audit_id)
        .order_by(RuntimeCommand.created_at, RuntimeCommand.id)
    )
    commands: list[RuntimeCommandOut] = []
    for row in rows:
        command = CandidateCommand.model_validate(row.command)
        commands.append(
            RuntimeCommandOut(
                id=row.id,
                command_key=row.command_key,
                kind=command.kind,
                argv=command.argv,
                workdir_rel=command.workdir_rel,
                description=command.description,
                evidence=command.evidence,
                requires_dependencies=command.requires_dependencies,
                third_party_imports=command.third_party_imports,
                estimated_risk=command.estimated_risk,
                runnable=command.runnable,
                reason_if_not_runnable=command.reason_if_not_runnable,
                provenance=command.provenance,
                discovered_at=row.discovered_at,
            )
        )
    return commands


# ----------------------------------------------------------------------------- approvals


def merged_limits(settings: Settings, override: LimitsOverride | None) -> SandboxLimits | None:
    if override is None:
        return None
    defaults = SandboxLimits.from_settings(settings).model_dump()
    values = {k: v for k, v in override.model_dump().items() if v is not None}
    return SandboxLimits(**{**defaults, **values})


def job_timeout_seconds(settings: Settings, limits: SandboxLimits, with_install: bool) -> int:
    install = settings.sandbox_install_timeout_seconds if with_install else 0
    return limits.timeout_seconds + install + JOB_TIMEOUT_MARGIN_SECONDS


def request_run(
    session: Session, settings: Settings, audit_id: str, body: ApprovalRequest
) -> tuple[RuntimeProfileRun, Job]:
    """Approve exactly one discovered command for one run and queue it for the worker."""
    audit = audit_service.get_audit(session, audit_id)
    if audit.mode != AuditMode.RUNTIME:
        raise ConflictError("runtime commands can only be approved for RUNTIME audits")
    if audit.status not in (AuditStatus.SUCCEEDED, AuditStatus.PARTIAL) or audit.stage not in (
        AuditStage.AWAITING_RUNTIME_APPROVAL,
        AuditStage.COMPLETED,
    ):
        raise ConflictError(
            "the static stages and command discovery have not finished for this audit",
            details={"status": audit.status, "stage": audit.stage},
        )
    status = latest_backend_status(session, settings)
    if not status.available:
        raise UnavailableError(
            f"runtime mode is disabled: {status.reason}",
            details={"backend": status.backend, "checked_at": str(status.checked_at)},
        )
    active = session.scalar(
        select(RuntimeProfileRun.id).where(
            RuntimeProfileRun.audit_id == audit_id,
            RuntimeProfileRun.status.in_([s.value for s in RuntimeRunStatus if s.is_active]),
        )
    )
    if active is not None:
        raise ConflictError("another runtime run is still in progress", details={"run_id": active})
    approval = approve_command(
        session,
        audit_id,
        body.command_id,
        argv=body.argv,
        approved_by=body.approved_by,
        allow_dependency_install=body.allow_dependency_install,
        allow_sdist_builds=body.allow_sdist_builds,
        limits_override=merged_limits(settings, body.limits_override),
        settings=settings,
    )
    command = session.get(RuntimeCommand, approval.command_id)
    run = RuntimeProfileRun(
        audit_id=audit_id,
        snapshot_id=approval.snapshot_id,
        command_id=approval.command_id,
        approval_id=approval.id,
        status=RuntimeRunStatus.QUEUED,
        argv=list(approval.argv),
        workdir_rel=approval.workdir_rel,
        command_kind=command.kind if command else "unknown",
        approved_by=approval.approved_by,
        trace_memory=body.trace_memory,
        allow_dependency_install=approval.allow_dependency_install,
        limits=dict(approval.limits),
    )
    session.add(run)
    session.flush()
    limits = SandboxLimits.model_validate(approval.limits)
    job = enqueue_job(
        session,
        RUNTIME_PROFILE_JOB,
        {"run_id": run.id},
        idempotency_key=f"{RUNTIME_PROFILE_JOB}:{run.id}",
        timeout_seconds=job_timeout_seconds(settings, limits, approval.allow_dependency_install),
        # The approval is single use, so a retry could never run the command again.
        max_attempts=1,
        subject_type="runtime_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


def request_backend_check(session: Session) -> Job:
    return enqueue_job(session, BACKEND_CHECK_JOB, {}, max_attempts=1, subject_type="runtime")


# ----------------------------------------------------------------------------- correlation


def _correlation_hotspots(session: Session, run_id: str) -> list[CorrelationHotspot]:
    rows = session.scalars(
        select(RuntimeHotspotRecord).where(
            RuntimeHotspotRecord.run_id == run_id, RuntimeHotspotRecord.is_user_code.is_(True)
        )
    )
    return [
        CorrelationHotspot(
            id=row.id,
            function_name=row.function_name,
            file_path=row.file_path,
            line_number=row.line_number,
            total_calls=row.total_calls,
            internal_time_ms=row.internal_time_ms,
            cumulative_time_ms=row.cumulative_time_ms,
            percentage_of_profiled_time=row.percentage_of_profiled_time,
            attributed_time_ms=row.attributed_time_ms,
            attributed_percentage=row.attributed_percentage,
            is_user_code=row.is_user_code,
        )
        for row in rows
    ]


def apply_correlation(session: Session, audit_id: str, run_id: str) -> CorrelationResult:
    """Correlate a successful run with the audit's findings and persist the result.

    Finding evidence always reflects the most recent successful run: every finding is reset to
    its static score before the new evidence is applied, so repeating this is idempotent.
    """
    audit = audit_service.get_audit(session, audit_id)
    index = audit_service.require_code_index(audit)
    records = list(session.scalars(select(FindingRecord).where(FindingRecord.audit_id == audit_id)))
    findings = [Finding.model_validate(r.data) for r in records]
    hotspots = _correlation_hotspots(session, run_id)
    result = correlate(run_id, index, findings, hotspots)

    by_id = {f.id: f for f in result.findings}
    for record in records:
        finding = by_id[Finding.model_validate(record.data).id]
        record.data = finding.model_dump(mode="json")
        record.priority_score = finding.priority_score
    symbols = {c.hotspot_id: c.location.symbol for c in result.correlations}
    symbols.update({r.hotspot_id: r.location.symbol for r in result.runtime_only})
    for hotspot in session.scalars(
        select(RuntimeHotspotRecord).where(RuntimeHotspotRecord.id.in_(list(symbols)))
    ):
        hotspot.symbol = symbols[hotspot.id]
    session.execute(
        delete(RuntimeCorrelationRecord).where(RuntimeCorrelationRecord.run_id == run_id)
    )
    session.add_all(
        RuntimeCorrelationRecord(
            audit_id=audit_id,
            run_id=run_id,
            hotspot_id=c.hotspot_id,
            kind="finding",
            finding_id=c.finding_id,
            data=c.model_dump(mode="json"),
        )
        for c in result.correlations
    )
    session.add_all(
        RuntimeCorrelationRecord(
            audit_id=audit_id,
            run_id=run_id,
            hotspot_id=r.hotspot_id,
            kind="runtime_only",
            data=r.model_dump(mode="json"),
        )
        for r in result.runtime_only
    )
    session.flush()
    return result


# ----------------------------------------------------------------------------- queries


def _run_summary(run: RuntimeProfileRun) -> RunSummaryOut:
    return RunSummaryOut(
        id=run.id,
        status=run.status,
        failure_reason=run.failure_reason,
        argv=[str(a) for a in run.argv],
        workdir_rel=run.workdir_rel,
        command_kind=run.command_kind,
        command_id=run.command_id,
        approval_id=run.approval_id,
        approved_by=run.approved_by,
        job_id=run.job_id,
        exit_code=run.exit_code,
        command_exit_code=run.command_exit_code,
        timed_out=run.timed_out,
        oom_killed=run.oom_killed,
        duration_s=run.duration_s,
        wall_time_ms=run.wall_time_ms,
        cpu_time_ms=run.cpu_time_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _hotspot_out(row: RuntimeHotspotRecord) -> HotspotOut:
    return HotspotOut(
        id=row.id,
        rank=row.rank,
        function_name=row.function_name,
        symbol=row.symbol,
        file_path=row.file_path,
        line_number=row.line_number,
        location_kind=row.location_kind,
        is_user_code=row.is_user_code,
        primitive_calls=row.primitive_calls,
        total_calls=row.total_calls,
        internal_time_ms=row.internal_time_ms,
        cumulative_time_ms=row.cumulative_time_ms,
        attributed_time_ms=row.attributed_time_ms,
        percentage_of_profiled_time=row.percentage_of_profiled_time,
        cumulative_percentage=row.cumulative_percentage,
        attributed_percentage=row.attributed_percentage,
    )


def run_hotspots(
    session: Session, run_id: str, *, user_only: bool, limit: int
) -> list[RuntimeHotspotRecord]:
    stmt = select(RuntimeHotspotRecord).where(RuntimeHotspotRecord.run_id == run_id)
    if user_only:
        stmt = stmt.where(RuntimeHotspotRecord.is_user_code.is_(True))
        stmt = stmt.order_by(RuntimeHotspotRecord.attributed_time_ms.desc())
    else:
        stmt = stmt.order_by(RuntimeHotspotRecord.rank)
    return list(session.scalars(stmt.limit(limit)))


def run_correlations(
    session: Session, run_id: str
) -> tuple[list[FindingCorrelation], list[RuntimeOnlyHotspot]]:
    rows = list(
        session.scalars(
            select(RuntimeCorrelationRecord).where(RuntimeCorrelationRecord.run_id == run_id)
        )
    )
    correlations = [FindingCorrelation.model_validate(r.data) for r in rows if r.kind == "finding"]
    runtime_only = [
        RuntimeOnlyHotspot.model_validate(r.data) for r in rows if r.kind == "runtime_only"
    ]
    correlations.sort(key=lambda c: (-c.measured_priority_score, c.finding_id))
    runtime_only.sort(key=lambda r: -r.attributed_time_ms)
    return correlations, runtime_only


def list_runs(session: Session, audit_id: str) -> list[RuntimeProfileRun]:
    return list(
        session.scalars(
            select(RuntimeProfileRun)
            .where(RuntimeProfileRun.audit_id == audit_id)
            .order_by(RuntimeProfileRun.created_at, RuntimeProfileRun.id)
        )
    )


def measured_run(runs: Sequence[RuntimeProfileRun]) -> RuntimeProfileRun | None:
    """The most recent successful run (its evidence is the one attached to the findings)."""
    succeeded = [r for r in runs if r.status == RuntimeRunStatus.SUCCEEDED and r.finished_at]
    return max(succeeded, key=lambda r: r.finished_at or r.created_at, default=None)


def runtime_overview(session: Session, settings: Settings, audit_id: str) -> RuntimeAuditOut:
    audit = audit_service.get_audit(session, audit_id)
    runtime_summary = (audit.summary or {}).get("runtime") or {}
    status = latest_backend_status(session, settings)
    runs = list_runs(session, audit_id)
    measured = measured_run(runs)
    hotspots: list[HotspotOut] = []
    correlations: list[FindingCorrelation] = []
    runtime_only: list[RuntimeOnlyHotspot] = []
    if measured is not None:
        hotspots = [
            _hotspot_out(h)
            for h in run_hotspots(session, measured.id, user_only=True, limit=OVERVIEW_HOTSPOTS)
        ]
        correlations, runtime_only = run_correlations(session, measured.id)
    commands = session.scalar(
        select(RuntimeCommand.id).where(RuntimeCommand.audit_id == audit_id).limit(1)
    )
    return RuntimeAuditOut(
        audit_id=audit.id,
        mode=AuditMode(audit.mode),
        audit_status=AuditStatus(audit.status),
        stage=AuditStage(audit.stage),
        runtime_requested=audit.mode == AuditMode.RUNTIME,
        runtime_available=status.available,
        unavailable_reason=None if status.available else status.reason,
        backend=status.backend,
        commands_discovered=int(runtime_summary.get("commands_discovered", 0))
        if commands is not None
        else 0,
        runs=[_run_summary(r) for r in runs],
        measured_run_id=measured.id if measured else None,
        hotspots=hotspots,
        correlations=correlations,
        runtime_only_hotspots=runtime_only,
        warnings=list(audit.warnings or []),
    )


def get_run(session: Session, audit_id: str, run_id: str) -> RuntimeProfileRun:
    audit_service.get_audit(session, audit_id)
    run = session.get(RuntimeProfileRun, run_id)
    if run is None or run.audit_id != audit_id:
        raise NotFoundError(f"runtime run {run_id} not found in audit {audit_id}")
    return run


def run_detail(session: Session, audit_id: str, run_id: str) -> RunDetailOut:
    run = get_run(session, audit_id, run_id)
    correlations, runtime_only = run_correlations(session, run.id)
    return RunDetailOut(
        **_run_summary(run).model_dump(),
        limits=run.limits or {},
        trace_memory=run.trace_memory,
        allow_dependency_install=run.allow_dependency_install,
        dependency_install=run.dependency_install,
        total_profiled_time_ms=run.total_profiled_time_ms,
        function_count=run.function_count,
        memory=MemoryOut(
            peak_rss_bytes=run.peak_rss_bytes,
            rss_at_exit_bytes=run.rss_at_exit_bytes,
            tracemalloc_peak_bytes=run.tracemalloc_peak_bytes,
            tracemalloc_retained_bytes=run.tracemalloc_retained_bytes,
        ),
        environment=run.environment or {},
        stdout_tail=run.stdout_tail,
        stderr_tail=run.stderr_tail,
        artifacts={
            "profile_prof": run.prof_artifact_id,
            "profile_stats_json": run.stats_artifact_id,
            "harness_result_json": run.result_artifact_id,
        },
        warnings=list(run.warnings or []),
        hotspots=[
            _hotspot_out(h)
            for h in run_hotspots(session, run.id, user_only=False, limit=DETAIL_HOTSPOTS)
        ],
        correlations=correlations,
        runtime_only_hotspots=runtime_only,
    )
