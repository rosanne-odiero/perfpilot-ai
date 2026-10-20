"""Correctness validation of a patched workspace against the original.

Every execution goes through :func:`backend.profiling.runner.run_harness`: a fresh child
process with a fixed interpreter, scrubbed environment, rlimits, a wall-clock timeout and an RSS
ceiling. Checks:

``isolation``             only the target file differs between the two copies
``compile``               the patched target file compiles
``functional_tests``      the workload's own tests pass on the patched copy (and on the original)
``output_equivalence``    original vs candidate outputs over several fixture sizes and seeds,
                          judged by the workload's output contract (exact/unordered/tolerance)
``edge_cases``            the same for the contract's literal edge-case inputs
``exception_parity``      error inputs raise the same exception type in both versions
``input_mutation``        the candidate must not start mutating its input
``patched_code_exercised`` the tests or cases actually ran the patched function

Overall status: ``FAILED`` if any required check failed, ``INCONCLUSIVE`` if any required check
could not decide (timeouts, too few tests, outputs too large to compare), ``PASSED`` only when
every required check passed, ``NOT_RUN`` when nothing ran.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from backend.benchmarking.correctness import compare_outputs
from backend.core.redaction import redact_text
from backend.models.workloads import OutputComparison, WorkloadSpec
from backend.optimization.state_machine import ValidationStatus
from backend.optimization.workspace import changed_files
from backend.profiling.hotspots import parse_profile
from backend.profiling.runner import HarnessRun, ResourceLimits, run_harness
from backend.services.workloads import resolve_target_file

ProgressFn = Callable[[str, float, str | None], None]
_SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
_MAX_LISTED = 20
_TOO_LARGE = "too large"


class CheckResult(BaseModel):
    name: str
    required: bool
    status: ValidationStatus
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class ValidationOutcome(BaseModel):
    status: ValidationStatus
    required_checks_passed: bool
    checks: list[CheckResult]
    summary: str
    baseline_input_hashes: dict[str, str] = Field(default_factory=dict)
    log: str = ""


class ValidationInputs(BaseModel):
    spec: WorkloadSpec
    baseline_root: Path
    patched_root: Path
    target_file: str
    target_function: str | None
    run_dir: Path


def overall_status(checks: list[CheckResult]) -> ValidationStatus:
    required = [check for check in checks if check.required]
    if not required:
        return ValidationStatus.NOT_RUN
    statuses = {check.status for check in required}
    if ValidationStatus.FAILED in statuses:
        return ValidationStatus.FAILED
    if statuses != {ValidationStatus.PASSED}:
        return ValidationStatus.INCONCLUSIVE
    return ValidationStatus.PASSED


def _combine(statuses: list[ValidationStatus]) -> ValidationStatus:
    if not statuses:
        return ValidationStatus.INCONCLUSIVE
    if ValidationStatus.FAILED in statuses:
        return ValidationStatus.FAILED
    if any(status != ValidationStatus.PASSED for status in statuses):
        return ValidationStatus.INCONCLUSIVE
    return ValidationStatus.PASSED


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str) -> None:
        self.lines.append(redact_text(text).text)

    def harness(self, label: str, run: HarnessRun) -> None:
        self.add(
            f"[{label}] argv={' '.join(run.argv[-3:])} returncode={run.returncode} "
            f"timed_out={run.timed_out} wall={run.wall_seconds:.2f}s ok={run.ok}"
        )
        if run.stdout.strip():
            self.add(f"[{label}] stdout:\n{run.stdout[-6000:]}")
        if run.stderr.strip():
            self.add(f"[{label}] stderr:\n{run.stderr[-3000:]}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ------------------------------------------------------------------------------------------
# Functional tests
# ------------------------------------------------------------------------------------------


class _TestRun(BaseModel):
    status: ValidationStatus
    detail: str
    counts: dict[str, int] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    target_calls: int | None = None


def _test_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in reversed(stdout.strip().splitlines()):
        found = _SUMMARY_COUNT.findall(line)
        if found and (" in " in line or "no tests ran" in line):
            for number, kind in found:
                counts["errors" if kind.startswith("error") else kind] = int(number)
            break
    return counts


def _target_calls(prof: Path, root: Path, target_file: str, function: str | None) -> int | None:
    if function is None or not prof.is_file():
        return None
    name = function.rsplit(".", 1)[-1]
    records = parse_profile(prof, root).hotspots
    return sum(
        r.total_calls for r in records if r.file_path == target_file and r.function_name == name
    )


def _run_tests(inputs: ValidationInputs, root: Path, label: str, log: _Log) -> _TestRun:
    spec = inputs.spec
    paths: list[str] = []
    for relative in spec.validation.tests:
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
            return _TestRun(
                status=ValidationStatus.INCONCLUSIVE, detail=f"test path {relative!r} not found"
            )
        paths.append(str(resolved))
    if not paths:
        return _TestRun(status=ValidationStatus.NOT_RUN, detail="no test paths configured")
    run_dir = inputs.run_dir / f"tests-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    ini = run_dir / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    args = [
        *paths[1:],
        "-q",
        "-rfE",
        "--tb=line",
        "--no-header",
        "-c",
        str(ini),
        f"--rootdir={root}",
        f"--confcutdir={root}",
    ]
    run = run_harness(
        {
            "workspace_root": str(root),
            "mode": "command",
            "command": {
                "kind": "pytest",
                "target": Path(paths[0]).relative_to(root.resolve()).as_posix(),
                "args": args,
            },
        },
        run_dir=run_dir,
        limits=ResourceLimits(
            timeout_seconds=min(3600, spec.timeout_seconds * 2),
            memory_limit_mb=spec.memory_limit_mb,
        ),
        permitted_env=spec.permitted_env_vars,
        rss_sample_interval=0.05,
    )
    log.harness(f"pytest {label}", run)
    if run.timed_out or run.memory_limit_exceeded:
        return _TestRun(
            status=ValidationStatus.INCONCLUSIVE, detail=f"tests {run.failure_reason()}"
        )
    if not run.ok:
        return _TestRun(
            status=ValidationStatus.INCONCLUSIVE,
            detail=f"the test runner failed: {run.failure_reason()}",
        )
    result = run.result or {}
    exit_code = int((result.get("command") or {}).get("exit_code", -1))
    counts = _test_counts(run.stdout)
    failures = [
        redact_text(line).text[:300]
        for line in run.stdout.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
    ][:_MAX_LISTED]
    prof = run_dir / "out" / str((result.get("profile") or {}).get("prof_file", "profile.prof"))
    calls = _target_calls(prof, root, inputs.target_file, inputs.target_function)
    passed = counts.get("passed", 0)
    if exit_code == 1 or counts.get("failed") or counts.get("errors"):
        status, detail = ValidationStatus.FAILED, f"{len(failures) or 'some'} test(s) failed"
    elif exit_code == 0 and passed >= max(1, spec.validation.min_tests):
        status, detail = ValidationStatus.PASSED, f"{passed} test(s) passed"
    elif exit_code in (0, 5):
        status = ValidationStatus.INCONCLUSIVE
        detail = (
            f"only {passed} test(s) passed; at least {spec.validation.min_tests} required "
            "(insufficient coverage)"
        )
    else:
        status, detail = ValidationStatus.INCONCLUSIVE, f"pytest exited with code {exit_code}"
    return _TestRun(
        status=status,
        detail=detail,
        counts=counts,
        failures=failures,
        exit_code=exit_code,
        target_calls=calls,
    )


def _functional_check(baseline: _TestRun, patched: _TestRun) -> CheckResult:
    data = {
        "baseline": baseline.model_dump(exclude={"status"}),
        "candidate": patched.model_dump(exclude={"status"}),
    }
    if patched.status == ValidationStatus.FAILED:
        status, detail = ValidationStatus.FAILED, f"candidate: {patched.detail}"
    elif baseline.status != ValidationStatus.PASSED:
        status = ValidationStatus.INCONCLUSIVE
        detail = f"the original does not pass its own tests ({baseline.detail}); they cannot judge"
    else:
        status, detail = patched.status, f"candidate: {patched.detail}"
    return CheckResult(
        name="functional_tests", required=True, status=status, detail=detail, data=data
    )


# ------------------------------------------------------------------------------------------
# Case comparisons
# ------------------------------------------------------------------------------------------


def _cases(spec: WorkloadSpec) -> list[dict[str, Any]]:
    config = spec.validation
    sizes = config.sizes or list(dict.fromkeys([next(iter(spec.sizes)), spec.default_size]))
    seeds = config.seeds or [spec.seed, spec.seed + 1, spec.seed + 2]
    cases: list[dict[str, Any]] = [
        {
            "id": f"fixture:{size}:seed={seed}",
            "kind": "fixture",
            "fixture_kwargs": {**spec.sizes[size], "seed": seed},
        }
        for size in sizes
        for seed in dict.fromkeys(seeds)
    ]
    cases += [
        {"id": f"edge:{c.name}", "kind": "literal", "input": c.input} for c in config.edge_cases
    ]
    cases += [
        {"id": f"error:{c.name}", "kind": "literal", "input": c.input} for c in config.error_cases
    ]
    return cases


def _run_cases(
    inputs: ValidationInputs, root: Path, label: str, cases: list[dict[str, Any]], log: _Log
) -> tuple[dict[str, dict[str, Any]], str | None]:
    spec = inputs.spec
    run = run_harness(
        {
            "workspace_root": str(root),
            "mode": "cases",
            "entrypoint": spec.entrypoint,
            "fixture": spec.fixture_id,
            "fixture_kwargs": spec.fixture_kwargs(spec.default_size),
            "invocation_mode": spec.invocation_mode.value,
            "cases": cases,
            "include_output_value": spec.expected_output.comparison != OutputComparison.EXACT,
        },
        run_dir=inputs.run_dir / f"cases-{label}",
        limits=ResourceLimits(
            timeout_seconds=min(3600, spec.timeout_seconds * max(1, len(cases))),
            memory_limit_mb=spec.memory_limit_mb,
        ),
        permitted_env=spec.permitted_env_vars,
        rss_sample_interval=0.05,
    )
    log.harness(f"cases {label}", run)
    if not run.ok:
        return {}, f"{label} case run {run.failure_reason()}"
    entries = (run.result or {}).get("cases") or []
    return {str(entry.get("id")): entry for entry in entries if isinstance(entry, dict)}, None


def _exception(entry: dict[str, Any]) -> str:
    return str((entry.get("exception") or {}).get("type", "?"))


def _compare_case(
    spec: WorkloadSpec, base: dict[str, Any] | None, cand: dict[str, Any] | None
) -> tuple[ValidationStatus, str]:
    if base is None or cand is None:
        return ValidationStatus.INCONCLUSIVE, "missing result"
    if base.get("status") == "bad_case" or cand.get("status") == "bad_case":
        return ValidationStatus.INCONCLUSIVE, f"invalid case input: {base.get('error')}"
    if base.get("input_sha256") != cand.get("input_sha256"):
        return ValidationStatus.FAILED, "the two versions received different inputs"
    if base["status"] == "exception" or cand["status"] == "exception":
        if base["status"] == cand["status"] and _exception(base) == _exception(cand):
            return ValidationStatus.PASSED, f"both raise {_exception(base)}"
        if base["status"] == "exception" and cand["status"] == "exception":
            return (
                ValidationStatus.FAILED,
                f"original raises {_exception(base)}, candidate {_exception(cand)}",
            )
        if base["status"] == "exception":
            return ValidationStatus.FAILED, f"original raises {_exception(base)}, candidate returns"
        return ValidationStatus.FAILED, f"candidate raises {_exception(cand)}, original returns"
    result = compare_outputs(base, cand, spec.expected_output)
    if result.equivalent:
        return ValidationStatus.PASSED, result.detail
    if _TOO_LARGE in result.detail:
        return ValidationStatus.INCONCLUSIVE, result.detail
    return ValidationStatus.FAILED, result.detail


def _case_check(
    name: str,
    spec: WorkloadSpec,
    ids: list[str],
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    *,
    baseline_must_succeed: bool,
) -> CheckResult:
    per_case: dict[str, dict[str, str]] = {}
    statuses: list[ValidationStatus] = []
    for case_id in ids:
        base, cand = baseline.get(case_id), candidate.get(case_id)
        if baseline_must_succeed and base is not None and base.get("status") != "ok":
            status = ValidationStatus.INCONCLUSIVE
            detail = f"the original itself failed on this input ({_exception(base)})"
        else:
            status, detail = _compare_case(spec, base, cand)
        statuses.append(status)
        per_case[case_id] = {"status": status.value, "detail": detail}
    status = _combine(statuses)
    failed = [k for k, v in per_case.items() if v["status"] == ValidationStatus.FAILED]
    detail = (
        f"{len(ids)} case(s): "
        + ", ".join(f"{sum(1 for s in statuses if s == v)} {v.value}" for v in ValidationStatus)
        + (f"; first failure {failed[0]}: {per_case[failed[0]]['detail']}" if failed else "")
    )
    return CheckResult(
        name=name, required=True, status=status, detail=detail, data={"cases": per_case}
    )


def _error_check(
    spec: WorkloadSpec,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> CheckResult:
    per_case: dict[str, dict[str, str]] = {}
    statuses: list[ValidationStatus] = []
    for case in spec.validation.error_cases:
        case_id = f"error:{case.name}"
        base, cand = baseline.get(case_id), candidate.get(case_id)
        if base is None or cand is None:
            status, detail = ValidationStatus.INCONCLUSIVE, "missing result"
        elif base.get("status") != "exception":
            status = ValidationStatus.INCONCLUSIVE
            detail = "the original does not raise for this error input (contract is wrong)"
        elif case.expected_exception and _exception(base) != case.expected_exception:
            status = ValidationStatus.INCONCLUSIVE
            detail = f"the original raises {_exception(base)}, not {case.expected_exception}"
        elif cand.get("status") != "exception":
            status, detail = ValidationStatus.FAILED, "the candidate returns instead of raising"
        elif _exception(cand) != _exception(base):
            status = ValidationStatus.FAILED
            detail = f"original raises {_exception(base)}, candidate {_exception(cand)}"
        else:
            status, detail = ValidationStatus.PASSED, f"both raise {_exception(base)}"
        statuses.append(status)
        per_case[case_id] = {"status": status.value, "detail": detail}
    status = _combine(statuses)
    failed = [k for k, v in per_case.items() if v["status"] != ValidationStatus.PASSED]
    detail = f"{len(statuses)} error input(s), {len(failed)} not at parity" + (
        f"; {failed[0]}: {per_case[failed[0]]['detail']}" if failed else ""
    )
    return CheckResult(
        name="exception_parity",
        required=True,
        status=status,
        detail=detail,
        data={"cases": per_case},
    )


def _mutation_check(
    spec: WorkloadSpec,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> CheckResult:
    allowed = spec.validation.allow_input_mutation
    introduced, unknown = [], []
    for case_id, cand in candidate.items():
        base = baseline.get(case_id) or {}
        if cand.get("input_mutated") is None:
            unknown.append(case_id)
        elif cand["input_mutated"] and not base.get("input_mutated"):
            introduced.append(case_id)
    if not candidate:
        status, detail = ValidationStatus.INCONCLUSIVE, "no case results to inspect"
    elif introduced and not allowed:
        status = ValidationStatus.FAILED
        detail = f"the candidate mutates its input where the original does not: {introduced[:5]}"
    elif unknown and not allowed:
        status = ValidationStatus.INCONCLUSIVE
        detail = f"input mutation could not be determined for {unknown[:5]}"
    else:
        status = ValidationStatus.PASSED
        detail = (
            "input mutation is permitted by the workload contract"
            if allowed
            else f"no new input mutation across {len(candidate)} case(s)"
        )
    return CheckResult(
        name="input_mutation",
        required=not allowed,
        status=status,
        detail=detail,
        data={"introduced": introduced, "unknown": unknown},
    )


# ------------------------------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------------------------------


def _timed(check: Callable[[], CheckResult]) -> CheckResult:
    started = time.perf_counter()
    result = check()
    result.duration_seconds = round(time.perf_counter() - started, 3)
    return result


def _isolation_check(inputs: ValidationInputs) -> CheckResult:
    changed = changed_files(inputs.baseline_root, inputs.patched_root)
    if changed == [inputs.target_file]:
        return CheckResult(
            name="isolation",
            required=True,
            status=ValidationStatus.PASSED,
            detail=f"only {inputs.target_file} differs from the original",
            data={"changed_files": changed},
        )
    return CheckResult(
        name="isolation",
        required=True,
        status=ValidationStatus.FAILED,
        detail=f"unexpected file changes: {changed}",
        data={"changed_files": changed},
    )


def _compile_check(inputs: ValidationInputs) -> CheckResult:
    path = inputs.patched_root / inputs.target_file
    try:
        compile(path.read_text(encoding="utf-8"), inputs.target_file, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, OSError) as exc:
        return CheckResult(
            name="compile", required=True, status=ValidationStatus.FAILED, detail=str(exc)
        )
    return CheckResult(
        name="compile", required=True, status=ValidationStatus.PASSED, detail="compiles"
    )


def _exercised_check(
    inputs: ValidationInputs, patched_tests: _TestRun, cases_ran: bool
) -> CheckResult:
    spec = inputs.spec
    entry_file = resolve_target_file(inputs.patched_root, spec.entrypoint)
    entry_relative = entry_file.relative_to(inputs.patched_root.resolve()).as_posix()
    via_cases = (
        cases_ran
        and entry_relative == inputs.target_file
        and inputs.target_function == spec.entrypoint_function
    )
    calls = patched_tests.target_calls
    if via_cases or (calls or 0) > 0:
        detail = (
            f"tests called `{inputs.target_function}` {calls} time(s)"
            if calls
            else "the patched function is the workload entrypoint, called by every case"
        )
        status = ValidationStatus.PASSED
    else:
        status = ValidationStatus.INCONCLUSIVE
        detail = "no check executed the patched function (insufficient coverage)"
    return CheckResult(
        name="patched_code_exercised",
        required=True,
        status=status,
        detail=detail,
        data={"test_calls": calls, "entrypoint_cases": via_cases},
    )


def _no_progress(_stage: str, _fraction: float, _message: str | None = None) -> None:
    return None


def run_validation(
    inputs: ValidationInputs, progress: ProgressFn | None = None
) -> ValidationOutcome:
    report: ProgressFn = progress or _no_progress
    spec = inputs.spec
    log = _Log()
    log.add(f"validating {inputs.target_file} ({inputs.target_function}) of workload {spec.slug}")
    inputs.run_dir.mkdir(parents=True, exist_ok=True)
    checks = [_timed(lambda: _isolation_check(inputs)), _timed(lambda: _compile_check(inputs))]
    if any(check.status != ValidationStatus.PASSED for check in checks):
        return _finish(checks, {}, log)

    report("functional_tests", 0.15, "running the workload's tests (original and candidate)")
    started = time.perf_counter()
    baseline_tests = _run_tests(inputs, inputs.baseline_root, "baseline", log)
    patched_tests = _run_tests(inputs, inputs.patched_root, "candidate", log)
    functional = _functional_check(baseline_tests, patched_tests)
    functional.duration_seconds = round(time.perf_counter() - started, 3)
    checks.append(functional)

    report("comparing", 0.5, "comparing outputs, edge cases and exceptions")
    started = time.perf_counter()
    cases = _cases(spec)
    baseline, base_error = _run_cases(inputs, inputs.baseline_root, "baseline", cases, log)
    candidate, cand_error = _run_cases(inputs, inputs.patched_root, "candidate", cases, log)
    elapsed = round(time.perf_counter() - started, 3)
    error = base_error or cand_error
    fixture_ids = [c["id"] for c in cases if c["kind"] == "fixture"]
    edge_ids = [f"edge:{c.name}" for c in spec.validation.edge_cases]
    case_checks: list[CheckResult] = []
    if error:
        names = ["output_equivalence", "input_mutation"]
        names += ["edge_cases"] if edge_ids else []
        names += ["exception_parity"] if spec.validation.error_cases else []
        case_checks = [
            CheckResult(name=n, required=True, status=ValidationStatus.INCONCLUSIVE, detail=error)
            for n in names
        ]
    else:
        case_checks.append(
            _case_check(
                "output_equivalence",
                spec,
                fixture_ids,
                baseline,
                candidate,
                baseline_must_succeed=True,
            )
        )
        if edge_ids:
            case_checks.append(
                _case_check(
                    "edge_cases", spec, edge_ids, baseline, candidate, baseline_must_succeed=False
                )
            )
        if spec.validation.error_cases:
            case_checks.append(_error_check(spec, baseline, candidate))
        case_checks.append(_mutation_check(spec, baseline, candidate))
    for check in case_checks:
        check.duration_seconds = elapsed
    checks.extend(case_checks)
    if not edge_ids:
        checks.append(
            CheckResult(
                name="edge_cases",
                required=False,
                status=ValidationStatus.NOT_RUN,
                detail="the workload contract defines no edge cases",
            )
        )
    if not spec.validation.error_cases:
        checks.append(
            CheckResult(
                name="exception_parity",
                required=False,
                status=ValidationStatus.NOT_RUN,
                detail="the workload contract defines no error inputs",
            )
        )
    checks.append(_exercised_check(inputs, patched_tests, cases_ran=not error))
    hashes = {
        case_id: str(entry["input_sha256"])
        for case_id, entry in baseline.items()
        if case_id.startswith("fixture:") and entry.get("input_sha256")
    }
    report("finishing", 0.95, "summarizing checks")
    return _finish(checks, hashes, log)


def _finish(checks: list[CheckResult], hashes: dict[str, str], log: _Log) -> ValidationOutcome:
    status = overall_status(checks)
    required = [c for c in checks if c.required]
    passed = sum(1 for c in required if c.status == ValidationStatus.PASSED)
    summary = f"{status.value}: {passed}/{len(required)} required checks passed" + "".join(
        f"; {c.name} {c.status.value}" for c in required if c.status != ValidationStatus.PASSED
    )
    for check in checks:
        log.add(f"check {check.name}: {check.status.value} ({check.detail})")
    log.add(summary)
    return ValidationOutcome(
        status=status,
        required_checks_passed=status == ValidationStatus.PASSED,
        checks=checks,
        summary=summary,
        baseline_input_hashes=hashes,
        log=log.text(),
    )
