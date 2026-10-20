"""Worker-side execution of one approved runtime profile run (``runtime.profile`` job).

Repository code runs only through the sandbox :class:`ExecutionBackend`. The trusted profiling
harness (``backend/profiling/harness_entry.py``) and its request JSON are mounted read-only under
``/perfpilot``; the harness writes its result, the raw ``.prof`` and a JSON pstats table to
``/out``. The worker reads only JSON from ``/out`` and stores the raw files as artifacts.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, PerfPilotError, UnavailableError
from backend.core.logging import get_logger
from backend.core.redaction import redact_text
from backend.infrastructure.artifact_store import ArtifactStore
from backend.models.database import session_scope, utcnow
from backend.models.enums import AuditStage
from backend.models.orm.audit import AuditRun
from backend.models.orm.runtime import RuntimeApproval, RuntimeApprovalStatus
from backend.models.orm.runtime_profiles import (
    RuntimeHotspotRecord,
    RuntimeProfileRun,
    RuntimeRunStatus,
)
from backend.profiling.runner import HARNESS_PATH, INTERPRETER_FLAGS
from backend.runtime.backends import observe_backend, record_backend_status
from backend.runtime.discovery import unavailable_warning
from backend.runtime.profile_stats import measure_profile
from backend.runtime.service import apply_correlation
from backend.sandbox.approvals import (
    ApprovedExecution,
    consume_approval,
    consume_dependency_install,
    revoke_approval,
)
from backend.sandbox.backend import (
    SANDBOX_EXTRA_ROOT,
    SANDBOX_OUTPUT,
    SANDBOX_WORKSPACE,
    BackendStatus,
    ExecutionBackend,
    ReadOnlyMount,
    SandboxRunRequest,
    SandboxRunResult,
)
from backend.sandbox.environment import (
    RequirementsSource,
    deps_dir_for,
    prepare_environment,
)
from backend.services import audits as audit_service

log = get_logger(__name__)

HARNESS_TARGET = f"{SANDBOX_EXTRA_ROOT}/harness_entry.py"
REQUEST_TARGET = f"{SANDBOX_EXTRA_ROOT}/request.json"
RESULT_FILE = "result.json"
PROF_FILE = "profile.prof"
STATS_FILE = "profile_stats.json"
TAIL_CHARS = 8000
MAX_PYPROJECT_BYTES = 512 * 1024
TEST_EXTRAS = ("test", "tests", "testing", "dev")
SANDBOX_ENV = {"PYTHONHASHSEED": "0"}

CancelCheck = Callable[[], None]


class RunFailed(Exception):
    """The run ended without measurements. ``status`` and ``reason`` are recorded on the run."""

    def __init__(self, status: RuntimeRunStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


# ----------------------------------------------------------------------------- argv mapping


def harness_command(argv: list[str], *, trace_memory: bool) -> dict[str, Any]:
    """Map an approved argv (one of the three allowlisted shapes) to a harness command."""
    common = {"trace_memory": trace_memory, "stats_json": True}
    if len(argv) >= 3 and argv[:3] == ["python", "-m", "pytest"]:
        target: str | None = None
        args: list[str] = []
        rest = iter(argv[3:])
        for item in rest:
            if item == "-k":
                args += [item, next(rest, "")]
            elif item.startswith("-") or target is not None:
                args.append(item)
            else:
                target = item
        return {"kind": "pytest", "target": target or ".", "args": args, **common}
    if len(argv) == 3 and argv[:2] == ["python", "-m"]:
        return {"kind": "module", "target": argv[2], "args": [], **common}
    if len(argv) == 2 and argv[0] == "python" and not argv[1].startswith("-"):
        return {"kind": "script", "target": argv[1], "args": [], **common}
    raise ConflictError("approved argv does not have an executable shape")


def container_workdir(workdir_rel: str) -> str:
    return SANDBOX_WORKSPACE if workdir_rel == "." else f"{SANDBOX_WORKSPACE}/{workdir_rel}"


def harness_argv() -> list[str]:
    return [
        "python",
        *INTERPRETER_FLAGS,
        HARNESS_TARGET,
        REQUEST_TARGET,
        f"{SANDBOX_OUTPUT}/{RESULT_FILE}",
    ]


# ----------------------------------------------------------------------------- dependencies


def choose_requirements_source(workspace: Path, workdir_rel: str) -> RequirementsSource | None:
    """Pick the dependency manifest for a command: requirements.txt, else PEP 621 pyproject."""
    for directory in dict.fromkeys([workdir_rel, "."]):
        base = workspace if directory == "." else workspace / directory
        requirements = base / "requirements.txt"
        if requirements.is_file() and not requirements.is_symlink():
            rel = requirements.relative_to(workspace).as_posix()
            return RequirementsSource(kind="requirements_file", path_rel=rel)
        pyproject = base / "pyproject.toml"
        if (
            pyproject.is_file()
            and not pyproject.is_symlink()
            and pyproject.stat().st_size <= MAX_PYPROJECT_BYTES
        ):
            try:
                project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(project, dict):
                continue
            optional = project.get("optional-dependencies")
            extras = [e for e in TEST_EXTRAS if isinstance(optional, dict) and e in optional]
            if project.get("dependencies") or extras:
                rel = pyproject.relative_to(workspace).as_posix()
                return RequirementsSource(kind="pyproject", path_rel=rel, extras=extras)
    return None


# ----------------------------------------------------------------------------- helpers


def _tail(text: str | None) -> str | None:
    if not text:
        return None
    return redact_text(text[-TAIL_CHARS:]).text


def _update_run(run_id: str, **fields: Any) -> None:
    with session_scope() as session:
        run = session.get(RuntimeProfileRun, run_id)
        if run is not None:
            for key, value in fields.items():
                setattr(run, key, value)


def _set_audit_stage(audit_id: str, stage: AuditStage) -> None:
    with session_scope() as session:
        audit = session.get(AuditRun, audit_id)
        if audit is not None:
            audit.stage = stage


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _section(data: dict[str, Any] | None, key: str) -> dict[str, Any]:
    value = data.get(key) if data else None
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ns_to_ms(value: Any) -> float | None:
    number = _int_or_none(value)
    return number / 1e6 if number is not None else None


def _short(value: Any, limit: int = 300) -> str:
    return redact_text(str(value)[:limit]).text


def outcome_of(
    result: SandboxRunResult, harness: dict[str, Any] | None
) -> tuple[RuntimeRunStatus, str | None]:
    """Classify a sandbox result honestly. Only SUCCEEDED runs produce hotspots."""
    if result.oom_killed:
        return RuntimeRunStatus.OOM_KILLED, "the run exceeded its memory limit (OOM-killed)"
    if result.timed_out:
        return RuntimeRunStatus.TIMED_OUT, "the run exceeded its wall-clock limit and was killed"
    if harness is None:
        return (
            RuntimeRunStatus.FAILED,
            f"the profiling harness produced no result (container exit code {result.exit_code})",
        )
    if harness.get("status") != "ok":
        error = _section(harness, "error")
        return (
            RuntimeRunStatus.FAILED,
            f"the command raised {_short(error.get('type', 'an error'), 80)}: "
            f"{_short(error.get('message', ''))}",
        )
    command = _section(harness, "command")
    code = command.get("exit_code")
    if code != 0:
        kind = command.get("kind")
        detail = " (test failures or errors)" if kind == "pytest" and code == 1 else ""
        return RuntimeRunStatus.FAILED, f"the command exited with code {_short(code, 20)}{detail}"
    if result.exit_code != 0:
        return RuntimeRunStatus.FAILED, f"the container exited with code {result.exit_code}"
    return RuntimeRunStatus.SUCCEEDED, None


def _fingerprint(
    backend: ExecutionBackend,
    status: BackendStatus,
    execution: ApprovedExecution,
    result: SandboxRunResult,
    harness: dict[str, Any] | None,
    *,
    trace_memory: bool,
    deps: bool,
) -> dict[str, Any]:
    process = _section(harness, "process")
    return {
        "backend": backend.name,
        "backend_server_version": status.details.get("server_version"),
        "image": execution.image,
        "image_digest": result.image_digest,
        "python_version": _short(process.get("python_version"), 40) if process else None,
        "python_implementation": _short(process.get("python_implementation"), 40)
        if process
        else None,
        "platform": _short(process.get("platform"), 120) if process else None,
        "network": "none",
        "workdir": container_workdir(execution.workdir_rel),
        "limits": execution.limits.model_dump(),
        "profiler": "cProfile" + (" + tracemalloc (concurrent)" if trace_memory else ""),
        "dependencies": "prepared /deps (read-only)" if deps else "image only",
        "pythonhashseed": SANDBOX_ENV["PYTHONHASHSEED"],
    }


# ----------------------------------------------------------------------------- the job


def _check_backend(
    settings: Settings, run: RuntimeProfileRun
) -> tuple[ExecutionBackend, BackendStatus]:
    backend, status = observe_backend(settings)
    with session_scope() as session:
        record_backend_status(session, settings, status)
    if backend is None or not status.available:
        reason = status.reason or "sandbox backend unavailable"
        with session_scope() as session:
            revoke_approval(session, run.approval_id, reason=f"sandbox unavailable: {reason}")
            row = session.get(RuntimeProfileRun, run.id)
            if row is not None:
                row.status = RuntimeRunStatus.UNAVAILABLE
                row.failure_reason = reason
                row.finished_at = utcnow()
            audit = session.get(AuditRun, run.audit_id)
            if audit is not None:
                audit.warnings = [*(audit.warnings or []), unavailable_warning(reason)]
        raise UnavailableError(f"runtime mode is disabled: {reason}")
    return backend, status


def _prepare_dependencies(
    settings: Settings, backend: ExecutionBackend, run: RuntimeProfileRun, workspace: Path
) -> None:
    _update_run(run.id, status=RuntimeRunStatus.PREPARING_DEPENDENCIES)
    with session_scope() as session:
        grant = consume_dependency_install(session, run.approval_id)
    source = choose_requirements_source(workspace, run.workdir_rel)
    if source is None:
        raise RunFailed(
            RuntimeRunStatus.FAILED,
            "dependency installation was approved but no requirements.txt or PEP 621 "
            "pyproject dependencies were found",
        )
    try:
        prepared = prepare_environment(
            backend, grant, workspace_dir=workspace, source=source, settings=settings
        )
    except PerfPilotError as exc:
        raise RunFailed(
            RuntimeRunStatus.FAILED, f"dependency preparation failed: {exc.message}"
        ) from exc
    summary = {
        "source": source.model_dump(),
        "succeeded": prepared.succeeded,
        "requirements": len(prepared.manifest.requirements),
        "skipped": [s.model_dump() for s in prepared.manifest.skipped],
        "exit_code": prepared.result.exit_code,
        "timed_out": prepared.result.timed_out,
        "image_digest": prepared.result.image_digest,
        "stderr_tail": _tail(prepared.result.stderr),
    }
    _update_run(run.id, dependency_install=summary)
    if not prepared.succeeded:
        raise RunFailed(RuntimeRunStatus.FAILED, "dependency installation failed in the sandbox")


def _store_artifacts(
    settings: Settings, run_id: str, out_dir: Path, files: list[str]
) -> dict[str, str | None]:
    store = ArtifactStore(settings)
    ids: dict[str, str | None] = {"prof": None, "stats": None, "result": None}
    plan = {
        "prof": (PROF_FILE, "application/octet-stream"),
        "stats": (STATS_FILE, "application/json"),
        "result": (RESULT_FILE, "application/json"),
    }
    with session_scope() as session:
        for key, (name, media_type) in plan.items():
            if name not in files:
                continue
            artifact = store.save_file(
                session,
                kind="runtime-profile",
                source=out_dir / name,
                filename=f"{run_id}-{name}",
                media_type=media_type,
                owner_type="runtime_run",
                owner_id=run_id,
                meta={"untrusted": True, "note": "written inside the sandbox by profiled code"},
            )
            ids[key] = artifact.id
    return ids


def _persist_hotspots(run_id: str, out_dir: Path, workspace: Path) -> tuple[int, float, int]:
    profile = measure_profile(out_dir / STATS_FILE, workspace)
    with session_scope() as session:
        session.add_all(
            RuntimeHotspotRecord(
                run_id=run_id,
                rank=h.rank,
                function_name=h.function_name[:512],
                file_path=h.file_path[:1024],
                line_number=h.line_number,
                location_kind=h.location_kind.value,
                is_user_code=h.is_user_code,
                primitive_calls=h.primitive_calls,
                total_calls=h.total_calls,
                internal_time_ms=h.internal_time_ms,
                cumulative_time_ms=h.cumulative_time_ms,
                attributed_time_ms=h.attributed_time_ms,
                percentage_of_profiled_time=h.percentage_of_profiled_time,
                cumulative_percentage=h.cumulative_percentage,
                attributed_percentage=h.attributed_percentage,
            )
            for h in profile.hotspots
        )
    return len(profile.hotspots), profile.total_profiled_time_ms, profile.function_count


def execute_profile_run(
    settings: Settings, run_id: str, check_cancelled: CancelCheck
) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(RuntimeProfileRun, run_id)
        if run is None:
            raise ConflictError(f"runtime run {run_id} not found")
        if run.status != RuntimeRunStatus.QUEUED:
            raise ConflictError(f"runtime run {run_id} is {run.status}, not QUEUED")
        snapshot = audit_service.get_snapshot(session, run.snapshot_id)
        workspace = audit_service.resolve_workspace(settings, snapshot.workspace_path or "")
        approval = session.get(RuntimeApproval, run.approval_id)
        if approval is None:
            raise ConflictError("the run's approval no longer exists")
        session.expunge(run)

    backend, status = _check_backend(settings, run)
    _set_audit_stage(run.audit_id, AuditStage.RUNTIME_PROFILING)
    _update_run(run_id, started_at=utcnow())
    base = settings.artifacts_dir / "runtime-runs" / run_id
    try:
        if run.allow_dependency_install:
            _prepare_dependencies(settings, backend, run, workspace)
        check_cancelled()
        with session_scope() as session:
            execution = consume_approval(session, run.approval_id)
        deps = deps_dir_for(settings, run.snapshot_id)
        deps_dir = deps if deps.is_dir() and not deps.is_symlink() else None
        _update_run(run_id, status=RuntimeRunStatus.RUNNING)
        return _execute(settings, backend, status, run, execution, workspace, deps_dir, base)
    except RunFailed as failure:
        _finish_failed(run, failure.status, failure.reason)
        return {"run_id": run_id, "status": failure.status.value, "reason": failure.reason}
    except BaseException as exc:
        reason = exc.message if isinstance(exc, PerfPilotError) else f"{type(exc).__name__}: {exc}"
        _finish_failed(run, RuntimeRunStatus.FAILED, _short(reason, 1000))
        raise
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _finish_failed(run: RuntimeProfileRun, status: RuntimeRunStatus, reason: str) -> None:
    with session_scope() as session:
        row = session.get(RuntimeProfileRun, run.id)
        if row is not None and row.status not in (RuntimeRunStatus.UNAVAILABLE,):
            row.status = status
            row.failure_reason = reason
            row.finished_at = utcnow()
        approval = session.get(RuntimeApproval, run.approval_id)
        if approval is not None and approval.status == RuntimeApprovalStatus.APPROVED:
            with contextlib.suppress(PerfPilotError):
                revoke_approval(session, approval.id, reason=f"run failed: {reason}"[:500])
        audit = session.get(AuditRun, run.audit_id)
        if audit is not None:
            measured = session.scalar(
                select(RuntimeProfileRun.id).where(
                    RuntimeProfileRun.audit_id == run.audit_id,
                    RuntimeProfileRun.status == RuntimeRunStatus.SUCCEEDED,
                )
            )
            audit.stage = AuditStage.COMPLETED if measured else AuditStage.AWAITING_RUNTIME_APPROVAL
            audit.warnings = [
                *(audit.warnings or []),
                f"Runtime run {run.id} ({' '.join(str(a) for a in run.argv)}) produced no "
                f"measurements: {reason}",
            ]


def _execute(
    settings: Settings,
    backend: ExecutionBackend,
    status: BackendStatus,
    run: RuntimeProfileRun,
    execution: ApprovedExecution,
    workspace: Path,
    deps_dir: Path | None,
    base: Path,
) -> dict[str, Any]:
    in_dir, out_dir = base / "in", base / "out"
    shutil.rmtree(base, ignore_errors=True)
    in_dir.mkdir(parents=True)
    out_dir.mkdir()
    request_file = in_dir / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "mode": "command",
                "workspace_root": container_workdir(execution.workdir_rel),
                "output_dir": SANDBOX_OUTPUT,
                "command": harness_command(execution.argv, trace_memory=run.trace_memory),
            }
        ),
        encoding="utf-8",
    )
    request = SandboxRunRequest(
        workspace_dir=workspace,
        argv=harness_argv(),
        workdir_rel=execution.workdir_rel,
        output_dir=out_dir,
        env=dict(SANDBOX_ENV),
        limits=execution.limits,
        network="none",
        image=execution.image,
        extra_mounts=[
            ReadOnlyMount(source=HARNESS_PATH, target=HARNESS_TARGET),
            ReadOnlyMount(source=request_file, target=REQUEST_TARGET),
        ],
        deps_dir=deps_dir,
    )
    result = backend.run(request)
    files = set(result.output_files)
    harness = _read_json(out_dir / RESULT_FILE) if RESULT_FILE in files else None
    outcome, reason = outcome_of(result, harness)
    artifacts = _store_artifacts(settings, run.id, out_dir, sorted(files))
    profile = _section(harness, "profile")
    memory = _section(harness, "memory")
    process = _section(harness, "process")
    command = _section(harness, "command")
    warnings: list[str] = []
    if result.rejected_outputs:
        warnings.append(f"{len(result.rejected_outputs)} unexpected output entries were discarded")
    if result.stdout_truncated or result.stderr_truncated:
        warnings.append("command output exceeded the capture limit and was truncated")

    hotspots = 0
    total_ms: float | None = None
    function_count: int | None = None
    if outcome is RuntimeRunStatus.SUCCEEDED:
        if STATS_FILE not in files:
            outcome = RuntimeRunStatus.FAILED
            reason = "the run produced no profile statistics (missing or over the output size cap)"
        else:
            try:
                hotspots, total_ms, function_count = _persist_hotspots(run.id, out_dir, workspace)
            except PerfPilotError as exc:
                outcome, reason = (
                    RuntimeRunStatus.FAILED,
                    f"invalid profile statistics: {exc.message}",
                )

    fields: dict[str, Any] = {
        "exit_code": result.exit_code,
        "command_exit_code": _int_or_none(command.get("exit_code")),
        "timed_out": result.timed_out,
        "oom_killed": result.oom_killed,
        "duration_s": result.duration_s,
        "environment": _fingerprint(
            backend,
            status,
            execution,
            result,
            harness,
            trace_memory=run.trace_memory,
            deps=deps_dir is not None,
        ),
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
        "prof_artifact_id": artifacts["prof"],
        "stats_artifact_id": artifacts["stats"],
        "result_artifact_id": artifacts["result"],
        "warnings": warnings,
    }
    if outcome is not RuntimeRunStatus.SUCCEEDED:
        _update_run(run.id, **fields)
        raise RunFailed(outcome, reason or "the run failed")

    fields.update(
        wall_time_ms=_ns_to_ms(profile.get("wall_ns_under_profiler")),
        cpu_time_ms=_ns_to_ms(profile.get("cpu_ns_under_profiler")),
        total_profiled_time_ms=total_ms,
        function_count=function_count,
        peak_rss_bytes=_int_or_none(process.get("ru_maxrss_bytes")),
        rss_at_exit_bytes=_int_or_none(process.get("rss_at_exit_bytes")),
        tracemalloc_peak_bytes=_int_or_none(memory.get("tracemalloc_peak_bytes")),
        tracemalloc_retained_bytes=_int_or_none(memory.get("tracemalloc_retained_bytes")),
    )
    _update_run(run.id, **fields)
    _set_audit_stage(run.audit_id, AuditStage.CORRELATING)
    with session_scope() as session:
        result_corr = apply_correlation(session, run.audit_id, run.id)
        row = session.get(RuntimeProfileRun, run.id)
        if row is not None:
            row.status = RuntimeRunStatus.SUCCEEDED
            row.finished_at = utcnow()
        audit = session.get(AuditRun, run.audit_id)
        if audit is not None:
            audit.stage = AuditStage.COMPLETED
            runtime = dict((audit.summary or {}).get("runtime") or {})
            runtime.update(
                state="measured",
                measured_run_id=run.id,
                correlated_findings=len(result_corr.correlations),
                runtime_only_hotspots=len(result_corr.runtime_only),
            )
            audit.summary = {**(audit.summary or {}), "runtime": runtime}
    log.info(
        "runtime run measured",
        extra={"run_id": run.id, "audit_id": run.audit_id, "hotspots": hotspots},
    )
    return {
        "run_id": run.id,
        "status": RuntimeRunStatus.SUCCEEDED.value,
        "hotspots": hotspots,
        "correlated_findings": len(result_corr.correlations),
        "runtime_only_hotspots": len(result_corr.runtime_only),
    }
