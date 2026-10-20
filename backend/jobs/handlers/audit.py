"""``audit.run``: run the audit stage pipeline for an AuditRun and persist its results."""

from __future__ import annotations

import time
from typing import Any

from backend.audit.pipeline import STATIC_STAGES, AuditState, PipelineStage, run_pipeline
from backend.audit.static_audit import StaticAuditResult, summarize
from backend.core.exceptions import NotConfiguredError, ValidationFailedError
from backend.core.logging import get_logger
from backend.jobs.registry import JobCancelledError, JobContext, job_handler
from backend.models.audit_schemas import RepositoryInventory
from backend.models.database import session_scope
from backend.models.enums import AuditMode, AuditStage, AuditStatus
from backend.models.orm.audit import AuditRun
from backend.runtime.discovery import RUNTIME_DISCOVERY_STAGE, finish_runtime_discovery
from backend.services import audits as audit_service

log = get_logger(__name__)

# Ordered stages for a STATIC audit. Later phases extend this tuple (AI_ANALYSIS after
# PRIORITIZING; runtime stages for RUNTIME mode) without changing the handler.
AUDIT_STAGES: dict[AuditMode, tuple[PipelineStage, ...]] = {AuditMode.STATIC: STATIC_STAGES}
# RUNTIME = the static stages plus static command discovery; execution waits for an approval.
AUDIT_STAGES[AuditMode.RUNTIME] = (*AUDIT_STAGES[AuditMode.STATIC], RUNTIME_DISCOVERY_STAGE)


def _update_audit(audit_id: str, **fields: Any) -> None:
    with session_scope() as session:
        audit = session.get(AuditRun, audit_id)
        if audit is not None:
            for key, value in fields.items():
                setattr(audit, key, value)


def _load(ctx: JobContext, audit_id: str) -> tuple[AuditState, AuditMode, list[str]]:
    with session_scope() as session:
        audit = audit_service.get_audit(session, audit_id)
        snapshot = audit_service.get_snapshot(session, audit.snapshot_id)
        workspace_path, inventory_json = audit_service.ensure_snapshot_ready(snapshot)
        mode = AuditMode(audit.mode)
        if mode not in AUDIT_STAGES:
            raise NotConfiguredError(audit_service.RUNTIME_NOT_CONFIGURED)
        root = audit_service.resolve_workspace(ctx.settings, workspace_path)
        inventory = RepositoryInventory.model_validate(inventory_json)
        warnings = list(audit.warnings or [])
        audit.status = AuditStatus.RUNNING
        audit.error = None
        audit.job_id = ctx.job_id
    state = AuditState(
        root=root,
        inventory=inventory,
        settings=ctx.settings,
        progress=lambda stage, fraction, message: ctx.progress(stage.value, fraction, message),
    )
    return state, mode, warnings


def _persist(audit_id: str, result: StaticAuditResult, warnings: list[str]) -> AuditStatus:
    status = AuditStatus.PARTIAL if result.is_partial else AuditStatus.SUCCEEDED
    partial_reasons = [*result.limits_hit]
    if result.stats.parse_errors:
        partial_reasons.append(f"{result.stats.parse_errors} file(s) could not be parsed")
    if result.stats.rule_errors:
        partial_reasons.append(f"rules skipped for {result.stats.rule_errors} file(s)")
    summary = {
        **result.stats.model_dump(mode="json"),
        "provenance": "STATIC",
        "limits_hit": result.limits_hit,
        "partial": result.is_partial,
        "partial_reasons": partial_reasons,
    }
    with session_scope() as session:
        audit = audit_service.get_audit(session, audit_id)
        audit_service.replace_findings(session, audit_id, result.findings)
        audit.code_index = result.index.model_dump(mode="json")
        audit.summary = summary
        audit.warnings = [*warnings, *result.warnings]
        audit.status = status
        audit.stage = AuditStage.COMPLETED
    return status


@job_handler(audit_service.AUDIT_JOB_TYPE)
def run_audit(ctx: JobContext) -> dict[str, Any]:
    audit_id = ctx.payload.get("audit_id")
    if not isinstance(audit_id, str) or not audit_id:
        raise ValidationFailedError("payload must contain a string 'audit_id'")
    try:
        state, mode, warnings = _load(ctx, audit_id)
        started = time.perf_counter()
        run_pipeline(
            state,
            AUDIT_STAGES[mode],
            on_stage=lambda stage: _update_audit(audit_id, stage=stage.stage),
        )
        result = summarize(state, time.perf_counter() - started)
        status = _persist(audit_id, result, warnings)
        if mode is AuditMode.RUNTIME:
            finish_runtime_discovery(audit_id, state)
    except JobCancelledError:
        _update_audit(audit_id, status=AuditStatus.FAILED, error="cancelled by user")
        raise
    except Exception as exc:
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        _update_audit(audit_id, status=AuditStatus.FAILED, error=message)
        raise
    ctx.progress(AuditStage.COMPLETED.value, 1.0, f"{len(result.findings)} findings")
    log.info(
        "audit completed",
        extra={"audit_id": audit_id, "status": status.value, "findings": len(result.findings)},
    )
    return {
        "audit_id": audit_id,
        "status": status.value,
        "findings": len(result.findings),
        "files_analyzed": result.stats.files_analyzed,
        "parse_errors": result.stats.parse_errors,
        "duration_seconds": result.stats.duration_seconds,
    }
