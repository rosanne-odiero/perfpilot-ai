"""Worker job ``repo_optimization.validate``: the repository's own tests, before and after.

The approved pytest command runs twice in the sandbox with identical limits, image, network
(``none``), dependencies and harness request: once on the original snapshot and once on the
patched copy. pytest writes a JUnit report to ``/out``; per-test outcomes are compared.

* ``FAILED``: a test that passed on the original fails, errors, is skipped or disappears on the
  patched copy, or the patched suite does not complete (timeout, OOM, crash, non-zero exit).
* ``INCONCLUSIVE``: the original suite itself does not complete or does not pass, no test passed
  on the original, or the patched function was never executed by the tests (from the cProfile
  table of the patched run).
* ``PASSED``: none of the above.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, PerfPilotError, ValidationFailedError
from backend.core.logging import get_logger
from backend.infrastructure.artifact_store import ArtifactStore
from backend.models.database import session_scope, utcnow
from backend.models.orm.repo_optimization import RepoOptimizationCandidate, RepoValidationRun
from backend.optimization.junit import JUnitReport, parse_junit_file
from backend.optimization.patch_generator import find_function
from backend.optimization.repo_candidates import (
    advance,
    candidate_facts,
    ensure_patched_copy,
    get_candidate,
    snapshot_root,
)
from backend.optimization.repo_sandbox import (
    CommandScope,
    ContainerRun,
    environment_fingerprint,
    prepared_deps,
    require_backend,
    run_container,
    short,
    tail,
)
from backend.optimization.repo_workspace import candidate_root
from backend.optimization.state_machine import OptimizationState, ValidationStatus
from backend.optimization.workspace import (
    OriginalTreeChangedError,
    changed_files,
    verify_original_unchanged,
)
from backend.runtime.execution import STATS_FILE, harness_command
from backend.runtime.profile_stats import load_stats_table, sandbox_classifier
from backend.sandbox.backend import (
    SANDBOX_OUTPUT,
    BackendStatus,
    ExecutionBackend,
    SandboxLimits,
)
from backend.services.source_snapshots import hash_tree

log = get_logger(__name__)

JUNIT_FILE = "junit.xml"
ProgressFn = Callable[[str, float, str | None], None]
V = ValidationStatus


class Check(BaseModel):
    name: str
    status: ValidationStatus
    required: bool = True
    detail: str = ""


class VariantOutcome(BaseModel):
    variant: str
    failure: str | None
    exit_code: int | None
    command_exit_code: int | None
    timed_out: bool
    oom_killed: bool
    duration_s: float
    counts: dict[str, int] | None
    junit_error: str | None
    junit_artifact_id: str | None = None


def pytest_command(argv: list[str]) -> dict[str, Any]:
    """The approved pytest argv as a harness command, plus a JUnit report written to ``/out``.
    The extra flag only adds a report; it does not change which tests run or how."""
    command = harness_command(argv, trace_memory=False)
    if command["kind"] != "pytest":
        raise ConflictError("the approved test command is not a pytest command")
    command["args"] = [*command["args"], f"--junitxml={SANDBOX_OUTPUT}/{JUNIT_FILE}"]
    return command


def function_first_line(source: str, qualname: str | None, line_start: int, line_end: int) -> int:
    """``co_firstlineno`` of the target function in ``source`` (first decorator line)."""
    tree = ast.parse(source)
    _name, node = find_function(tree, qualname, line_start, line_end)
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def patched_function_calls(
    stats_file: Path, workspace: Path, target_file: str, function: str, first_line: int
) -> int:
    """Calls of the patched function recorded by cProfile in the patched run (0 if none)."""
    rows, _callers = load_stats_table(stats_file)
    classify = sandbox_classifier(workspace)
    calls = 0
    for filename, line, name, _cc, nc, _tt, _ct in rows:
        if name == function and line == first_line and classify(filename)[1] == target_file:
            calls += nc
    return calls


def _variant(
    variant: str, run: ContainerRun, junit: JUnitReport | None, junit_error: str | None
) -> VariantOutcome:
    return VariantOutcome(
        variant=variant,
        failure=run.failure(),
        exit_code=run.result.exit_code,
        command_exit_code=run.command_exit_code,
        timed_out=run.result.timed_out,
        oom_killed=run.result.oom_killed,
        duration_s=run.result.duration_s,
        counts=junit.counts if junit else None,
        junit_error=junit_error,
    )


def _junit(run: ContainerRun) -> tuple[JUnitReport | None, str | None]:
    if JUNIT_FILE not in run.files:
        return None, "pytest wrote no JUnit report"
    try:
        return parse_junit_file(run.out_dir / JUNIT_FILE), None
    except ValidationFailedError as exc:
        return None, exc.message


def compare(
    original: VariantOutcome,
    patched: VariantOutcome,
    original_junit: JUnitReport | None,
    patched_junit: JUnitReport | None,
) -> tuple[ValidationStatus, list[Check], dict[str, Any]]:
    """Decide the outcome from both runs (without the coverage check)."""
    checks: list[Check] = []
    comparison: dict[str, Any] = {}
    if original_junit is None or original.timed_out or original.oom_killed:
        reason = original.failure or original.junit_error or "no test results"
        checks.append(
            Check(
                name="original_suite",
                status=V.INCONCLUSIVE,
                detail=f"the original suite did not complete: {reason}",
            )
        )
        return V.INCONCLUSIVE, checks, comparison
    original_passed = original_junit.passed
    if patched_junit is None or patched.timed_out or patched.oom_killed:
        reason = patched.failure or patched.junit_error or "no test results"
        checks.append(Check(name="original_suite", status=V.PASSED, detail=_counts(original)))
        checks.append(
            Check(
                name="patched_suite",
                status=V.FAILED,
                detail=f"the patched suite did not complete while the original did: {reason}",
            )
        )
        return V.FAILED, checks, comparison
    regressions = sorted(
        test
        for test in original_passed
        if patched_junit.outcomes.get(test) != original_junit.outcomes[test]
    )
    comparison = {
        "original_passed": len(original_passed),
        "patched_passed": len(patched_junit.passed),
        "regressions": [
            {"test": test, "patched": str(patched_junit.outcomes.get(test, "missing"))}
            for test in regressions[:200]
        ],
        "regression_count": len(regressions),
        "newly_passing": sorted(patched_junit.passed - original_passed)[:200],
    }
    original_ok = original.command_exit_code == 0 and original.failure is None
    checks.append(
        Check(
            name="original_suite",
            status=V.PASSED if original_ok else V.INCONCLUSIVE,
            detail=_counts(original)
            + ("" if original_ok else f"; {original.failure or 'the suite did not pass'}"),
        )
    )
    if regressions:
        checks.append(
            Check(
                name="patched_suite",
                status=V.FAILED,
                detail=f"{len(regressions)} test(s) that pass on the original do not pass on the "
                f"patched copy (first: {regressions[0]})",
            )
        )
        return V.FAILED, checks, comparison
    if original_ok and patched.failure is not None:
        checks.append(Check(name="patched_suite", status=V.FAILED, detail=patched.failure))
        return V.FAILED, checks, comparison
    checks.append(
        Check(
            name="patched_suite",
            status=V.PASSED,
            detail=f"all {len(original_passed)} test(s) that pass on the original also pass",
        )
    )
    if not original_ok:
        return V.INCONCLUSIVE, checks, comparison
    if not original_passed:
        checks[0] = checks[0].model_copy(
            update={"status": V.INCONCLUSIVE, "detail": "no test passed on the original"}
        )
        return V.INCONCLUSIVE, checks, comparison
    return V.PASSED, checks, comparison


def _counts(outcome: VariantOutcome) -> str:
    counts = outcome.counts or {}
    return ", ".join(f"{counts.get(k, 0)} {k}" for k in ("passed", "failed", "error", "skipped"))


class _Context(BaseModel):
    validation_id: str
    candidate_id: str
    snapshot_id: str
    audit_id: str
    target_file: str
    target_function: str | None
    line_start: int
    line_end: int
    original_tree: str
    patched_source: str
    scope: CommandScope


def _load(settings: Settings, validation_id: str) -> tuple[_Context, Path]:
    with session_scope() as session:
        run = session.get(RepoValidationRun, validation_id)
        if run is None:
            raise ConflictError(f"validation {validation_id} not found")
        if run.status != V.NOT_RUN or run.started_at is not None:
            raise ConflictError(f"validation {validation_id} has already run")
        candidate = get_candidate(session, run.candidate_id)
        run.started_at = utcnow()
        context = _Context(
            validation_id=run.id,
            candidate_id=candidate.id,
            snapshot_id=candidate.snapshot_id,
            audit_id=candidate.audit_id,
            target_file=candidate.target_file,
            target_function=candidate.target_function,
            line_start=candidate.line_start,
            line_end=candidate.line_end,
            original_tree=candidate.original_tree_sha256,
            patched_source=candidate.patched_source,
            scope=CommandScope(
                approval_id=run.approval_id,
                argv=[str(a) for a in run.argv],
                workdir_rel=run.workdir_rel,
                image=run.image,
                limits=SandboxLimits.model_validate(run.limits),
            ),
        )
        root = snapshot_root(session, settings, candidate.snapshot_id)
    return context, root


def _finish(
    settings: Settings,
    ctx: _Context,
    status: ValidationStatus,
    *,
    summary: str,
    checks: list[Check] | None = None,
    variants: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    executed: bool | None = None,
    environment: dict[str, Any] | None = None,
    log_text: str = "",
    error: str | None = None,
    patched_tree: str | None = None,
) -> None:
    with session_scope() as session:
        run = session.get(RepoValidationRun, ctx.validation_id)
        candidate = session.get(RepoOptimizationCandidate, ctx.candidate_id)
        assert run is not None
        assert candidate is not None
        finished = utcnow()
        run.status = status
        run.summary = summary
        run.checks = [c.model_dump(mode="json") for c in checks or []]
        run.variants = variants or {}
        run.test_comparison = comparison or {}
        run.patched_function_executed = executed
        run.environment = environment or {}
        run.patched_tree_sha256 = patched_tree
        run.error = error
        run.finished_at = finished
        started = run.started_at
        if started is not None:
            started = started if started.tzinfo else started.replace(tzinfo=finished.tzinfo)
            run.duration_seconds = (finished - started).total_seconds()
        if log_text:
            artifact = ArtifactStore(settings).save_bytes(
                session,
                kind="repo-validation",
                filename=f"repo-validation-{ctx.validation_id}.log",
                data=log_text.encode("utf-8"),
                media_type="text/plain",
                owner_type="repo_validation_run",
                owner_id=ctx.validation_id,
                meta={"format": "validation log (redacted)", "untrusted": True},
            )
            run.logs_artifact_id = artifact.id
        candidate.validation_status = status
        if OptimizationState(candidate.state) != OptimizationState.VALIDATING:
            return
        facts = candidate_facts(candidate, validation_status=status)
        if status == V.PASSED:
            advance(candidate, OptimizationState.VALIDATED, facts, reason=summary)
        elif status == V.FAILED:
            candidate.error = summary
            advance(candidate, OptimizationState.FAILED, facts, reason=summary)
        else:
            facts.validation_status = V.INCONCLUSIVE
            advance(
                candidate,
                OptimizationState.AWAITING_APPROVAL,
                facts,
                reason=f"validation inconclusive: {summary}",
            )


def _store_junit(settings: Settings, ctx: _Context, variant: str, run: ContainerRun) -> str | None:
    if JUNIT_FILE not in run.files:
        return None
    with session_scope() as session:
        artifact = ArtifactStore(settings).save_file(
            session,
            kind="repo-validation",
            source=run.out_dir / JUNIT_FILE,
            filename=f"repo-validation-{ctx.validation_id}-{variant}-junit.xml",
            media_type="application/xml",
            owner_type="repo_validation_run",
            owner_id=ctx.validation_id,
            meta={"untrusted": True, "note": "written inside the sandbox by the test run"},
        )
        return artifact.id


def execute_repo_validation(
    settings: Settings, validation_id: str, progress: ProgressFn
) -> dict[str, Any]:
    ctx, root = _load(settings, validation_id)
    scratch = candidate_root(settings, ctx.candidate_id) / "runs" / f"validation-{validation_id}"
    try:
        backend, status = require_backend(settings)
        verify_original_unchanged(root, ctx.original_tree, "before validation")
        progress("preparing", 0.1, "verifying the isolated patched workspace")
        patched_root = ensure_patched_copy(settings, ctx.candidate_id, root)
        patched_tree = hash_tree(patched_root)[0]
        differing = changed_files(root, patched_root)
        command = pytest_command(ctx.scope.argv)
        deps = prepared_deps(settings, ctx.snapshot_id)
        runs: dict[str, ContainerRun] = {}
        for index, (variant, workspace) in enumerate(
            (("original", root), ("patched", patched_root))
        ):
            progress("testing", 0.2 + 0.35 * index, f"running the tests on the {variant} tree")
            runs[variant] = run_container(
                backend,
                scope=ctx.scope,
                workspace=workspace,
                scratch=scratch / variant,
                command=command,
                deps_dir=deps,
            )
        verify_original_unchanged(root, ctx.original_tree, "after validation")
        if hash_tree(patched_root)[0] != patched_tree:
            raise ConflictError("the patched workspace changed while it was being validated")
    except OriginalTreeChangedError as exc:
        _finish(settings, ctx, V.FAILED, summary=exc.message, error=exc.message)
        raise
    except PerfPilotError as exc:
        _finish(settings, ctx, V.INCONCLUSIVE, summary=exc.message, error=exc.message)
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    try:
        return _evaluate(
            settings, ctx, backend, status, runs, differing, patched_root, patched_tree
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _evaluate(
    settings: Settings,
    ctx: _Context,
    backend: ExecutionBackend,
    status: BackendStatus,
    runs: dict[str, ContainerRun],
    differing: list[str],
    patched_root: Path,
    patched_tree: str,
) -> dict[str, Any]:
    original_junit, original_error = _junit(runs["original"])
    patched_junit, patched_error = _junit(runs["patched"])
    original = _variant("original", runs["original"], original_junit, original_error)
    patched = _variant("patched", runs["patched"], patched_junit, patched_error)
    original.junit_artifact_id = _store_junit(settings, ctx, "original", runs["original"])
    patched.junit_artifact_id = _store_junit(settings, ctx, "patched", runs["patched"])

    isolation_ok = differing == [ctx.target_file]
    checks = [
        Check(
            name="isolation",
            status=V.PASSED if isolation_ok else V.FAILED,
            detail=f"files that differ from the snapshot: {', '.join(differing) or 'none'}",
        ),
        Check(
            name="compile",
            status=V.PASSED,
            detail="the patched file compiled and passed the patch validator before approval",
        ),
    ]
    status_, suite_checks, comparison = compare(original, patched, original_junit, patched_junit)
    checks += suite_checks
    executed: bool | None = None
    if not isolation_ok:
        status_ = V.FAILED
    elif status_ == V.PASSED:
        executed, coverage = _coverage_check(ctx, runs["patched"], patched_root)
        checks.append(coverage)
        if not executed:
            status_ = V.INCONCLUSIVE
    summary = _summary(status_, checks)
    log_text = "\n".join(
        f"== {variant} ({' '.join(ctx.scope.argv)}) ==\n"
        f"failure: {run.failure() or 'none'}\n"
        f"-- stdout (tail, redacted) --\n{tail(run.result.stdout)}\n"
        f"-- stderr (tail, redacted) --\n{tail(run.result.stderr)}\n"
        for variant, run in runs.items()
    )
    _finish(
        settings,
        ctx,
        status_,
        summary=summary,
        checks=checks,
        variants={
            "original": original.model_dump(mode="json"),
            "patched": patched.model_dump(mode="json"),
        },
        comparison=comparison,
        executed=executed,
        environment={
            **environment_fingerprint(backend, status, ctx.scope, runs["patched"]),
            "dependencies": "prepared /deps (read-only)"
            if prepared_deps(settings, ctx.snapshot_id)
            else "image only",
            "request_sha256": runs["patched"].request_sha256,
            "identical_requests": runs["original"].request_sha256 == runs["patched"].request_sha256,
        },
        log_text=log_text,
        patched_tree=patched_tree,
    )
    log.info(
        "repository candidate validated",
        extra={"candidate_id": ctx.candidate_id, "status": status_.value},
    )
    return {"validation_run_id": ctx.validation_id, "status": status_.value, "summary": summary}


def _coverage_check(ctx: _Context, run: ContainerRun, patched_root: Path) -> tuple[bool, Check]:
    name = (ctx.target_function or "").rsplit(".", 1)[-1]
    if STATS_FILE not in run.files or not name:
        return False, Check(
            name="patched_code_exercised",
            status=V.INCONCLUSIVE,
            detail="the patched run produced no profile statistics",
        )
    try:
        first_line = function_first_line(
            ctx.patched_source, ctx.target_function, ctx.line_start, ctx.line_end
        )
        calls = patched_function_calls(
            run.out_dir / STATS_FILE, patched_root, ctx.target_file, name, first_line
        )
    except (PerfPilotError, SyntaxError, ValueError) as exc:
        detail = exc.message if isinstance(exc, PerfPilotError) else str(exc)
        return False, Check(
            name="patched_code_exercised",
            status=V.INCONCLUSIVE,
            detail=f"could not read the patched run's profile: {short(detail)}",
        )
    if calls == 0:
        return False, Check(
            name="patched_code_exercised",
            status=V.INCONCLUSIVE,
            detail=f"no test executed `{ctx.target_function}` in {ctx.target_file} (cProfile of "
            "the patched run); the tests cannot vouch for the change",
        )
    return True, Check(
        name="patched_code_exercised",
        status=V.PASSED,
        detail=f"`{ctx.target_function}` was called {calls} time(s) by the tests (cProfile)",
    )


def _summary(status: ValidationStatus, checks: list[Check]) -> str:
    decisive = [c for c in checks if c.status == status] or checks
    if status == V.PASSED:
        return "validation passed: " + "; ".join(c.detail for c in checks if c.detail)[:1500]
    return (
        f"validation {status.value.lower()}: "
        + "; ".join(f"{c.name}: {c.detail}" for c in decisive)[:1500]
    )
