"""Projects and registered workloads."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.deps import DbSession
from backend.models.workload_api import ProjectOut, WorkloadDetailOut, WorkloadOut
from backend.services import workload_views

router = APIRouter(tags=["workloads"])


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(session: DbSession) -> list[ProjectOut]:
    return workload_views.projects_view(session)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, session: DbSession) -> ProjectOut:
    return workload_views.project_view(session, project_id)


@router.get("/projects/{project_id}/workloads", response_model=list[WorkloadOut])
def list_project_workloads(project_id: str, session: DbSession) -> list[WorkloadOut]:
    return workload_views.workloads_view(session, project_id)


@router.get("/workloads/{workload_id}", response_model=WorkloadDetailOut)
def get_workload(workload_id: str, session: DbSession) -> WorkloadDetailOut:
    return workload_views.workload_detail_view(session, workload_id)
