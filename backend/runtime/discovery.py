"""RUNTIME audit stages: static command discovery, then wait for an explicit approval.

``runtime_discovery_stage`` runs in the worker after the static stages. It never executes
repository code: discovery only reads files. It also records the sandbox availability the worker
observed, so the API can report it without touching Docker. ``finish_runtime_discovery`` persists
the proposals and parks the audit at ``AWAITING_RUNTIME_APPROVAL``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.audit.pipeline import AuditState, PipelineStage
from backend.core.exceptions import PerfPilotError
from backend.core.logging import get_logger
from backend.models.database import session_scope
from backend.models.enums import AuditStage
from backend.models.orm.audit import AuditRun
from backend.runtime.backends import observe_backend, record_backend_status
from backend.sandbox.approvals import record_discovered_commands
from backend.sandbox.backend import BackendStatus
from backend.sandbox.command_discovery import DiscoveryReport, discover_commands

log = get_logger(__name__)

STAGE_OUTPUT_KEY = "runtime_discovery"


class RuntimeDiscoveryOutput(BaseModel):
    report: DiscoveryReport | None
    error: str | None = None
    backend: BackendStatus


def runtime_discovery_stage(state: AuditState) -> None:
    state.report(0.1, "checking the sandbox backend")
    _, status = observe_backend(state.settings)
    with session_scope() as session:
        record_backend_status(session, state.settings, status)
    state.report(0.3, "discovering test and benchmark commands (static, nothing is executed)")
    try:
        report: DiscoveryReport | None = discover_commands(state.root, state.inventory)
        error = None
    except PerfPilotError as exc:
        report, error = None, exc.message
        state.warnings.append(f"Runtime command discovery failed: {exc.message}")
    state.stage_outputs[STAGE_OUTPUT_KEY] = RuntimeDiscoveryOutput(
        report=report, error=error, backend=status
    )
    found = len(report.commands) if report else 0
    state.report(1.0, f"{found} candidate command(s) proposed")


RUNTIME_DISCOVERY_STAGE = PipelineStage(
    AuditStage.RUNTIME_DISCOVERY,
    runtime_discovery_stage,
    "Discovering runnable test/benchmark commands (static)",
)


def unavailable_warning(reason: str | None) -> str:
    return (
        f"Runtime profiling is unavailable: {reason or 'unknown reason'}. The static audit is "
        "complete; discovered commands can be approved once the sandbox is available. Repository "
        "code is never run outside the sandbox."
    )


def finish_runtime_discovery(audit_id: str, state: AuditState) -> dict[str, Any]:
    """Record proposals and park the audit at AWAITING_RUNTIME_APPROVAL. Returns the summary."""
    output = state.stage_outputs.get(STAGE_OUTPUT_KEY)
    if not isinstance(output, RuntimeDiscoveryOutput):
        raise RuntimeError("the RUNTIME_DISCOVERY stage must run before finishing discovery")
    commands = output.report.commands if output.report else []
    with session_scope() as session:
        audit = session.get(AuditRun, audit_id)
        if audit is None:
            raise RuntimeError(f"audit {audit_id} vanished")
        record_discovered_commands(session, audit_id, audit.snapshot_id, commands)
        summary: dict[str, Any] = {
            "state": "awaiting_approval",
            "runtime_available": output.backend.available,
            "unavailable_reason": None if output.backend.available else output.backend.reason,
            "backend": output.backend.backend,
            "commands_discovered": len(commands),
            "runnable_commands": sum(1 for c in commands if c.runnable),
            "files_scanned": output.report.files_scanned if output.report else 0,
            "discovery_limits_hit": output.report.limits_hit if output.report else [],
            "discovery_error": output.error,
            "provenance": "STATIC",
        }
        warnings = list(audit.warnings or [])
        if not output.backend.available:
            warnings.append(unavailable_warning(output.backend.reason))
        if output.error and not any("command discovery failed" in str(w) for w in warnings):
            warnings.append(f"Runtime command discovery failed: {output.error}")
        audit.warnings = warnings
        audit.summary = {**(audit.summary or {}), "runtime": summary}
        audit.stage = AuditStage.AWAITING_RUNTIME_APPROVAL
    log.info(
        "runtime discovery finished",
        extra={
            "audit_id": audit_id,
            "commands": len(commands),
            "runtime_available": output.backend.available,
        },
    )
    return summary
