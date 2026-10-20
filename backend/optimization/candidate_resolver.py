"""Benchmark integration: lets ``POST /api/benchmarks`` run a VALIDATED patch candidate.

``install()`` registers three hooks with :mod:`backend.benchmarking.candidates`:

* ``check`` (at submission): the candidate must belong to the benchmarked workload and pass the
  ``VALIDATED -> BENCHMARKING`` guard (validation PASSED, approved, original tree unchanged);
  otherwise the request fails with 404/409 before any job is queued;
* ``resolve`` (in the worker): copies the validated patched workspace after re-checking its hash;
* ``on_finished``: links the run and moves the candidate to ``COMPLETED`` only when the run
  succeeded on the same input as the validated baseline; a non-comparable run marks it FAILED,
  a crashed run returns it to ``VALIDATED`` for a retry.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from backend.benchmarking.candidates import CandidateSource, register_candidate_resolver
from backend.core.config import get_settings
from backend.core.exceptions import ConflictError, SecurityViolationError
from backend.core.logging import get_logger
from backend.models.orm.optimization import OptimizationCandidate, ValidationRun
from backend.models.orm.workloads import BenchmarkRun, SourceSnapshot
from backend.models.workloads import (
    BenchmarkClassification,
    CandidateKind,
    RunStatus,
    WorkloadSpec,
)
from backend.optimization.lifecycle import advance, candidate_facts
from backend.optimization.state_machine import OptimizationState
from backend.optimization.workspace import candidate_dir, verify_original_unchanged
from backend.services.optimizations import get_candidate, workspace_of
from backend.services.source_snapshots import hash_tree
from backend.services.workloads import build_spec, get_workload

log = get_logger(__name__)


def candidate_label(candidate: OptimizationCandidate) -> str:
    origin = candidate.transform or candidate.generated_by
    return f"validated patch candidate {candidate.id} ({candidate.generated_by}: {origin})"


def check(session: Session, spec: WorkloadSpec, candidate_id: str) -> str:
    candidate = get_candidate(session, candidate_id)
    if candidate.workload_id != spec.id:
        raise ConflictError(
            "the candidate was validated against a different workload; its input cannot be "
            "compared with this baseline",
            details={"candidate_workload_id": candidate.workload_id},
        )
    validation = (
        session.get(ValidationRun, candidate.validation_run_id)
        if candidate.validation_run_id
        else None
    )
    facts = candidate_facts(candidate, workspace=workspace_of(spec), validation=validation)
    advance(candidate, OptimizationState.BENCHMARKING, facts, reason="benchmark requested")
    return candidate_label(candidate)


def resolve(session: Session, spec: WorkloadSpec, candidate_id: str, dest: Path) -> CandidateSource:
    candidate = get_candidate(session, candidate_id)
    if candidate.state != OptimizationState.BENCHMARKING:
        raise ConflictError(f"candidate {candidate_id} is {candidate.state}, not BENCHMARKING")
    verify_original_unchanged(
        workspace_of(spec), candidate.original_tree_sha256, "before benchmarking"
    )
    if not candidate.patched_workspace_path or not candidate.patched_tree_sha256:
        raise ConflictError("the candidate has no validated workspace")
    patched = Path(candidate.patched_workspace_path).resolve()
    if not patched.is_relative_to(candidate_dir(get_settings(), candidate.id)):
        raise SecurityViolationError("the patched workspace is outside the candidate directory")
    if hash_tree(patched)[0] != candidate.patched_tree_sha256:
        raise ConflictError("the patched workspace changed after validation; re-validate it")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(patched, dest, symlinks=True)
    return CandidateSource(
        kind=CandidateKind.CANDIDATE,
        candidate_id=candidate.id,
        label=candidate_label(candidate),
        root=dest.resolve(),
        source_sha256=candidate.patched_file_sha256 or "",
    )


def _input_matches(
    session: Session, candidate: OptimizationCandidate, run: BenchmarkRun, spec_seed: int
) -> bool:
    if run.input_sha256 is None:
        return False
    validation = (
        session.get(ValidationRun, candidate.validation_run_id)
        if candidate.validation_run_id
        else None
    )
    expected = (validation.baseline_input_hashes if validation else {}).get(
        f"fixture:{run.size}:seed={spec_seed}"
    )
    if expected is not None and expected != run.input_sha256:
        return False
    snapshot = (
        session.get(SourceSnapshot, run.baseline_snapshot_id) if run.baseline_snapshot_id else None
    )
    return snapshot is not None and snapshot.tree_sha256 == candidate.original_tree_sha256


def on_finished(session: Session, run: BenchmarkRun) -> None:
    if run.candidate_id is None:
        return
    candidate = session.get(OptimizationCandidate, run.candidate_id)
    if candidate is None or candidate.state != OptimizationState.BENCHMARKING:
        log.warning(
            "benchmark finished for a candidate that is not BENCHMARKING",
            extra={"benchmark_run_id": run.id, "candidate_id": run.candidate_id},
        )
        return
    spec = build_spec(get_workload(session, candidate.workload_id))
    candidate.benchmark_run_id = run.id
    validation = (
        session.get(ValidationRun, candidate.validation_run_id)
        if candidate.validation_run_id
        else None
    )
    matches = _input_matches(session, candidate, run, spec.seed)
    facts = candidate_facts(
        candidate,
        validation=validation,
        benchmark_status=run.status,
        benchmark_input_matches_baseline=matches,
    )
    invalid = run.classification == BenchmarkClassification.INVALID
    if run.status == RunStatus.SUCCEEDED and not invalid and matches:
        advance(
            candidate,
            OptimizationState.COMPLETED,
            facts,
            reason=f"benchmark {run.id}: {run.classification}",
        )
    elif invalid or (run.status == RunStatus.SUCCEEDED and not matches):
        reason = (
            f"benchmark {run.id} is not comparable with the baseline: "
            f"{run.classification_reason or 'input differs from the validated baseline input'}"
        )
        candidate.error = reason
        advance(candidate, OptimizationState.FAILED, facts, reason=reason)
    else:
        advance(
            candidate,
            OptimizationState.VALIDATED,
            facts,
            reason=f"benchmark {run.id} failed to run ({run.error}); it can be retried",
        )


def install() -> None:
    register_candidate_resolver(resolve, checker=check, on_finished=on_finished)
