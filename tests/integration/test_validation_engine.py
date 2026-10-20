from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from backend.benchmarking.correctness import compare_outputs
from backend.models.workloads import (
    InvocationMode,
    OutputComparison,
    OutputContract,
    ValidationCase,
    ValidationConfig,
    WorkloadSpec,
)
from backend.optimization.state_machine import ValidationStatus
from backend.optimization.validation import (
    CheckResult,
    ValidationInputs,
    overall_status,
    run_validation,
)

ORIGINAL = """
def double_all(items: list[int]) -> list[int]:
    out = []
    for item in items:
        out.append(item * 2)
    return out
"""
FIXTURES = """
import random


def make_input(n: int, seed: int = 0) -> list[int]:
    rng = random.Random(seed)
    return [rng.randint(0, 100) for _ in range(n)]
"""
TESTS = """
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mini_workload", ROOT / "workload.py")
workload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workload)


def test_empty():
    assert workload.double_all([]) == []


def test_values():
    assert workload.double_all([1, 2, 3]) == [2, 4, 6]


def test_order():
    assert workload.double_all([3, 1]) == [6, 2]
"""


def _spec(root: Path, **validation: object) -> WorkloadSpec:
    config = {
        "min_tests": 3,
        "sizes": ["small", "medium"],
        "seeds": [1, 2],
        "edge_cases": [ValidationCase(name="single", input=[5])],
        "error_cases": [ValidationCase(name="none", input=None, expected_exception="TypeError")],
        **validation,
    }
    return WorkloadSpec(
        id="w",
        project_id="p",
        slug="mini",
        name="mini",
        description="",
        entrypoint="workload:double_all",
        fixture_id="fixtures:make_input",
        invocation_mode=InvocationMode.SINGLE,
        expected_output=OutputContract(type="list[int]"),
        timeout_seconds=3,
        memory_limit_mb=512,
        workspace_path=str(root),
        default_size="small",
        sizes={"small": {"n": 20}, "medium": {"n": 200}},
        seed=1,
        validation=ValidationConfig.model_validate(config),
    )


@pytest.fixture
def original(tmp_path: Path) -> Path:
    root = tmp_path / "original" / "mini"
    (root / "tests").mkdir(parents=True)
    (root / "workload.py").write_text(ORIGINAL.lstrip())
    (root / "fixtures.py").write_text(FIXTURES.lstrip())
    (root / "tests" / "test_workload.py").write_text(TESTS.lstrip())
    return root


def _validate(
    tmp_path: Path, original: Path, candidate: str, **validation: object
) -> dict[str, CheckResult]:
    baseline = tmp_path / "baseline" / "mini"
    patched = tmp_path / "patched" / "mini"
    shutil.copytree(original, baseline)
    shutil.copytree(original, patched)
    (patched / "workload.py").write_text(textwrap.dedent(candidate).lstrip())
    outcome = run_validation(
        ValidationInputs(
            spec=_spec(original, **validation),
            baseline_root=baseline,
            patched_root=patched,
            target_file="workload.py",
            target_function="double_all",
            run_dir=tmp_path / "run",
        )
    )
    checks = {check.name: check for check in outcome.checks}
    checks["__overall__"] = CheckResult(
        name="overall", required=False, status=outcome.status, detail=outcome.summary
    )
    assert outcome.required_checks_passed == (outcome.status == ValidationStatus.PASSED)
    if outcome.status == ValidationStatus.PASSED:
        assert outcome.baseline_input_hashes
    assert "check functional_tests" in outcome.log
    return checks


def test_equivalent_candidate_passes(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        """
        def double_all(items: list[int]) -> list[int]:
            return [item + item for item in items]
        """,
    )
    assert checks["__overall__"].status == ValidationStatus.PASSED, checks["__overall__"].detail
    for name in (
        "isolation",
        "compile",
        "functional_tests",
        "output_equivalence",
        "edge_cases",
        "exception_parity",
        "input_mutation",
        "patched_code_exercised",
    ):
        assert checks[name].status == ValidationStatus.PASSED, (name, checks[name].detail)
        assert checks[name].required
    assert len(checks["output_equivalence"].data["cases"]) == 4


def test_wrong_output_fails(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        """
        def double_all(items: list[int]) -> list[int]:
            return sorted(item * 2 for item in items)
        """,
    )
    assert checks["__overall__"].status == ValidationStatus.FAILED
    assert checks["functional_tests"].status == ValidationStatus.FAILED
    assert checks["functional_tests"].data["candidate"]["failures"]
    assert checks["output_equivalence"].status == ValidationStatus.FAILED
    assert "hashes differ" in checks["output_equivalence"].detail


