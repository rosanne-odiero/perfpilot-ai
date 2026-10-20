"""Contracts for workloads, profiling, and benchmarking (base spec §6, §11-§12).

``WorkloadManifest`` is the on-disk ``workload.json`` of a bundled example. ``WorkloadSpec`` is
the registered, validated form (with database ids) that the runner executes.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SIZE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Variables that change interpreter, loader, or search-path behaviour can never be forwarded.
_FORBIDDEN_ENV_PREFIXES = ("PYTHON", "LD_", "DYLD_", "PERFPILOT_")
_FORBIDDEN_ENV_NAMES = {"PATH", "HOME", "TMPDIR", "VIRTUAL_ENV", "CONDA_PREFIX"}


class InvocationMode(StrEnum):
    """How the fixture's return value is passed to the entrypoint."""

    SINGLE = "single"  # fn(data)
    POSITIONAL = "positional"  # fn(*data)
    KEYWORD = "keyword"  # fn(**data)
    NONE = "none"  # fn()


class OutputComparison(StrEnum):
    EXACT = "exact"
    FLOAT_TOLERANCE = "float_tolerance"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class BenchmarkClassification(StrEnum):
    IMPROVED = "IMPROVED"
    REGRESSED = "REGRESSED"
    NO_CLEAR_DIFFERENCE = "NO_CLEAR_DIFFERENCE"
    INCONCLUSIVE = "INCONCLUSIVE"
    INVALID = "INVALID"


class CandidateKind(StrEnum):
    REFERENCE = "reference"
    CANDIDATE = "candidate"


class HotspotLocation(StrEnum):
    WORKSPACE = "workspace"
    STDLIB = "stdlib"
    SITE_PACKAGES = "site-packages"
    BUILTIN = "builtin"
    HARNESS = "harness"
    EXTERNAL = "external"


class HotspotSort(StrEnum):
    EXCLUSIVE = "exclusive"
    CUMULATIVE = "cumulative"
    CALLS = "calls"


REFERENCE_CANDIDATE_LABEL = "reference implementation (bundled, for testing)"


def validate_target(value: str, what: str) -> str:
    """``module[.sub]:function`` made of identifiers only: no paths, no ``..``, no dunders."""
    if not _TARGET.match(value):
        raise ValueError(f"{what} must look like 'module:function' using identifiers only")
    if any(part.startswith("__") for part in re.split(r"[.:]", value)):
        raise ValueError(f"{what} may not reference dunder names")
    return value


def validate_env_names(names: list[str]) -> list[str]:
    for name in names:
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name {name!r}")
        if name in _FORBIDDEN_ENV_NAMES or name.startswith(_FORBIDDEN_ENV_PREFIXES):
            raise ValueError(f"environment variable {name!r} may not be forwarded to workloads")
    return sorted(set(names))


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="Human-readable expected type, e.g. 'dict[str, int]'")
    comparison: OutputComparison = OutputComparison.EXACT
    rel_tol: float = Field(default=1e-9, ge=0)
    abs_tol: float = Field(default=0.0, ge=0)
    description: str = ""


class WorkloadManifest(BaseModel):
    """Schema of an example's ``workload.json``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str
    entrypoint: str
    fixture: str
    fixture_description: str = ""
    invocation_mode: InvocationMode = InvocationMode.SINGLE
    expected_output: OutputContract
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    memory_limit_mb: int = Field(default=1024, ge=16, le=65536)
    permitted_env_vars: list[str] = Field(default_factory=list)
    default_size: str
    sizes: dict[str, dict[str, Any]]
    seed: int = 0
    reference_candidate: str | None = Field(
        default=None, description="Workspace-relative path of a bundled reference implementation"
    )
    suspected_bottleneck: str = ""

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        return validate_target(value, "entrypoint")

    @field_validator("fixture")
    @classmethod
    def _fixture(cls, value: str) -> str:
        return validate_target(value, "fixture")

    @field_validator("permitted_env_vars")
    @classmethod
    def _env(cls, value: list[str]) -> list[str]:
        return validate_env_names(value)

    @field_validator("reference_candidate")
    @classmethod
    def _reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith(("/", "\\")) or ".." in re.split(r"[/\\]", value) or ":" in value:
            raise ValueError("reference_candidate must be a relative path inside the workspace")
        if not value.endswith(".py"):
            raise ValueError("reference_candidate must be a .py file")
        return value

    @model_validator(mode="after")
    def _sizes(self) -> WorkloadManifest:
        if not self.sizes:
            raise ValueError("at least one size is required")
        for size in self.sizes:
            if not _SIZE_NAME.match(size):
                raise ValueError(f"invalid size name {size!r}")
        if self.default_size not in self.sizes:
            raise ValueError("default_size must be one of sizes")
        return self


class WorkloadSpec(BaseModel):
    """A registered workload (base spec §6). Targets are resolved inside ``workspace_path``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    slug: str
    name: str
    description: str
    entrypoint: str
    fixture_id: str
    fixture_description: str = ""
    input_dataset_id: str | None = Field(
        default=None, description="Set when inputs come from a stored dataset, not a fixture"
    )
    invocation_mode: InvocationMode
    expected_output: OutputContract
    timeout_seconds: int = Field(ge=1, le=3600)
    memory_limit_mb: int = Field(ge=16, le=65536)
    working_dir_id: str | None = Field(
        default=None, description="Id of the source snapshot of the working directory"
    )
    workspace_path: str
    permitted_env_vars: list[str] = Field(default_factory=list)
    default_size: str
    sizes: dict[str, dict[str, Any]]
    seed: int = 0
    reference_candidate: str | None = None
    suspected_bottleneck: str = ""

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        return validate_target(value, "entrypoint")

    @field_validator("fixture_id")
    @classmethod
    def _fixture(cls, value: str) -> str:
        return validate_target(value, "fixture")

    @field_validator("permitted_env_vars")
    @classmethod
    def _env(cls, value: list[str]) -> list[str]:
        return validate_env_names(value)

    @property
    def entrypoint_module(self) -> str:
        return self.entrypoint.split(":", 1)[0]

    @property
    def entrypoint_function(self) -> str:
        return self.entrypoint.split(":", 1)[1]

    def fixture_kwargs(self, size: str) -> dict[str, Any]:
        return {**self.sizes[size], "seed": self.seed}


# ------------------------------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------------------------------


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload_id: str = Field(min_length=1, max_length=64)
    size: str | None = Field(default=None, description="Defaults to the workload's default size")
    enable_sampling: bool = Field(default=False, description="Also run py-spy when available")


class CandidateRef(BaseModel):
    """Reference to a validated patch candidate (resolved by a registered resolver)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=64)


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload_id: str = Field(min_length=1, max_length=64)
    candidate: Literal["reference"] | CandidateRef = "reference"
    size: str | None = None
    warmups: int = Field(default=5, ge=0, le=100)
    runs: int = Field(default=20, ge=3, le=500)
    memory_runs: int = Field(default=3, ge=0, le=50)
    threshold_percent: float = Field(default=5.0, gt=0, le=100)
    seed: int = Field(default=12345, ge=0, le=2**32 - 1)
    order: Literal["random", "alternate"] = "random"
    bootstrap_resamples: int = Field(default=10_000, ge=1_000, le=200_000)
