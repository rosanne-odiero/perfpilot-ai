"""Worker-side helpers shared by repository-candidate validation and benchmarking.

Each run is one fresh container from the approved scope (image, limits, working directory, argv):
the trusted harness and its request are mounted read-only under ``/perfpilot``, the workspace
(original snapshot or patched copy) read-only at ``/workspace``, the snapshot's prepared
dependencies (if any) read-only at ``/deps``, network ``none``. Only JSON and XML written to
``/out`` are read back, and both are treated as untrusted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.core.config import Settings
from backend.core.exceptions import UnavailableError
from backend.core.redaction import redact_text
from backend.models.database import session_scope
from backend.models.orm.runtime import RuntimeApproval
from backend.profiling.runner import HARNESS_PATH
from backend.runtime.backends import observe_backend, record_backend_status
from backend.runtime.execution import (
    HARNESS_TARGET,
    REQUEST_TARGET,
    RESULT_FILE,
    SANDBOX_ENV,
    container_workdir,
    harness_argv,
)
from backend.sandbox.backend import (
    SANDBOX_OUTPUT,
    BackendStatus,
    ExecutionBackend,
    ReadOnlyMount,
    SandboxLimits,
    SandboxRunRequest,
    SandboxRunResult,
)
from backend.sandbox.environment import deps_dir_for

TAIL_CHARS = 8_000


class CommandScope(BaseModel):
    """Exactly what the user approved for this audit (pinned from a ``RuntimeApproval``)."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    argv: list[str]
    workdir_rel: str
    image: str
    limits: SandboxLimits

    @classmethod
    def from_approval(cls, approval: RuntimeApproval) -> CommandScope:
        return cls(
            approval_id=approval.id,
            argv=[str(a) for a in approval.argv],
            workdir_rel=approval.workdir_rel,
            image=approval.image,
            limits=SandboxLimits.model_validate(approval.limits),
        )


class ContainerRun(BaseModel):
    result: SandboxRunResult
    harness: dict[str, Any] | None
    out_dir: Path
    files: list[str]
    request_sha256: str

    def section(self, key: str) -> dict[str, Any]:
        value = self.harness.get(key) if self.harness else None
        return value if isinstance(value, dict) else {}

    @property
    def command_exit_code(self) -> int | None:
        code = self.section("command").get("exit_code")
        return code if isinstance(code, int) and not isinstance(code, bool) else None

    def failure(self) -> str | None:
        """Why the run did not complete normally (``None`` if the command ran and exited 0)."""
        if self.result.oom_killed:
            return "the run exceeded its memory limit (OOM-killed)"
        if self.result.timed_out:
            return "the run exceeded its wall-clock limit and was killed"
        if self.harness is None:
            return f"the harness produced no result (container exit code {self.result.exit_code})"
        if self.harness.get("status") != "ok":
            error = self.section("error")
            return (
                f"the command raised {short(error.get('type', 'an error'), 80)}: "
                f"{short(error.get('message', ''))}"
            )
        if self.command_exit_code != 0:
            return f"the command exited with code {self.command_exit_code}"
        if self.result.exit_code != 0:
            return f"the container exited with code {self.result.exit_code}"
        return None


def short(value: Any, limit: int = 300) -> str:
    return redact_text(str(value)[:limit]).text


def tail(text: str | None) -> str:
    return redact_text(text[-TAIL_CHARS:]).text if text else ""


def require_backend(settings: Settings) -> tuple[ExecutionBackend, BackendStatus]:
    """The worker's sandbox, re-checked now (and recorded for the API). Never a host fallback."""
    backend, status = observe_backend(settings)
    with session_scope() as session:
        record_backend_status(session, settings, status)
    if backend is None or not status.available:
        raise UnavailableError(
            f"runtime mode is disabled: {status.reason or 'sandbox backend unavailable'}"
        )
    return backend, status


def prepared_deps(settings: Settings, snapshot_id: str) -> Path | None:
    deps = deps_dir_for(settings, snapshot_id)
    return deps if deps.is_dir() and not deps.is_symlink() else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def request_document(scope: CommandScope, command: dict[str, Any]) -> str:
    return json.dumps(
        {
            "mode": "command",
            "workspace_root": container_workdir(scope.workdir_rel),
            "output_dir": SANDBOX_OUTPUT,
            "command": command,
        },
        sort_keys=True,
    )


def run_container(
    backend: ExecutionBackend,
    *,
    scope: CommandScope,
    workspace: Path,
    scratch: Path,
    command: dict[str, Any],
    deps_dir: Path | None,
) -> ContainerRun:
    """One fresh container running the harness on ``command`` (identical for every variant)."""
    shutil.rmtree(scratch, ignore_errors=True)
    in_dir, out_dir = scratch / "in", scratch / "out"
    in_dir.mkdir(parents=True)
    out_dir.mkdir()
    document = request_document(scope, command)
    request_file = in_dir / "request.json"
    request_file.write_text(document, encoding="utf-8")
    request = SandboxRunRequest(
        workspace_dir=workspace,
        argv=harness_argv(),
        workdir_rel=scope.workdir_rel,
        output_dir=out_dir,
        env=dict(SANDBOX_ENV),
        limits=scope.limits,
        network="none",
        image=scope.image,
        extra_mounts=[
            ReadOnlyMount(source=HARNESS_PATH, target=HARNESS_TARGET),
            ReadOnlyMount(source=request_file, target=REQUEST_TARGET),
        ],
        deps_dir=deps_dir,
    )
    result = backend.run(request)
    files = sorted(result.output_files)
    harness = _read_json(out_dir / RESULT_FILE) if RESULT_FILE in files else None
    return ContainerRun(
        result=result,
        harness=harness,
        out_dir=out_dir,
        files=files,
        request_sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
    )


def environment_fingerprint(
    backend: ExecutionBackend, status: BackendStatus, scope: CommandScope, run: ContainerRun
) -> dict[str, Any]:
    process = run.section("process")
    return {
        "backend": backend.name,
        "backend_server_version": status.details.get("server_version"),
        "image": scope.image,
        "image_digest": run.result.image_digest,
        "python_version": short(process.get("python_version"), 40) if process else None,
        "python_implementation": short(process.get("python_implementation"), 40)
        if process
        else None,
        "platform": short(process.get("platform"), 120) if process else None,
        "network": "none",
        "workdir": container_workdir(scope.workdir_rel),
        "limits": scope.limits.model_dump(),
        "pythonhashseed": SANDBOX_ENV["PYTHONHASHSEED"],
    }