def test_exception_parity_and_mutation_are_checked(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        """
        def double_all(items: list[int]) -> list[int]:
            if items is None:
                return []
            out = [item * 2 for item in items]
            items.clear()
            return out
        """,
    )
    assert checks["__overall__"].status == ValidationStatus.FAILED
    assert checks["exception_parity"].status == ValidationStatus.FAILED
    assert "returns instead of raising" in checks["exception_parity"].detail
    assert checks["input_mutation"].status == ValidationStatus.FAILED
    assert checks["output_equivalence"].status == ValidationStatus.PASSED


def test_input_mutation_may_be_permitted(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        """
        def double_all(items: list[int]) -> list[int]:
            out = [item * 2 for item in items]
            items.append(0)
            items.pop()
            items.reverse()
            items.reverse()
            return out
        """,
        allow_input_mutation=True,
    )
    assert not checks["input_mutation"].required
    assert checks["__overall__"].status == ValidationStatus.PASSED


def test_timeouts_are_inconclusive_not_passed(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        """
        import time


        def double_all(items: list[int]) -> list[int]:
            if len(items) > 100:
                time.sleep(30)
            return [item * 2 for item in items]
        """,
        sizes=["medium"],
        seeds=[1],
        edge_cases=[],
        error_cases=[],
    )
    assert checks["__overall__"].status == ValidationStatus.INCONCLUSIVE
    assert checks["output_equivalence"].status == ValidationStatus.INCONCLUSIVE
    assert "timed out" in checks["output_equivalence"].detail
    assert checks["functional_tests"].status == ValidationStatus.PASSED
    assert checks["edge_cases"].status == ValidationStatus.NOT_RUN
    assert not checks["edge_cases"].required


def test_insufficient_tests_are_inconclusive(tmp_path: Path, original: Path) -> None:
    checks = _validate(
        tmp_path,
        original,
        "def double_all(items: list[int]) -> list[int]:\n    return [i * 2 for i in items]\n",
        min_tests=10,
    )
    assert checks["functional_tests"].status == ValidationStatus.INCONCLUSIVE
    assert "insufficient coverage" in checks["functional_tests"].detail
    assert checks["__overall__"].status == ValidationStatus.INCONCLUSIVE


def test_extra_changes_in_the_workspace_fail_isolation(tmp_path: Path, original: Path) -> None:
    (original / "tests" / "test_workload.py").write_text(TESTS.lstrip())
    baseline = tmp_path / "baseline" / "mini"
    patched = tmp_path / "patched" / "mini"
    shutil.copytree(original, baseline)
    shutil.copytree(original, patched)
    (patched / "tests" / "test_workload.py").write_text("def test_nothing():\n    pass\n")
    outcome = run_validation(
        ValidationInputs(
            spec=_spec(original),
            baseline_root=baseline,
            patched_root=patched,
            target_file="workload.py",
            target_function="double_all",
            run_dir=tmp_path / "run",
        )
    )
    assert outcome.status == ValidationStatus.FAILED
    isolation = next(c for c in outcome.checks if c.name == "isolation")
    assert isolation.data["changed_files"] == ["tests/test_workload.py"]


def test_overall_status_rules() -> None:
    def check(status: ValidationStatus, required: bool = True) -> CheckResult:
        return CheckResult(name="c", required=required, status=status, detail="")

    passed, failed = ValidationStatus.PASSED, ValidationStatus.FAILED
    inconclusive, not_run = ValidationStatus.INCONCLUSIVE, ValidationStatus.NOT_RUN
    assert overall_status([]) == not_run
    assert overall_status([check(passed, required=False)]) == not_run
    assert overall_status([check(passed), check(failed, required=False)]) == passed
    assert overall_status([check(passed), check(inconclusive)]) == inconclusive
    assert overall_status([check(passed), check(not_run)]) == inconclusive
    assert overall_status([check(inconclusive), check(failed)]) == failed


def test_unordered_and_tolerance_contracts() -> None:
    unordered = OutputContract(type="list", comparison=OutputComparison.UNORDERED)
    base = {"output_sha256": "a", "output_value": [1, 2, 2, {"__tuple__": [1]}]}
    same = {"output_sha256": "b", "output_value": [{"__tuple__": [1]}, 2, 1, 2]}
    other = {"output_sha256": "c", "output_value": [1, 2, {"__tuple__": [1]}]}
    assert compare_outputs(base, same, unordered).equivalent
    result = compare_outputs(base, other, unordered)
    assert not result.equivalent
    assert "only in baseline" in result.detail
    tolerance = OutputContract(
        type="float", comparison=OutputComparison.FLOAT_TOLERANCE, rel_tol=0, abs_tol=1e-6
    )
    assert compare_outputs(
        {"output_sha256": "a", "output_value": [0.3]},
        {"output_sha256": "b", "output_value": [0.1 + 0.2]},
        tolerance,
    ).equivalent
    assert not compare_outputs(
        {"output_sha256": "a", "output_value": [0.3]},
        {"output_sha256": "b", "output_value": [0.31]},
        tolerance,
    ).equivalent
