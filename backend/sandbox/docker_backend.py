"""Docker implementation of :class:`~backend.sandbox.backend.ExecutionBackend`.

The ``docker`` CLI is invoked through ``subprocess`` with a resolved absolute executable path and a
fully constructed argv; there is never a shell. Every container gets the same hardening flags
(:func:`hardening_flags`).

Lifecycle and OOM detection: containers are started **without** ``--rm`` under a unique name.
After the client exits (or after a wall-clock timeout, when we ``docker kill`` the container), we
``docker container inspect`` it to read ``State.ExitCode`` / ``State.OOMKilled`` and the image ID,
and then always ``docker rm -f`` it in a ``finally`` block. With ``--rm`` the container would be
gone before its OOM flag could be read. Containers are labelled ``perfpilot.sandbox=1`` so leftovers
from a crashed worker can be found with ``docker ps -a --filter label=perfpilot.sandbox=1``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO, Any

from backend.core.config import Settings, get_settings
from backend.core.exceptions import SecurityViolationError, ValidationFailedError
from backend.core.redaction import redact_text
from backend.sandbox.backend import (
    SANDBOX_DEPS,
    SANDBOX_EXTRA_ROOT,
    SANDBOX_OUTPUT,
    SANDBOX_USER,
    SANDBOX_WORKSPACE,
    BackendStatus,
    DependencyInstallRequest,
    SandboxError,
    SandboxLimits,
    SandboxRunRequest,
    SandboxRunResult,
)
from backend.sandbox.paths import check_mount_source, overlaps, resolve_contained, resolve_inside

# Fixed locations searched (after an explicit ``sandbox_docker_path``) for the docker CLI.
FIXED_DOCKER_DIRS = (
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
    "/Applications/Docker.app/Contents/Resources/bin",
)
# Only these variables of the worker's environment reach the docker CLI (never API keys etc.).
_CLI_ENV_PASSTHROUGH = (
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
)
CONTAINER_LABEL = "perfpilot.sandbox=1"
INSTALL_MANIFEST_TARGET = f"{SANDBOX_EXTRA_ROOT}/requirements.txt"

_CLI_TIMEOUT = 30.0
_INFO_TIMEOUT = 15.0
_KILL_GRACE_SECONDS = 15.0
_READER_JOIN_SECONDS = 10.0
_MAX_OUTPUT_FILES = 1000


def find_docker_executable(settings: Settings) -> Path | None:
    """Resolve the docker CLI to an absolute, executable file (or ``None``)."""
    if settings.sandbox_docker_path is not None:
        candidate = settings.sandbox_docker_path.expanduser()
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    search = os.pathsep.join([os.environ.get("PATH", ""), *FIXED_DOCKER_DIRS])
    found = shutil.which("docker", path=search)
    if found is None:
        return None
    path = Path(found)
    return path if path.is_absolute() else path.resolve()


def hardening_flags(limits: SandboxLimits, *, network: str) -> list[str]:
    """Flags applied to every sandbox container (execution and dependency install)."""
    return [
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",  # noqa: S108 - inside container
        "--cpus",
        f"{limits.cpus:g}",
        "--memory",
        f"{limits.memory_mb}m",
        "--memory-swap",
        f"{limits.memory_mb}m",
        "--pids-limit",
        str(limits.pids),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        SANDBOX_USER,
        "--ipc",
        "private",
        "--ulimit",
        "core=0:0",
        "--ulimit",
        "nofile=1024:1024",
        "--init",
        "--pull",
        "never",
        "--label",
        CONTAINER_LABEL,
        "--hostname",
        "sandbox",
    ]


def _bind(source: Path, target: str, *, readonly: bool) -> list[str]:
    spec = f"type=bind,source={source},target={target}"
    return ["--mount", f"{spec},readonly" if readonly else spec]


class _BoundedReader(threading.Thread):
    """Drain a pipe completely while keeping at most ``limit`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._chunks: list[bytes] = []
        self._kept = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while chunk := self._stream.read(65536):
                room = self._limit - self._kept
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._kept += min(room, len(chunk))
                if len(chunk) > room:
                    self.truncated = True
        except (OSError, ValueError):
            return

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _prepare_writable_dir(path: Path) -> None:
    """Empty dir the container (uid 65534) must write to. Lives inside PerfPilot's data dir."""
    if any(path.iterdir()):
        raise ValidationFailedError("sandbox output directory must be empty")
    # The sandbox uid differs from the worker's; on Linux bind mounts keep host permissions.
    path.chmod(0o777)


def collect_output_files(output_dir: Path, max_bytes: int) -> tuple[list[str], list[str]]:
    """Accept regular files from ``output_dir`` within the size/count caps; remove the rest.

    Symlinks are never followed and are deleted together with FIFOs, devices and anything that
    exceeds the caps, so later readers of ``output_dir`` only see vetted regular files.
    """
    accepted: list[str] = []
    rejected: list[str] = []
    budget = max_bytes
    for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if (base / name).is_symlink():
                dirnames.remove(name)
                filenames.append(name)
        for name in sorted(filenames):
            entry = base / name
            rel = entry.relative_to(output_dir).as_posix()
            info = entry.lstat()
            ok = (
                stat.S_ISREG(info.st_mode)
                and info.st_size <= budget
                and len(accepted) < _MAX_OUTPUT_FILES
            )
            if ok:
                accepted.append(rel)
                budget -= info.st_size
                continue
            rejected.append(rel)
            # May fail for files the sandbox uid created in its own subdirs; they stay unlisted.
            with contextlib.suppress(OSError):
                entry.unlink()
    return sorted(accepted), sorted(rejected)


class DockerSandboxBackend:
    name = "docker"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._docker: Path | None = find_docker_executable(self.settings)

    # ------------------------------------------------------------------ availability
    def availability(self) -> BackendStatus:
        def unavailable(reason: str, **details: Any) -> BackendStatus:
            return BackendStatus(available=False, backend=self.name, reason=reason, details=details)

        if self._docker is None:
            return unavailable(
                "docker CLI not found (install Docker Desktop / Docker Engine, or set "
                "PERFPILOT_SANDBOX_DOCKER_PATH)"
            )
        details: dict[str, Any] = {"docker_path": str(self._docker)}
        try:
            info = self._cli(["info", "--format", "{{json .}}"], timeout=_INFO_TIMEOUT)
        except subprocess.TimeoutExpired:
            return unavailable("docker daemon did not respond in time", **details)
        except OSError as exc:
            return unavailable(f"docker CLI could not be executed: {exc.strerror}", **details)
        if info.returncode != 0:
            return unavailable(
                f"docker daemon not reachable: {_first_line(info.stderr)}", **details
            )
        try:
            data = json.loads(info.stdout)
        except json.JSONDecodeError:
            return unavailable("unexpected output from 'docker info'", **details)
        if not isinstance(data, dict):
            return unavailable("unexpected output from 'docker info'", **details)
        server_errors = data.get("ServerErrors") or []
        if server_errors or not data.get("ServerVersion"):
            first = str(server_errors[0]) if server_errors else "no server version reported"
            return unavailable(f"docker daemon not reachable: {_first_line(first)}", **details)
        details.update(
            server_version=data.get("ServerVersion"),
            operating_system=data.get("OperatingSystem"),
            cgroup_version=data.get("CgroupVersion"),
            security_options=data.get("SecurityOptions"),
        )
        image = self.settings.sandbox_image
        try:
            inspect = self._cli(
                ["image", "inspect", "--format", "{{.Id}}", image], timeout=_CLI_TIMEOUT
            )
        except (subprocess.TimeoutExpired, OSError):
            return unavailable("docker did not answer the sandbox image lookup", **details)
        if inspect.returncode != 0:
            return unavailable(
                f"sandbox image {image!r} not found; build it with 'make sandbox-image'", **details
            )
        details.update(image=image, image_id=inspect.stdout.strip())
        return BackendStatus(available=True, backend=self.name, details=details)

    # ------------------------------------------------------------------ argv builders
    def build_run_argv(self, request: SandboxRunRequest, *, container_name: str) -> list[str]:
        """Validate every host path and build the full ``docker run`` argv."""
        docker = self._require_docker()
        workspaces = self.settings.workspaces_dir
        artifacts = self.settings.artifacts_dir
        workspace = resolve_contained(request.workspace_dir, [workspaces], what="workspace_dir")
        if not workspace.is_dir():
            raise ValidationFailedError("workspace_dir must be a directory")
        workdir = (
            workspace
            if request.workdir_rel == "."
            else resolve_inside(workspace, request.workdir_rel, what="workdir_rel")
        )
        if not workdir.is_dir():
            raise ValidationFailedError("workdir_rel must be a directory inside the workspace")
        output = resolve_contained(request.output_dir, [workspaces, artifacts], what="output_dir")
        if not output.is_dir():
            raise ValidationFailedError("output_dir must be a directory")
        if overlaps(output, workspace):
            raise SecurityViolationError("output_dir must not overlap the workspace")

        mounts = _bind(workspace, SANDBOX_WORKSPACE, readonly=True)
        mounts += _bind(output, SANDBOX_OUTPUT, readonly=False)
        checked = [(workspace, "workspace_dir"), (output, "output_dir")]
        env = {"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}  # noqa: S108
        env["PYTHONUNBUFFERED"] = "1"
        if request.deps_dir is not None:
            deps = resolve_contained(request.deps_dir, [workspaces], what="deps_dir")
            if not deps.is_dir() or overlaps(deps, workspace) or overlaps(deps, output):
                raise SecurityViolationError("deps_dir must be a separate directory")
            mounts += _bind(deps, SANDBOX_DEPS, readonly=True)
            checked.append((deps, "deps_dir"))
            env["PYTHONPATH"] = SANDBOX_DEPS
        package_root = Path(__file__).resolve().parents[1]
        for mount in request.extra_mounts:
            source = resolve_contained(
                mount.source, [workspaces, artifacts, package_root], what="extra mount source"
            )
            if overlaps(source, output):
                raise SecurityViolationError("extra mount must not overlap output_dir")
            mounts += _bind(source, mount.target, readonly=True)
            checked.append((source, "extra mount source"))
        for path, what in checked:
            check_mount_source(path, what=what)
        env.update(request.env)

        container_workdir = (
            SANDBOX_WORKSPACE
            if request.workdir_rel == "."
            else f"{SANDBOX_WORKSPACE}/{request.workdir_rel}"
        )
        argv = [str(docker), "run", "--name", container_name]
        argv += hardening_flags(request.limits, network=request.network)
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        argv += mounts
        argv += ["--workdir", container_workdir, request.image, *request.argv]
        return argv

    def build_install_argv(
        self, request: DependencyInstallRequest, *, container_name: str
    ) -> list[str]:
        docker = self._require_docker()
        workspaces = self.settings.workspaces_dir
        manifest = resolve_contained(request.manifest_file, [workspaces], what="manifest_file")
        if not manifest.is_file():
            raise ValidationFailedError("manifest_file must be a file")
        target = resolve_contained(request.target_dir, [workspaces], what="target_dir")
        if not target.is_dir() or overlaps(manifest, target):
            raise ValidationFailedError("target_dir must be a directory separate from the manifest")
        check_mount_source(manifest, what="manifest_file")
        check_mount_source(target, what="target_dir")

        pip = [
            "python",
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--no-input",
            "--no-warn-script-location",
            "--target",
            SANDBOX_DEPS,
            "--prefer-binary" if request.allow_sdists else "--only-binary=:all:",
        ]
        if request.index_url:
            pip += ["--index-url", request.index_url]
        pip += ["--requirement", INSTALL_MANIFEST_TARGET]

        argv = [str(docker), "run", "--name", container_name]
        argv += hardening_flags(request.limits, network="bridge")
        for key, value in {
            "HOME": "/tmp",  # noqa: S108
            "TMPDIR": "/tmp",  # noqa: S108
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }.items():
            argv += ["--env", f"{key}={value}"]
        argv += _bind(manifest, INSTALL_MANIFEST_TARGET, readonly=True)
        argv += _bind(target, SANDBOX_DEPS, readonly=False)
        argv += ["--workdir", "/tmp", request.image, *pip]  # noqa: S108
        return argv

    # ------------------------------------------------------------------ execution
    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        name = _container_name()
        argv = self.build_run_argv(request, container_name=name)
        output = request.output_dir.resolve()
        _prepare_writable_dir(output)
        return self._execute(argv, name=name, limits=request.limits, output_dir=output)

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult:
        name = _container_name()
        argv = self.build_install_argv(request, container_name=name)
        target = request.target_dir.resolve()
        _prepare_writable_dir(target)
        return self._execute(argv, name=name, limits=request.limits, output_dir=None)

    def _execute(
        self, argv: list[str], *, name: str, limits: SandboxLimits, output_dir: Path | None
    ) -> SandboxRunResult:
        started = time.monotonic()
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._cli_env(),
            close_fds=True,
            start_new_session=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        out = _BoundedReader(proc.stdout, limits.output_max_bytes)
        err = _BoundedReader(proc.stderr, limits.output_max_bytes)
        out.start()
        err.start()
        timed_out = False
        state: dict[str, Any] | None = None
        try:
            try:
                proc.wait(timeout=limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_container(name)
                try:
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            duration = time.monotonic() - started
            out.join(_READER_JOIN_SECONDS)
            err.join(_READER_JOIN_SECONDS)
            state = self._inspect_container(name)
        finally:
            if proc.poll() is None:
                self._kill_container(name)
                proc.kill()
                proc.wait()
            self._remove_container(name)

        if state is None:
            raise SandboxError(
                "sandbox container could not be started",
                details={
                    "docker_exit_code": proc.returncode,
                    "stderr": redact_text(_first_line(err.text())).text,
                },
            )
        stdout = redact_text(out.text())
        stderr = redact_text(err.text())
        files: list[str] = []
        rejected: list[str] = []
        if output_dir is not None:
            files, rejected = collect_output_files(output_dir, limits.output_max_bytes)
        container_state = state.get("State") or {}
        exit_code = container_state.get("ExitCode")
        return SandboxRunResult(
            exit_code=exit_code if isinstance(exit_code, int) else proc.returncode,
            timed_out=timed_out,
            oom_killed=bool(container_state.get("OOMKilled")),
            duration_s=round(duration, 6),
            stdout=stdout.text,
            stderr=stderr.text,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            redactions=stdout.count + stderr.count,
            output_files=files,
            rejected_outputs=rejected,
            container_id=state.get("Id"),
            image_digest=state.get("Image"),
        )

    # ------------------------------------------------------------------ docker CLI helpers
    def _require_docker(self) -> Path:
        if self._docker is None:
            raise SandboxError("docker CLI not found")
        return self._docker

    def _cli_env(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _CLI_ENV_PASSTHROUGH if key in os.environ}
        env["PATH"] = os.pathsep.join((*FIXED_DOCKER_DIRS, "/bin"))
        env["LC_ALL"] = "C.UTF-8"
        return env

    def _cli(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self._require_docker()), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._cli_env(),
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def _kill_container(self, name: str) -> None:
        # On failure the client process is killed next and removal is still attempted.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            self._cli(["kill", name], timeout=_CLI_TIMEOUT)

    def _inspect_container(self, name: str) -> dict[str, Any] | None:
        try:
            result = self._cli(
                ["container", "inspect", "--format", "{{json .}}", name], timeout=_CLI_TIMEOUT
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _remove_container(self, name: str) -> None:
        # Leftovers are labelled perfpilot.sandbox=1 and can be found and removed later.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            self._cli(["rm", "--force", name], timeout=_CLI_TIMEOUT)


def _container_name() -> str:
    return f"perfpilot-sbx-{uuid.uuid4().hex[:20]}"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no output"
