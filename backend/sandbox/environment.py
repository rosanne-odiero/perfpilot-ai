"""Dependency preparation: the only sandbox step that is allowed network access.

It is a separate operation from execution and needs its own single-use grant
(:func:`backend.sandbox.approvals.consume_dependency_install`). Requirements are parsed
*statically* into a sanitized manifest first: pip options (extra indexes, ``-e``,
``--trusted-host``, ``-f``), direct URL / VCS references and local paths are dropped and
reported, so the install container only ever sees ``name[extras] specifier ; marker`` lines.
By default only wheels are accepted (``--only-binary=:all:``), so no repository-controlled
``setup.py`` runs; source builds need the separate ``allow_sdist_builds`` approval flag.

The result is ``<workspaces_dir>/runtime-envs/<snapshot_id>/deps``, which later runs mount
read-only at ``/deps`` with ``PYTHONPATH=/deps`` and ``--network none``.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import tomllib
import uuid
from pathlib import Path
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.exceptions import PerfPilotError, SecurityViolationError, ValidationFailedError
from backend.core.redaction import redact_text
from backend.sandbox.approvals import DependencyInstallGrant
from backend.sandbox.backend import (
    DependencyInstallRequest,
    ExecutionBackend,
    SandboxLimits,
    SandboxRunResult,
)
from backend.sandbox.paths import overlaps, resolve_contained, resolve_inside

MAX_REQUIREMENT_FILE_BYTES = 512 * 1024
MAX_INCLUDE_DEPTH = 5
MAX_REQUIREMENTS = 500
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_EXTRA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RequirementsSource(BaseModel):
    kind: Literal["requirements_file", "pyproject"]
    path_rel: str = Field(description="requirements*.txt or pyproject.toml inside the workspace")
    extras: list[str] = Field(
        default_factory=list, description="pyproject optional-dependency groups to include"
    )


class SkippedRequirement(BaseModel):
    location: str
    text: str = Field(description="Redacted original line")
    reason: str


class DependencyManifest(BaseModel):
    requirements: list[str]
    skipped: list[SkippedRequirement] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class PreparedEnvironment(BaseModel):
    snapshot_id: str
    deps_dir: str | None = Field(description="Set only when the install succeeded")
    manifest: DependencyManifest
    allow_sdist_builds: bool
    result: SandboxRunResult

    @property
    def succeeded(self) -> bool:
        return self.deps_dir is not None


def runtime_env_dir(settings: Settings, snapshot_id: str) -> Path:
    if not _SNAPSHOT_ID.match(snapshot_id):
        raise SecurityViolationError("invalid snapshot id")
    return settings.workspaces_dir / "runtime-envs" / snapshot_id


def deps_dir_for(settings: Settings, snapshot_id: str) -> Path:
    return runtime_env_dir(settings, snapshot_id) / "deps"


class _ManifestBuilder:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = DependencyManifest(requirements=[])
        self._seen_files: set[Path] = set()

    def skip(self, location: str, text: str, reason: str) -> None:
        shown = redact_text(text.strip()).text[:200]
        self.manifest.skipped.append(
            SkippedRequirement(location=location, text=shown, reason=reason)
        )

    def add(self, location: str, text: str) -> None:
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            self.skip(location, text, "not a 'name[extras] specifier' requirement (local path?)")
            return
        if requirement.url:
            self.skip(location, text, "direct URL / VCS references are not installed")
            return
        if len(self.manifest.requirements) >= MAX_REQUIREMENTS:
            self.skip(location, text, f"more than {MAX_REQUIREMENTS} requirements")
            return
        normalized = str(requirement)
        if normalized not in self.manifest.requirements:
            self.manifest.requirements.append(normalized)

    def _read(self, rel: str) -> tuple[Path, str]:
        path = resolve_inside(self.workspace, rel, what="requirements path")
        if not path.is_file() or (self.workspace / rel).is_symlink():
            raise ValidationFailedError(f"requirements file not found: {rel}")
        if path.stat().st_size > MAX_REQUIREMENT_FILE_BYTES:
            raise ValidationFailedError(f"requirements file too large: {rel}")
        return path, path.read_text(encoding="utf-8", errors="replace")

    def requirements_file(self, rel: str, depth: int = 0) -> None:
        path, text = self._read(rel)
        if path in self._seen_files:
            return
        self._seen_files.add(path)
        self.manifest.sources.append(path.relative_to(self.workspace).as_posix())
        for number, raw in _logical_lines(text):
            location = f"{rel}:{number}"
            line = re.sub(r"(^|\s)#.*$", "", raw).strip()
            if not line:
                continue
            include = re.match(r"^(-r|--requirement)(?:\s+|=)(\S+)$", line)
            if include:
                if depth >= MAX_INCLUDE_DEPTH:
                    self.skip(location, raw, "include depth exceeded")
                    continue
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(rel), include.group(2))
                )
                try:
                    self.requirements_file(target, depth + 1)
                except PerfPilotError as exc:
                    self.skip(location, raw, exc.message)
                continue
            if line.startswith("-"):
                self.skip(location, raw, f"pip option not allowed: {line.split()[0]}")
                continue
            # Per-requirement options (``--hash=...``) are dropped; the requirement is kept.
            self.add(location, re.split(r"\s+--?[a-z]", line, maxsplit=1)[0])

    def pyproject(self, rel: str, extras: list[str]) -> None:
        _, text = self._read(rel)
        self.manifest.sources.append(rel)
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValidationFailedError(f"{rel} is not valid TOML") from exc
        project = data.get("project", {})
        if not isinstance(project, dict) or "name" not in project:
            if "poetry" in data.get("tool", {}):
                raise ValidationFailedError(
                    "Poetry dependency tables are not supported; export a requirements file"
                )
            raise ValidationFailedError(f"{rel} has no [project] table")
        if "dependencies" in project.get("dynamic", []):
            self.skip(rel, "dynamic = ['dependencies']", "dynamic dependencies need a build")
        for item in project.get("dependencies", []) or []:
            self.add(f"{rel}:[project.dependencies]", str(item))
        optional = project.get("optional-dependencies", {}) or {}
        for extra in extras:
            if not _EXTRA.match(extra) or extra not in optional:
                raise ValidationFailedError(f"unknown optional-dependency group: {extra!r}")
            for item in optional[extra]:
                self.add(f"{rel}:[project.optional-dependencies.{extra}]", str(item))


def _logical_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        lines.append((start, buffer + line))
        buffer = ""
    if buffer:
        lines.append((start, buffer))
    return lines


def build_dependency_manifest(
    workspace_dir: Path, source: RequirementsSource
) -> DependencyManifest:
    """Statically turn a requirements file / pyproject into a sanitized requirement list."""
    builder = _ManifestBuilder(workspace_dir.resolve())
    if source.kind == "requirements_file":
        builder.requirements_file(source.path_rel)
    else:
        builder.pyproject(source.path_rel, source.extras)
    return builder.manifest


def install_limits(settings: Settings) -> SandboxLimits:
    return SandboxLimits(
        cpus=settings.sandbox_cpus,
        memory_mb=settings.sandbox_memory_mb,
        pids=settings.sandbox_pids_limit,
        timeout_seconds=settings.sandbox_install_timeout_seconds,
        tmpfs_mb=max(settings.sandbox_tmpfs_mb, 1024),
        output_max_bytes=settings.sandbox_output_max_bytes,
    )


def prepare_environment(
    backend: ExecutionBackend,
    grant: DependencyInstallGrant,
    *,
    workspace_dir: Path,
    source: RequirementsSource,
    settings: Settings | None = None,
) -> PreparedEnvironment:
    """Install a snapshot's declared dependencies into its per-snapshot ``deps`` directory."""
    settings = settings or get_settings()
    workspace = resolve_contained(workspace_dir, [settings.workspaces_dir], what="workspace_dir")
    env_root = runtime_env_dir(settings, grant.snapshot_id)
    if overlaps(env_root.resolve(), workspace):
        raise SecurityViolationError("runtime environment directory overlaps the workspace")
    manifest = build_dependency_manifest(workspace, source)
    if not manifest.requirements:
        raise ValidationFailedError(
            "no installable requirements found",
            details={"skipped": [s.model_dump() for s in manifest.skipped]},
        )

    env_root.mkdir(parents=True, exist_ok=True)
    manifest_file = env_root / "requirements.txt"
    manifest_file.write_text("\n".join(manifest.requirements) + "\n", encoding="utf-8")
    staging = env_root / f"deps.partial-{uuid.uuid4().hex[:12]}"
    staging.mkdir()
    final = deps_dir_for(settings, grant.snapshot_id)
    try:
        result = backend.install_dependencies(
            DependencyInstallRequest(
                manifest_file=manifest_file,
                target_dir=staging,
                allow_sdists=grant.allow_sdist_builds,
                index_url=settings.sandbox_pip_index_url,
                limits=install_limits(settings),
                image=grant.image,
            )
        )
        installed = result.succeeded
        if installed:
            if final.exists():
                shutil.rmtree(final)
            staging.rename(final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    prepared = PreparedEnvironment(
        snapshot_id=grant.snapshot_id,
        deps_dir=str(final) if installed else None,
        manifest=manifest,
        allow_sdist_builds=grant.allow_sdist_builds,
        result=result,
    )
    (env_root / "environment.json").write_text(
        json.dumps(
            {
                "approval_id": grant.approval_id,
                "image_digest": result.image_digest,
                "succeeded": installed,
                "manifest": manifest.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return prepared
