"""Run the workload harness in a constrained child process.

Hardening applied to every run:

* fixed interpreter (``sys.executable``) with ``-P -s -B`` (no script dir or user site on
  ``sys.path``, no bytecode written into the workspace) and a validated argv list, never a shell;
* a fresh working directory, ``HOME`` and ``TMPDIR`` under the run directory;
* a scrubbed environment: only a fixed minimal set plus the workload's permitted variables;
* POSIX ``setrlimit`` limits applied in the child before ``exec`` (CPU seconds, file size, open
  files, and address space where the kernel enforces it). Unsupported limits are reported;
* a wall-clock timeout and an RSS ceiling enforced from the parent by killing the process group;
* stdout/stderr redirected to files and truncated when read back.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, Field

from backend.core.exceptions import SecurityViolationError
from backend.core.redaction import redact_text

HARNESS_PATH = Path(__file__).resolve().with_name("harness_entry.py")
INTERPRETER_FLAGS = ("-P", "-s", "-B")
MAX_CAPTURED_STREAM_BYTES = 64 * 1024
_BASE_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]


class ResourceLimits(BaseModel):
    timeout_seconds: float = Field(gt=0)
    memory_limit_mb: int | None = Field(default=None, ge=16)
    cpu_seconds: int | None = Field(default=None, ge=1)
    max_file_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_open_files: int = Field(default=256, ge=16)


class LimitReport(BaseModel):
    applied: dict[str, int] = Field(default_factory=dict)
    unsupported: dict[str, str] = Field(default_factory=dict)
    enforced_by_parent: list[str] = Field(default_factory=list)


class HarnessRun(BaseModel):
    argv: list[str]
    returncode: int | None
    timed_out: bool
    memory_limit_exceeded: bool
    wall_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    result: dict[str, Any] | None
    rss_peak_bytes: int | None = Field(
        description="Peak of parent-sampled RSS of the child process tree (None if not sampled)"
    )
    rss_samples: int
    rss_sample_interval_s: float | None
    limits: LimitReport

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.memory_limit_exceeded
            and self.result is not None
            and self.result.get("status") == "ok"
        )

    def failure_reason(self) -> str:
        """Human-readable cause, with secrets redacted (it may quote workload exceptions)."""
        return redact_text(self._raw_failure_reason()).text

    def _raw_failure_reason(self) -> str:
        if self.timed_out:
            return "timed out"
        if self.memory_limit_exceeded:
            return "exceeded the memory limit (sampled RSS or RLIMIT_AS)"
        if self.result is None:
            tail = self.stderr.strip().splitlines()[-1:] or ["no output"]
            return f"harness exited with code {self.returncode} without a result: {tail[0]}"
        error = self.result.get("error") or {}
        return f"{error.get('type', 'error')}: {error.get('message', 'unknown error')}"


def build_child_env(
    permitted: Sequence[str], run_dir: Path, parent_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Minimal environment for the child. Only explicitly permitted variables are forwarded."""
    source = os.environ if parent_env is None else parent_env
    env = dict(_BASE_ENV)
    env["PATH"] = os.defpath
    env["HOME"] = str(run_dir / "cwd")
    env["TMPDIR"] = str(run_dir / "tmp")
    for name in permitted:
        if name in env:
            raise SecurityViolationError(f"environment variable {name!r} cannot be overridden")
        if name in source:
            env[name] = source[name]
    return env


def plan_limits(limits: ResourceLimits) -> tuple[list[tuple[int, int, int]], LimitReport]:
    """Decide which rlimits to apply in the child; record the ones this OS cannot enforce."""
    report = LimitReport(enforced_by_parent=["wall_clock_timeout"])
    if limits.memory_limit_mb is not None:
        report.enforced_by_parent.append("rss_ceiling (sampled)")
    if resource is None:
        report.unsupported["all"] = "the resource module is unavailable on this platform"
        return [], report
    wanted: list[tuple[str, int | None]] = [
        ("RLIMIT_CPU", limits.cpu_seconds or math.ceil(limits.timeout_seconds) + 5),
        ("RLIMIT_FSIZE", limits.max_file_bytes),
        ("RLIMIT_NOFILE", limits.max_open_files),
    ]
    memory_bytes = limits.memory_limit_mb * 1024 * 1024 if limits.memory_limit_mb else None
    if sys.platform == "darwin":
        report.unsupported["RLIMIT_AS"] = (
            "macOS does not enforce RLIMIT_AS/RLIMIT_DATA; memory is bounded by the parent's "
            "sampled-RSS ceiling instead"
        )
    else:
        wanted.append(("RLIMIT_AS", memory_bytes))
    plan: list[tuple[int, int, int]] = []
    for name, value in wanted:
        if value is None:
            continue
        constant = getattr(resource, name, None)
        if constant is None:
            report.unsupported[name] = "not defined on this platform"
            continue
        _soft, hard = resource.getrlimit(constant)
        effective = value if hard == resource.RLIM_INFINITY else min(value, hard)
        plan.append((constant, effective, hard))
        report.applied[name] = effective
    return plan, report


