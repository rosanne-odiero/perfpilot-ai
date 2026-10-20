"""The bundled reference implementation of a workload, next to the baseline it replaces.

Reference implementations are hand-written for tests, never platform output; the response carries
that label so clients can show it next to the code.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from backend.api.deps import DbSession
from backend.core.exceptions import NotFoundError
from backend.models.workload_api import SourceFileOut
from backend.models.workloads import REFERENCE_CANDIDATE_LABEL
from backend.services.workloads import (
    build_spec,
    get_workload,
    read_source,
    resolve_target_file,
    resolve_workspace_file,
)

router = APIRouter(tags=["workloads"])


class ReferenceSourceOut(BaseModel):
    workload_id: str
    label: str
    baseline: SourceFileOut
    reference: SourceFileOut


@router.get("/workloads/{workload_id}/reference", response_model=ReferenceSourceOut)
def get_reference_source(workload_id: str, session: DbSession) -> ReferenceSourceOut:
    spec = build_spec(get_workload(session, workload_id))
    if not spec.reference_candidate:
        raise NotFoundError(f"workload {spec.slug!r} has no bundled reference implementation")
    root = Path(spec.workspace_path).resolve()
    return ReferenceSourceOut(
        workload_id=workload_id,
        label=REFERENCE_CANDIDATE_LABEL,
        baseline=SourceFileOut(**read_source(root, resolve_target_file(root, spec.entrypoint))),
        reference=SourceFileOut(
            **read_source(root, resolve_workspace_file(root, spec.reference_candidate))
        ),
    )
