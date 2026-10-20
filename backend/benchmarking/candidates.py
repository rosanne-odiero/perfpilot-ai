"""Candidate source trees for A/B benchmarks.

A candidate is a full copy of the workload's workspace in which the entrypoint module is
replaced. Today the only built-in source is the bundled *reference implementation* (clearly
labeled as such). Validated patch candidates are provided by the patch-validation phase
(``backend.optimization.candidate_resolver``), which registers a resolver with
:func:`register_candidate_resolver`; without one, requesting a candidate fails with a typed
``NotConfiguredError``.

The registration can also supply a ``checker`` (called when a benchmark is *submitted*, so an
ineligible candidate is refused before any job is queued) and an ``on_finished`` hook (called
once the benchmark run reached a terminal status, successful or not).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.exceptions import NotConfiguredError, ValidationFailedError
from backend.infrastructure.artifact_store import sha256_file
from backend.models.orm.workloads import BenchmarkRun
from backend.models.workloads import (
    REFERENCE_CANDIDATE_LABEL,
    CandidateKind,
    CandidateRef,
    WorkloadSpec,
)
from backend.services.source_snapshots import IGNORED_DIRS
from backend.services.workloads import resolve_target_file, resolve_workspace_file


class CandidateSource(BaseModel):
    kind: CandidateKind
    candidate_id: str | None
    label: str
    root: Path
    source_sha256: str


CandidateResolver = Callable[[Session, WorkloadSpec, str, Path], CandidateSource]
CandidateChecker = Callable[[Session, WorkloadSpec, str], str]
BenchmarkFinishedHook = Callable[[Session, BenchmarkRun], None]
_resolver: CandidateResolver | None = None
_checker: CandidateChecker | None = None
_on_finished: BenchmarkFinishedHook | None = None


def register_candidate_resolver(
    resolver: CandidateResolver | None,
    *,
    checker: CandidateChecker | None = None,
    on_finished: BenchmarkFinishedHook | None = None,
) -> None:
    """Install (or with ``None`` remove) the resolver for ``{"candidate_id": ...}`` requests.

    ``checker(session, spec, candidate_id)`` returns the candidate's display label or raises a
    ``PerfPilotError``. ``on_finished(session, run)`` observes terminal benchmark runs.
    """
    global _resolver, _checker, _on_finished
    _resolver = resolver
    _checker = checker if resolver is not None else None
    _on_finished = on_finished if resolver is not None else None


def check_candidate(session: Session, spec: WorkloadSpec, candidate_id: str) -> str:
    """Refuse an ineligible candidate at submission time; returns its display label."""
    if _resolver is None:
        raise NotConfiguredError(
            "benchmarking validated patch candidates requires the patch-validation phase; "
            "no candidate resolver is registered",
            details={"candidate_id": candidate_id},
        )
    return _checker(session, spec, candidate_id) if _checker else f"candidate {candidate_id}"


def notify_benchmark_finished(session: Session, run: BenchmarkRun) -> None:
    if run.candidate_kind == CandidateKind.CANDIDATE and _on_finished is not None:
        _on_finished(session, run)


def _copy_workspace(root: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        root, dest, symlinks=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS, "*.pyc")
    )


def prepare_reference_candidate(spec: WorkloadSpec, dest: Path) -> CandidateSource:
    if not spec.reference_candidate:
        raise ValidationFailedError(f"workload {spec.slug!r} has no bundled reference candidate")
    root = Path(spec.workspace_path).resolve()
    reference = resolve_workspace_file(root, spec.reference_candidate)
    entry_module = resolve_target_file(root, spec.entrypoint)
    _copy_workspace(root, dest)
    shutil.copyfile(reference, dest / entry_module.relative_to(root))
    return CandidateSource(
        kind=CandidateKind.REFERENCE,
        candidate_id=None,
        label=REFERENCE_CANDIDATE_LABEL,
        root=dest.resolve(),
        source_sha256=sha256_file(reference),
    )


def resolve_candidate(
    session: Session, spec: WorkloadSpec, candidate: str | CandidateRef, dest: Path
) -> CandidateSource:
    if candidate == "reference":
        return prepare_reference_candidate(spec, dest)
    if isinstance(candidate, CandidateRef):
        if _resolver is None:
            raise NotConfiguredError(
                "benchmarking validated patch candidates requires the patch-validation phase; "
                "no candidate resolver is registered",
                details={"candidate_id": candidate.candidate_id},
            )
        return _resolver(session, spec, candidate.candidate_id, dest)
    raise ValidationFailedError(f"unsupported candidate {candidate!r}")
