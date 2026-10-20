"""Response builders for projects and workloads (shared by API routes and the CLI)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.orm.workloads import Project, Workload
from backend.models.workload_api import ProjectOut, SourceFileOut, WorkloadDetailOut, WorkloadOut
from backend.services.workloads import (
    build_spec,
    get_project,
    get_workload,
    list_projects,
    list_workloads,
    read_source,
    resolve_target_file,
)


def project_out(session: Session, project: Project) -> ProjectOut:
    count = session.scalar(
        select(func.count()).select_from(Workload).where(Workload.project_id == project.id)
    )
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        kind=project.kind,
        workload_count=int(count or 0),
        created_at=project.created_at,
    )


def projects_view(session: Session) -> list[ProjectOut]:
    return [project_out(session, p) for p in list_projects(session)]


def project_view(session: Session, project_id: str) -> ProjectOut:
    return project_out(session, get_project(session, project_id))


def workload_out(workload: Workload) -> WorkloadOut:
    spec = build_spec(workload)
    return WorkloadOut(
        id=workload.id,
        project_id=workload.project_id,
        slug=workload.slug,
        name=spec.name,
        description=spec.description,
        entrypoint=spec.entrypoint,
        fixture_id=spec.fixture_id,
        invocation_mode=spec.invocation_mode.value,
        default_size=spec.default_size,
        sizes=sorted(spec.sizes),
        has_reference_candidate=spec.reference_candidate is not None,
        tree_sha256=workload.tree_sha256,
    )


def workloads_view(session: Session, project_id: str) -> list[WorkloadOut]:
    return [workload_out(w) for w in list_workloads(session, project_id)]


def workload_detail_view(session: Session, workload_id: str) -> WorkloadDetailOut:
    workload = get_workload(session, workload_id)
    spec = build_spec(workload)
    root = Path(spec.workspace_path).resolve()
    readme_path = root / "README.md"
    return WorkloadDetailOut(
        **workload_out(workload).model_dump(),
        spec=spec.model_dump(mode="json", exclude={"workspace_path"}),
        fixture_description=spec.fixture_description,
        size_parameters=spec.sizes,
        suspected_bottleneck=spec.suspected_bottleneck,
        entrypoint_source=SourceFileOut(
            **read_source(root, resolve_target_file(root, spec.entrypoint))
        ),
        fixture_source=SourceFileOut(
            **read_source(root, resolve_target_file(root, spec.fixture_id))
        ),
        readme=readme_path.read_text(encoding="utf-8") if readme_path.is_file() else None,
    )
