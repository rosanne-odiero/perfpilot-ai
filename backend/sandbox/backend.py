"""Execution backend contract for running untrusted repository code (ADR-004, ADR-005).

Only the worker process uses an :class:`ExecutionBackend`; the API never does. There is exactly one
implementation, :class:`~backend.sandbox.docker_backend.DockerSandboxBackend`. When it is not
available, runtime mode is disabled with a reason: there is deliberately no host fallback.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.config import Settings, get_settings
from backend.core.exceptions import NotConfiguredError, PerfPilotError, UnavailableError
from backend.core.redaction import redact_text

SANDBOX_WORKSPACE = "/workspace"
SANDBOX_OUTPUT = "/out"
SANDBOX_DEPS = "/deps"
SANDBOX_EXTRA_ROOT = "/perfpilot"
SANDBOX_USER = "65534:65534"

MAX_ARGV_ITEMS = 256
MAX_ARG_CHARS = 4096
MAX_ARGV_TOTAL_CHARS = 64 * 1024
MAX_ENV_VALUE_CHARS = 4096
MAX_EXTRA_MOUNTS = 8

# Environment variables a caller may set inside the sandbox. Everything else (PATH, HOME,
# PYTHONPATH, LD_PRELOAD, credentials...) is either fixed by the backend or rejected.
ALLOWED_ENV_NAMES = frozenset(
    {
        "PYTHONHASHSEED",
        "PYTHONWARNINGS",
        "PYTHONFAULTHANDLER",
        "PYTHONTRACEMALLOC",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "TZ",
        "LANG",
        "LC_ALL",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    }
)
ALLOWED_ENV_PREFIXES = ("PERFPILOT_HARNESS_",)

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Docker image reference: [registry[:port]/]name[:tag][@sha256:digest]; never starts with '-'.
_IMAGE_REF = re.compile(
    r"^[a-z0-9][a-z0-9._\-/:]{0,254}(?:@sha256:[a-f0-9]{64})?$",
)


class SandboxError(PerfPilotError):
    """The sandbox could not run the request (as opposed to the command failing inside it)."""

    status_code = 500
    code = "sandbox_error"


class BackendStatus(BaseModel):
    available: bool
    backend: str
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SandboxLimits(BaseModel):
    """Resource limits for one container. Defaults come from ``sandbox_*`` settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: float = Field(default_factory=lambda: get_settings().sandbox_cpus, gt=0, le=64)
    memory_mb: int = Field(default_factory=lambda: get_settings().sandbox_memory_mb, ge=32)
    pids: int = Field(default_factory=lambda: get_settings().sandbox_pids_limit, ge=8)
    timeout_seconds: int = Field(
        default_factory=lambda: get_settings().sandbox_timeout_seconds, ge=1, le=24 * 3600
    )
    tmpfs_mb: int = Field(default_factory=lambda: get_settings().sandbox_tmpfs_mb, ge=1)
    output_max_bytes: int = Field(
        default_factory=lambda: get_settings().sandbox_output_max_bytes, ge=1024
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> SandboxLimits:
        return cls(
            cpus=settings.sandbox_cpus,
            memory_mb=settings.sandbox_memory_mb,
            pids=settings.sandbox_pids_limit,
            timeout_seconds=settings.sandbox_timeout_seconds,
            tmpfs_mb=settings.sandbox_tmpfs_mb,
            output_max_bytes=settings.sandbox_output_max_bytes,
        )

    def exceeding(self, ceiling: SandboxLimits) -> list[str]:
        """Names of limits that are looser (larger) than ``ceiling``."""
        mine, theirs = self.model_dump(), ceiling.model_dump()
        return [name for name, value in mine.items() if value > theirs[name]]


def validate_argv_structure(argv: list[str]) -> list[str]:
    """Structural checks for a container argv (content policy lives in command discovery)."""
    if not argv:
        raise ValueError("argv must not be empty")
    if len(argv) > MAX_ARGV_ITEMS:
        raise ValueError(f"argv has more than {MAX_ARGV_ITEMS} items")
    total = 0
    for item in argv:
        if "\0" in item:
            raise ValueError("argv items must not contain NUL bytes")
        if len(item) > MAX_ARG_CHARS:
            raise ValueError(f"argv item longer than {MAX_ARG_CHARS} characters")
        total += len(item)
    if total > MAX_ARGV_TOTAL_CHARS:
        raise ValueError("argv is too long")
    if argv[0].startswith("-") or not argv[0]:
        raise ValueError("argv[0] must be a program name")
    return argv


def validate_env(env: dict[str, str]) -> dict[str, str]:
    for name, value in env.items():
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name not in ALLOWED_ENV_NAMES and not name.startswith(ALLOWED_ENV_PREFIXES):
            raise ValueError(f"environment variable not allowed in the sandbox: {name}")
        if len(value) > MAX_ENV_VALUE_CHARS or any(c in value for c in "\0\n\r"):
            raise ValueError(f"invalid value for environment variable {name}")
        if redact_text(f"{name}={value}").count or redact_text(value).count:
            raise ValueError(f"environment variable {name} looks like it carries a secret")
    return env


def validate_image(image: str) -> str:
    if not _IMAGE_REF.match(image):
        raise ValueError(f"invalid container image reference: {image!r}")
    return image


class ReadOnlyMount(BaseModel):
    """An extra host file/dir (e.g. the trusted profiling harness) mounted read-only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Path
    target: str

    @field_validator("target")
    @classmethod
    def _target_under_extra_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or not path.is_relative_to(SANDBOX_EXTRA_ROOT)
            or path == PurePosixPath(SANDBOX_EXTRA_ROOT)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        ):
            raise ValueError(f"mount target must be a normalized path under {SANDBOX_EXTRA_ROOT}/")
        return value


def _default_image() -> str:
    return get_settings().sandbox_image


class SandboxRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_dir: Path = Field(description="Snapshot workspace; mounted read-only at /workspace")
    argv: list[str]
    workdir_rel: str = Field(default=".", description="Working dir relative to /workspace")
    output_dir: Path = Field(description="Empty host dir; mounted read-write at /out")
    env: dict[str, str] = Field(default_factory=dict)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    network: Literal["none"] = "none"
    image: str = Field(default_factory=_default_image)
    extra_mounts: list[ReadOnlyMount] = Field(default_factory=list)
    deps_dir: Path | None = Field(
        default=None, description="Prepared dependency dir; mounted read-only at /deps"
    )

    @field_validator("argv")
    @classmethod
    def _argv(cls, value: list[str]) -> list[str]:
        return validate_argv_structure(value)

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_env(value)

    @field_validator("image")
    @classmethod
    def _image(cls, value: str) -> str:
        return validate_image(value)

    @field_validator("workdir_rel")
    @classmethod
    def _workdir(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\0" in value
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or any(c in value for c in ',"\n\r')
        ):
            raise ValueError("workdir_rel must be a relative path inside the workspace")
        return path.as_posix()

    @model_validator(mode="after")
    def _unique_mounts(self) -> SandboxRunRequest:
        if len(self.extra_mounts) > MAX_EXTRA_MOUNTS:
            raise ValueError(f"at most {MAX_EXTRA_MOUNTS} extra mounts are allowed")
        targets = [m.target for m in self.extra_mounts]
        if len(set(targets)) != len(targets):
            raise ValueError("extra mount targets must be unique")
        return self


class DependencyInstallRequest(BaseModel):
    """Network-enabled ``pip install --target`` of a sanitized requirements manifest."""

    model_config = ConfigDict(extra="forbid")

    manifest_file: Path = Field(description="Sanitized requirements file written by PerfPilot")
    target_dir: Path = Field(description="Empty host dir; mounted read-write at /deps")
    allow_sdists: bool = False
    index_url: str | None = None
    limits: SandboxLimits
    image: str = Field(default_factory=_default_image)

    @field_validator("image")
    @classmethod
    def _image(cls, value: str) -> str:
        return validate_image(value)

    @field_validator("index_url")
    @classmethod
    def _index_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[A-Za-z0-9._~\-/]*)?", value):
            raise ValueError("index_url must be a plain http(s) URL without credentials")
        return value


class SandboxRunResult(BaseModel):
    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    duration_s: float = Field(description="Wall clock incl. container start/stop")
    stdout: str = Field(description="Truncated to output_max_bytes and secret-redacted")
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    redactions: int = 0
    output_files: list[str] = Field(
        default_factory=list, description="Regular files collected from /out (relative paths)"
    )
    rejected_outputs: list[str] = Field(
        default_factory=list, description="Symlinks/special/oversized entries removed from /out"
    )
    container_id: str | None = None
    image_digest: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.oom_killed


@runtime_checkable
class ExecutionBackend(Protocol):
    name: str

    def availability(self) -> BackendStatus: ...

    def run(self, request: SandboxRunRequest) -> SandboxRunResult: ...

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult: ...


def get_execution_backend(
    settings: Settings | None = None, *, require_available: bool = True
) -> ExecutionBackend:
    """Return the configured isolation backend.

    Raises :class:`UnavailableError` (with the reason) when the backend cannot run containers.
    Callers must treat that as "runtime mode disabled" and never fall back to the host.
    """
    settings = settings or get_settings()
    if settings.sandbox_backend != "docker":
        raise NotConfiguredError(
            f"unknown sandbox backend {settings.sandbox_backend!r}; only 'docker' is supported"
        )
    from backend.sandbox.docker_backend import DockerSandboxBackend

    backend = DockerSandboxBackend(settings)
    if require_available:
        status = backend.availability()
        if not status.available:
            raise UnavailableError(
                f"runtime mode is disabled: {status.reason}",
                details={"backend": status.backend, **status.details},
            )
    return backend
