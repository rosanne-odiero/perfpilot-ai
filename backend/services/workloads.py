"""Registry of workloads: bundled-example seeding, spec loading, and target validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.exceptions import (
    NotFoundError,
    PerfPilotError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.core.logging import get_logger
from backend.models.orm.workloads import Project, Workload
from backend.models.workloads import WorkloadManifest, WorkloadSpec
from backend.profiling import harness_entry
from backend.services.source_snapshots import create_snapshot, hash_tree

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = PROJECT_ROOT / "examples"
BUNDLED_PROJECT_NAME = "Bundled examples"
MANIFEST_NAME = "workload.json"
MAX_SOURCE_BYTES = 256 * 1024


def load_manifest(workspace: Path) -> WorkloadManifest:
    path = workspace / MANIFEST_NAME
    try:
        return WorkloadManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValidationFailedError(
            f"invalid workload manifest {path.name} in {workspace.name}: {exc}"
        ) from exc


def resolve_target_file(workspace_root: Path, target: str) -> Path:
    """Resolve ``module:function`` to its module file, which must lie inside the workspace.

    Uses the same resolution code as the child harness, so the parent rejects exactly what the
    child would reject, before any process is started.
    """
    root = workspace_root.resolve()
    module = target.split(":", 1)[0]
    try:
        return harness_entry.module_path(root, module)
    except harness_entry.HarnessError as exc:
        if exc.code == "outside_workspace":
            raise SecurityViolationError(str(exc), details={"target": target}) from exc
        raise ValidationFailedError(str(exc), details={"target": target}) from exc


def resolve_workspace_file(workspace_root: Path, relative: str) -> Path:
    root = workspace_root.resolve()
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SecurityViolationError("path must be relative and inside the workspace")
    path = (root / candidate).resolve()
    if not path.is_relative_to(root):
        raise SecurityViolationError("path resolves outside the workspace")
    if not path.is_file():
        raise ValidationFailedError(f"{relative!r} does not exist in the workspace")
    return path


def validate_targets(
    root: Path, entrypoint: str, fixture: str, reference_candidate: str | None
) -> None:
    if not root.is_dir():
        raise ValidationFailedError(f"workspace {root.name!r} is missing")
    resolve_target_file(root, entrypoint)
    resolve_target_file(root, fixture)
    if reference_candidate:
        resolve_workspace_file(root, reference_candidate)


def validate_spec_targets(spec: WorkloadSpec) -> None:
    validate_targets(
        Path(spec.workspace_path), spec.entrypoint, spec.fixture_id, spec.reference_candidate
    )


def build_spec(workload: Workload) -> WorkloadSpec:
    manifest = WorkloadManifest.model_validate(workload.manifest)
    return WorkloadSpec(
        id=workload.id,
        project_id=workload.project_id,
        slug=workload.slug,
        name=manifest.name,
        description=manifest.description,
        entrypoint=manifest.entrypoint,
        fixture_id=manifest.fixture,
        fixture_description=manifest.fixture_description,
        invocation_mode=manifest.invocation_mode,
        expected_output=manifest.expected_output,
        timeout_seconds=manifest.timeout_seconds,
        memory_limit_mb=manifest.memory_limit_mb,
        working_dir_id=workload.source_snapshot_id,
        workspace_path=workload.workspace_path,
        permitted_env_vars=manifest.permitted_env_vars,
        default_size=manifest.default_size,
        sizes=manifest.sizes,
        seed=manifest.seed,
        reference_candidate=manifest.reference_candidate,
        suspected_bottleneck=manifest.suspected_bottleneck,
        validation=manifest.validation,
    )


def discover_bundled(examples_dir: Path = EXAMPLES_DIR) -> list[tuple[str, Path, WorkloadManifest]]:
    if not examples_dir.is_dir():
        return []
    found = []
    for child in sorted(examples_dir.iterdir()):
        if not (child.is_dir() and (child / MANIFEST_NAME).is_file()):
            continue
        try:
            found.append((child.name, child.resolve(), load_manifest(child)))
        except ValidationFailedError as exc:
            log.warning(
                "skipping invalid bundled workload",
                extra={"slug": child.name, "error": exc.message},
            )
    return found


def seed_bundled_examples(session: Session, examples_dir: Path = EXAMPLES_DIR) -> Project | None:
    """Idempotently register ``examples/*`` as the "Bundled examples" project.

    Re-running updates paths, manifests, and source snapshots when the files changed (for
    example after the checkout moved), and never duplicates rows.
    """
    bundled = discover_bundled(examples_dir)
    if not bundled:
        log.warning("no bundled examples found", extra={"path": str(examples_dir)})
        return None
    project = session.scalar(select(Project).where(Project.name == BUNDLED_PROJECT_NAME))
    if project is None:
        project = Project(
            name=BUNDLED_PROJECT_NAME,
            description="Trusted example workloads shipped with PerfPilot (examples/).",
            kind="bundled_examples",
            root_path=str(examples_dir.resolve()),
        )
        session.add(project)
        session.flush()
    project.root_path = str(examples_dir.resolve())
    for slug, workspace, manifest in bundled:
        try:
            validate_targets(
                workspace, manifest.entrypoint, manifest.fixture, manifest.reference_candidate
            )
        except PerfPilotError as exc:
            log.warning(
                "skipping bundled workload with invalid targets",
                extra={"slug": slug, "error": exc.message},
            )
            continue
        workload = session.scalar(
            select(Workload).where(Workload.project_id == project.id, Workload.slug == slug)
        )
        if workload is None:
            workload = Workload(project_id=project.id, slug=slug, workspace_path=str(workspace))
            session.add(workload)
        workload.name = manifest.name
        workload.description = manifest.description
        workload.workspace_path = str(workspace)
        workload.manifest = manifest.model_dump(mode="json")
        session.flush()
        tree_sha, _ = hash_tree(workspace)
        if workload.tree_sha256 != tree_sha:
            snapshot = create_snapshot(
                session,
                workload_id=workload.id,
                role="baseline",
                label="registered",
                root=workspace,
            )
            workload.source_snapshot_id = snapshot.id
            workload.tree_sha256 = tree_sha
    session.flush()
    return project


def list_projects(session: Session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.name)))


def get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


def list_workloads(session: Session, project_id: str) -> list[Workload]:
    get_project(session, project_id)
    stmt = select(Workload).where(Workload.project_id == project_id).order_by(Workload.slug)
    return list(session.scalars(stmt))


def get_workload(session: Session, workload_id: str) -> Workload:
    workload = session.get(Workload, workload_id)
    if workload is None:
        raise NotFoundError(f"workload {workload_id} not found")
    return workload


def resolve_size(spec: WorkloadSpec, size: str | None) -> str:
    chosen = size or spec.default_size
    if chosen not in spec.sizes:
        raise ValidationFailedError(
            f"unknown size {chosen!r}", details={"allowed": sorted(spec.sizes)}
        )
    return chosen


def read_source(workspace_root: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    truncated = len(data) > MAX_SOURCE_BYTES
    return {
        "path": path.relative_to(workspace_root.resolve()).as_posix(),
        "content": data[:MAX_SOURCE_BYTES].decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