def _preexec(plan: list[tuple[int, int, int]]) -> Callable[[], None]:
    def apply() -> None:
        assert resource is not None
        for constant, soft, hard in plan:
            resource.setrlimit(constant, (soft, hard))

    return apply


def _read_capped(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    size = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(MAX_CAPTURED_STREAM_BYTES)
    return data.decode("utf-8", errors="replace"), size > MAX_CAPTURED_STREAM_BYTES


def _tree_rss(proc: psutil.Process) -> int:
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


class _RssSampler(threading.Thread):
    def __init__(self, pid: int, interval: float, ceiling_bytes: int | None) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.ceiling = ceiling_bytes
        self.peak = 0
        self.samples = 0
        self.exceeded = False
        self._stop_event = threading.Event()
        try:
            self._proc: psutil.Process | None = psutil.Process(pid)
        except psutil.NoSuchProcess:
            self._proc = None

    def run(self) -> None:
        while self._proc is not None and not self._stop_event.is_set():
            try:
                rss = _tree_rss(self._proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return
            self.samples += 1
            self.peak = max(self.peak, rss)
            if self.ceiling is not None and rss > self.ceiling:
                self.exceeded = True
                _kill_group(self._proc.pid)
                return
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_harness(
    request: dict[str, Any],
    *,
    run_dir: Path,
    limits: ResourceLimits,
    permitted_env: Sequence[str] = (),
    rss_sample_interval: float | None = 0.01,
    wrapper_argv: Sequence[str] = (),
) -> HarnessRun:
    """Write ``request`` to ``run_dir``, run the harness, and return the parsed outcome.

    ``request["output_dir"]`` defaults to ``<run_dir>/out``. ``wrapper_argv`` (e.g. py-spy)
    is prepended to the interpreter argv; it must start with an absolute executable path.
    """
    run_dir = run_dir.resolve()
    for sub in ("cwd", "out", "tmp"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    request = {"output_dir": str(run_dir / "out"), **request}
    request_path = run_dir / "request.json"
    result_path = run_dir / "result.json"
    result_path.unlink(missing_ok=True)
    request_path.write_text(json.dumps(request), encoding="utf-8")

    if wrapper_argv and not Path(wrapper_argv[0]).is_absolute():
        raise SecurityViolationError("wrapper executable must be an absolute path")
    argv = [
        *wrapper_argv,
        sys.executable,
        *INTERPRETER_FLAGS,
        str(HARNESS_PATH),
        str(request_path),
        str(result_path),
    ]
    plan, report = plan_limits(limits)
    env = build_child_env(permitted_env, run_dir)
    stdout_path, stderr_path = run_dir / "stdout.txt", run_dir / "stderr.txt"
    ceiling = limits.memory_limit_mb * 1024 * 1024 if limits.memory_limit_mb else None
    interval = rss_sample_interval if rss_sample_interval is not None else 0.1

    started = time.perf_counter()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(
            argv,
            cwd=run_dir / "cwd",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=_preexec(plan) if plan else None,
        )
        sampler = _RssSampler(proc.pid, interval, ceiling)
        timed_out_event = threading.Event()

        def on_timeout() -> None:
            timed_out_event.set()
            _kill_group(proc.pid)

        # A blocking wait plus a timer avoids Popen.wait(timeout=...)'s polling latency.
        timer = threading.Timer(limits.timeout_seconds, on_timeout)
        timer.daemon = True
        sampler.start()
        timer.start()
        try:
            proc.wait()
        finally:
            timer.cancel()
            sampler.stop()
            sampler.join(timeout=5)
        timed_out = timed_out_event.is_set()
    wall = time.perf_counter() - started

    result: dict[str, Any] | None = None
    if result_path.exists():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            result = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            result = None
    stdout, stdout_truncated = _read_capped(stdout_path)
    stderr, stderr_truncated = _read_capped(stderr_path)
    sampled = rss_sample_interval is not None and sampler.samples > 0
    # Where the kernel enforces RLIMIT_AS (Linux), the child fails with MemoryError before the
    # parent's RSS sampler sees the growth; that is the same limit being hit.
    hit_address_space_limit = "RLIMIT_AS" in report.applied and (
        (result or {}).get("error", {}).get("type") == "MemoryError"
        or (result is None and "MemoryError" in stderr)
    )
    return HarnessRun(
        argv=argv,
        returncode=proc.returncode,
        timed_out=timed_out,
        memory_limit_exceeded=sampler.exceeded or hit_address_space_limit,
        wall_seconds=wall,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        result=result,
        rss_peak_bytes=sampler.peak if sampled else None,
        rss_samples=sampler.samples,
        rss_sample_interval_s=rss_sample_interval if sampled else None,
        limits=report,
    )
