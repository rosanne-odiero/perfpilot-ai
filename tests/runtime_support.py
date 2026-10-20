"""Fixtures for Runtime Audit tests.

TEST DOUBLES (never product features):

* ``SubprocessHarnessBackend`` stands in for the Docker sandbox at the ``ExecutionBackend``
  boundary. It translates the container paths in a ``SandboxRunRequest`` (``/workspace``,
  ``/out``, ``/perfpilot/...``) to their host sources and runs the *real* profiling harness on the
  trusted ShopFast fixture in a host subprocess, so tests see real cProfile/tracemalloc output. It
  provides **no isolation** and is only ever pointed at bundled fixture code.
* ``ScriptedBackend`` returns a fixed ``SandboxRunResult`` (e.g. OOM-killed, timed out) to test how
  outcomes are recorded; it runs nothing.
* ``UnavailableBackend`` reports the sandbox as unavailable.

They are injected with ``backend.runtime.backends.set_backend_factory`` (a worker-side test seam).
``@pytest.mark.docker`` tests in this directory use the real Docker backend and are skipped with
the reason when Docker or the sandbox image is unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit_support import SHOPFAST, copy_into_workspace, create_snapshot, scan_inventory
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.jobs.worker import Worker
from backend.sandbox.backend import (
    SANDBOX_OUTPUT,
    SANDBOX_WORKSPACE,
    BackendStatus,
    DependencyInstallRequest,
    ExecutionBackend,
    SandboxRunRequest,
    SandboxRunResult,
)
from backend.sandbox.docker_backend import collect_output_files

SANDBOX_IMAGE = "perfpilot-sandbox:latest"
BackendSetter = Callable[[ExecutionBackend], ExecutionBackend]


# ----------------------------------------------------------------------------- test doubles


@dataclass
class SubprocessHarnessBackend:
    """TEST DOUBLE of the sandbox: runs the real harness in a host subprocess (no isolation)."""

    scratch: Path
    name: str = "test-double-subprocess"
    runs: list[SandboxRunRequest] = field(default_factory=list)
    installs: list[DependencyInstallRequest] = field(default_factory=list)
    harness_requests: list[dict[str, Any]] = field(default_factory=list)

    def availability(self) -> BackendStatus:
        return BackendStatus(
            available=True,
            backend=self.name,
            details={
                "server_version": "test-double",
                "docker_path": "/host/only/path/docker",
                "image": SANDBOX_IMAGE,
            },
        )

    def _host_path(self, value: str, request: SandboxRunRequest) -> str:
        mounts = {m.target: m.source for m in request.extra_mounts}
        if value in mounts:
            return str(mounts[value])
        if value == SANDBOX_WORKSPACE or value.startswith(SANDBOX_WORKSPACE + "/"):
            return str(request.workspace_dir) + value[len(SANDBOX_WORKSPACE) :]
        if value == SANDBOX_OUTPUT or value.startswith(SANDBOX_OUTPUT + "/"):
            return str(request.output_dir) + value[len(SANDBOX_OUTPUT) :]
        return value

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.runs.append(request)
        self.scratch.mkdir(parents=True, exist_ok=True)
        harness_request = json.loads(
            Path(self._host_path("/perfpilot/request.json", request)).read_text()
        )
        self.harness_requests.append(json.loads(json.dumps(harness_request)))
        for key in ("workspace_root", "output_dir"):
            harness_request[key] = self._host_path(harness_request[key], request)
        translated = self.scratch / f"request-{len(self.runs)}.json"
        translated.write_text(json.dumps(harness_request))
        argv = [self._host_path(a, request) for a in request.argv]
        assert argv[0] == "python"
        argv[0] = sys.executable
        argv[argv.index(self._host_path("/perfpilot/request.json", request))] = str(translated)
        env = {"PATH": os.defpath, "HOME": str(self.scratch), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(request.env)
        if request.deps_dir is not None:
            env["PYTHONPATH"] = str(request.deps_dir)
        workdir = (
            request.workspace_dir
            if request.workdir_rel == "."
            else request.workspace_dir / request.workdir_rel
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=request.limits.timeout_seconds,
                check=False,
            )
            exit_code: int | None = proc.returncode
            stdout, stderr, timed_out = proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired:
            exit_code, stdout, stderr, timed_out = None, "", "", True
        files, rejected = collect_output_files(request.output_dir, request.limits.output_max_bytes)
        # Output is returned unredacted so tests can check the worker's own redaction.
        return SandboxRunResult(
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=False,
            duration_s=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            output_files=files,
            rejected_outputs=rejected,
        )

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult:
        """Records the request and creates an (empty) package dir; installs nothing."""
        self.installs.append(request)
        (request.target_dir / "installed-by-test-double").mkdir()
        return SandboxRunResult(
            exit_code=0, timed_out=False, oom_killed=False, duration_s=0.0, stdout="", stderr=""
        )


@dataclass
class ScriptedBackend:
    """TEST DOUBLE: returns a fixed result for every run; executes nothing."""

    result: SandboxRunResult
    name: str = "test-double-scripted"
    runs: list[SandboxRunRequest] = field(default_factory=list)

    def availability(self) -> BackendStatus:
        return BackendStatus(available=True, backend=self.name)

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.runs.append(request)
        return self.result

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult:
        raise AssertionError("not used")


@dataclass
class UnavailableBackend:
    """TEST DOUBLE: a sandbox that cannot run containers."""

    reason: str = "docker CLI not found (test double)"
    name: str = "test-double-unavailable"
    runs: list[SandboxRunRequest] = field(default_factory=list)

    def availability(self) -> BackendStatus:
        return BackendStatus(available=False, backend=self.name, reason=self.reason)

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        raise AssertionError("an unavailable backend must never be asked to run anything")

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult:
        raise AssertionError("an unavailable backend must never be asked to install anything")


# ----------------------------------------------------------------------------- audit helpers


def drain(settings: Settings) -> None:
    Worker(settings, isolate=False).drain()


def shopfast_snapshot(
    settings: Settings, name: str = "shopfast", extra_files: dict[str, str] | None = None
) -> str:
    workspace = copy_into_workspace(settings, SHOPFAST, name)
    for rel, text in (extra_files or {}).items():
        (workspace / rel).write_text(text)
    return create_snapshot(workspace, scan_inventory(workspace))


def start_runtime_audit(client: TestClient, settings: Settings, snapshot_id: str) -> str:
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id, "mode": "RUNTIME"})
    assert response.status_code == 202, response.text
    drain(settings)
    return str(response.json()["subject_id"])


def command_by_argv(client: TestClient, audit_id: str, argv: list[str]) -> dict[str, Any]:
    commands = client.get(f"/api/audits/{audit_id}/runtime/commands").json()
    return next(c for c in commands if c["argv"] == argv)


def approve(
    client: TestClient, audit_id: str, command: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    body = {"command_id": command["id"], "argv": command["argv"], "approved_by": "tester", **extra}
    response = client.post(f"/api/audits/{audit_id}/runtime/approvals", json=body)
    assert response.status_code == 202, response.text
    accepted: dict[str, Any] = response.json()
    return accepted


WORKLOAD_ARGV = ["python", "benchmarks/run_workload.py"]
