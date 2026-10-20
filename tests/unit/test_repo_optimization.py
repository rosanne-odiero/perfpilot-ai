"""Unit tests for repository-optimization building blocks (no sandbox, no API)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from audit_support import SHOPFAST

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, SecurityViolationError, ValidationFailedError
from backend.infrastructure.artifact_store import sha256_bytes
from backend.ingestion.workspace import make_read_only
from backend.optimization.junit import CaseOutcome, parse_junit_bytes, parse_junit_file
from backend.optimization.patch_generator import PatchTarget, generate_patch
from backend.optimization.repo_benchmark import check_inputs, plan_order, timing_command
from backend.optimization.repo_candidates import _relative_to_workdir
from backend.optimization.repo_sandbox import CommandScope
from backend.optimization.repo_validation import (
    VariantOutcome,
    compare,
    function_first_line,
    pytest_command,
)
from backend.optimization.repo_workspace import (
    build_patched_copy,
    candidate_root,
    remove_candidate_workspace,
)
from backend.optimization.state_machine import ValidationStatus
from backend.optimization.workspace import OriginalTreeChangedError
from backend.sandbox.backend import SandboxLimits
from backend.services.source_snapshots import hash_tree

INVENTORY = "shopfast/inventory_sync.py"
CANDIDATE_ID = "0b0c3f5e-1d7a-4c1e-9a8f-2b3c4d5e6f70"


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    shutil.copytree(SHOPFAST, root, ignore=shutil.ignore_patterns("__pycache__", ".env"))
    return root


def _target(root: Path, rule_id: str, line_start: int, line_end: int, function: str) -> PatchTarget:
    return PatchTarget(
        source_root=root,
        target_file=INVENTORY,
        target_function=function,
        line_start=line_start,
        line_end=line_end,
        rule_id=rule_id,
        expected_sha256=sha256_bytes((root / INVENTORY).read_bytes()),
    )


# ----------------------------------------------------------------------------- patch engine


def test_nested_loop_join_on_shopfast_becomes_an_index(snapshot: Path) -> None:
    before = hash_tree(snapshot)
    proposal = generate_patch(_target(snapshot, "PP-LOOP-002", 11, 11, "reconcile"))
    assert proposal.status == "proposed", proposal.reason
    assert proposal.transform == "nested_loop_join_to_index"
    assert "for product in catalog:" in proposal.original_source
    compile(proposal.patched_source, INVENTORY, "exec")
    assert 'catalog_by_sku.get(row["sku"], ())' in proposal.patched_source
    assert hash_tree(snapshot) == before


def test_list_membership_on_shopfast_catalog(snapshot: Path) -> None:
    target = PatchTarget(
        source_root=snapshot,
        target_file="shopfast/catalog.py",
        target_function="filter_discontinued",
        line_start=41,
        line_end=41,
        rule_id="PP-LOOP-001",
        expected_sha256=sha256_bytes((snapshot / "shopfast/catalog.py").read_bytes()),
    )
    proposal = generate_patch(target)
    assert proposal.status == "proposed", proposal.reason
    assert "set(discontinued_skus)" in proposal.patched_source


def test_mutated_list_membership_has_no_safe_patch(snapshot: Path) -> None:
    proposal = generate_patch(_target(snapshot, "PP-LOOP-001", 33, 33, "restock_order"))
    assert proposal.status == "no_safe_patch"
    assert proposal.reason is not None
    assert proposal.reason.startswith("no safe deterministic patch")


# ----------------------------------------------------------------------------- workspace


def _diff(snapshot: Path) -> tuple[str, str]:
    proposal = generate_patch(_target(snapshot, "PP-LOOP-002", 11, 11, "reconcile"))
    assert proposal.patched_sha256 is not None
    return proposal.diff, proposal.patched_sha256


def test_patched_copy_is_copy_on_write_and_leaves_the_snapshot_untouched(
    settings: Settings, snapshot: Path
) -> None:
    before = hash_tree(snapshot)
    diff, patched_sha = _diff(snapshot)
    copy = build_patched_copy(
        settings,
        candidate_id=CANDIDATE_ID,
        snapshot_root=snapshot,
        original_tree_sha256=before[0],
        diff=diff,
        target_file=INVENTORY,
        patched_file_sha256=patched_sha,
    )
    assert copy.src == candidate_root(settings, CANDIDATE_ID) / "src"
    assert hash_tree(snapshot) == before
    after = hash_tree(copy.src)[1]
    assert {p for p in after if after[p] != before[1].get(p)} == {INVENTORY}
    assert sha256_bytes((copy.src / INVENTORY).read_bytes()) == patched_sha
    assert os.stat(copy.src / INVENTORY).st_nlink == 1
    assert copy.hard_linked_files + copy.copied_files == len(before[1])
    if copy.hard_linked_files:
        assert os.stat(snapshot / "shopfast/catalog.py").st_nlink == 2


def test_read_only_snapshot_gives_a_read_only_copy_that_can_be_removed(
    settings: Settings, snapshot: Path
) -> None:
    diff, patched_sha = _diff(snapshot)
    make_read_only(snapshot)
    modes = {p: p.stat().st_mode for p in snapshot.rglob("*")}
    copy = build_patched_copy(
        settings,
        candidate_id=CANDIDATE_ID,
        snapshot_root=snapshot,
        original_tree_sha256=hash_tree(snapshot)[0],
        diff=diff,
        target_file=INVENTORY,
        patched_file_sha256=patched_sha,
    )
    assert not os.access(copy.src / "shopfast", os.W_OK)
    assert not os.access(copy.src / INVENTORY, os.W_OK)
    assert {p: p.stat().st_mode for p in snapshot.rglob("*")} == modes
    remove_candidate_workspace(settings, CANDIDATE_ID)
    assert not copy.root.exists()
    assert hash_tree(snapshot)[1][INVENTORY] == sha256_bytes((snapshot / INVENTORY).read_bytes())


def test_patched_copy_refuses_a_changed_snapshot(settings: Settings, snapshot: Path) -> None:
    before = hash_tree(snapshot)[0]
    diff, patched_sha = _diff(snapshot)
    (snapshot / "shopfast/utils.py").write_text("# changed\n")
    with pytest.raises(OriginalTreeChangedError):
        build_patched_copy(
            settings,
            candidate_id=CANDIDATE_ID,
            snapshot_root=snapshot,
            original_tree_sha256=before,
            diff=diff,
            target_file=INVENTORY,
            patched_file_sha256=patched_sha,
        )


def test_patched_copy_revalidates_the_diff(settings: Settings, snapshot: Path) -> None:
    diff, _sha = _diff(snapshot)
    with pytest.raises(ValidationFailedError):
        build_patched_copy(
            settings,
            candidate_id=CANDIDATE_ID,
            snapshot_root=snapshot,
            original_tree_sha256=hash_tree(snapshot)[0],
            diff=diff,
            target_file=INVENTORY,
            patched_file_sha256=None,
            extra_protected=[INVENTORY],
        )
    with pytest.raises(ConflictError):
        build_patched_copy(
            settings,
            candidate_id=CANDIDATE_ID,
            snapshot_root=snapshot,
            original_tree_sha256=hash_tree(snapshot)[0],
            diff=diff,
            target_file=INVENTORY,
            patched_file_sha256="0" * 64,
        )


@pytest.mark.parametrize("bad", ["../x", "not-a-uuid", f"{CANDIDATE_ID}/.."])
def test_candidate_root_is_contained(settings: Settings, bad: str) -> None:
    with pytest.raises(SecurityViolationError):
        candidate_root(settings, bad)


def test_command_paths_are_normalized_inside_the_workdir() -> None:
    assert _relative_to_workdir(".", "benchmarks/run.py") == "benchmarks/run.py"
    assert _relative_to_workdir("svc", "./tests") == "svc/tests"
    assert _relative_to_workdir("svc/app", "../bench.py") == "svc/bench.py"


# ----------------------------------------------------------------------------- junit


JUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_a" name="test_ok" time="0.01"/>
<testcase classname="tests.test_a" name="test_fail"><failure message="x">tb</failure></testcase>
<testcase classname="tests.test_a" name="test_err"><error message="boom"/></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped message="later"/></testcase>
<testcase classname="tests.test_a" name="test_ok"><system-out>dup</system-out></testcase>
</testsuite></testsuites>"""


def test_junit_outcomes_are_parsed() -> None:
    report = parse_junit_bytes(JUNIT)
    assert report.outcomes == {
        "tests.test_a::test_ok": CaseOutcome.PASSED,
        "tests.test_a::test_fail": CaseOutcome.FAILED,
        "tests.test_a::test_err": CaseOutcome.ERROR,
        "tests.test_a::test_skip": CaseOutcome.SKIPPED,
        "tests.test_a::test_ok#2": CaseOutcome.PASSED,
    }
    assert report.counts == {"passed": 2, "failed": 1, "error": 1, "skipped": 1, "total": 5}


@pytest.mark.parametrize(
    "document",
    [
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><t>&b;</t>',
        b'<?xml version="1.0"?><!DOCTYPE t SYSTEM "file:///etc/passwd"><t/>',
        b'<!DOCTYPE t [<!ENTITY % p SYSTEM "http://example.test/x">%p;]><t/>',
        b"<testsuite><testcase",
    ],
)
def test_hostile_or_broken_junit_is_refused(document: bytes) -> None:
    with pytest.raises(ValidationFailedError):
        parse_junit_bytes(document)


def test_junit_size_and_type_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.optimization.junit as junit

    monkeypatch.setattr(junit, "MAX_JUNIT_BYTES", 100)
    big = tmp_path / "junit.xml"
    big.write_bytes(JUNIT)
    with pytest.raises(ValidationFailedError, match="larger than"):
        parse_junit_file(big)
    link = tmp_path / "link.xml"
    link.symlink_to(big)
    with pytest.raises(ValidationFailedError, match="missing"):
        parse_junit_file(link)
    monkeypatch.setattr(junit, "MAX_JUNIT_BYTES", 1 << 20)
    monkeypatch.setattr(junit, "MAX_TESTCASES", 2)
    with pytest.raises(ValidationFailedError, match="more than 2 tests"):
        parse_junit_bytes(JUNIT)


# ----------------------------------------------------------------------------- decisions


def _outcome(
    variant: str, *, exit_code: int = 0, failure: str | None = None, timed_out: bool = False
) -> VariantOutcome:
    return VariantOutcome(
        variant=variant,
        failure=failure,
        exit_code=0,
        command_exit_code=exit_code,
        timed_out=timed_out,
        oom_killed=False,
        duration_s=1.0,
        counts={},
        junit_error=None,
    )


def _junit(**outcomes: str) -> bytes:
    cases = "".join(
        f'<testcase classname="t" name="{name}">'
        + {"passed": "", "failed": "<failure/>", "error": "<error/>", "skipped": "<skipped/>"}[o]
        + "</testcase>"
        for name, o in outcomes.items()
    )
    return f"<testsuite>{cases}</testsuite>".encode()


def test_all_original_passes_kept_is_passed() -> None:
    report = parse_junit_bytes(_junit(a="passed", b="passed"))
    status, checks, comparison = compare(_outcome("o"), _outcome("p"), report, report)
    assert status == ValidationStatus.PASSED
    assert comparison["regression_count"] == 0
    assert [c.name for c in checks] == ["original_suite", "patched_suite"]


@pytest.mark.parametrize("patched_case", ["failed", "error", "skipped", None])
def test_a_regressed_test_fails_validation(patched_case: str | None) -> None:
    original = parse_junit_bytes(_junit(a="passed", b="passed"))
    patched = parse_junit_bytes(_junit(a="passed", **({"b": patched_case} if patched_case else {})))
    failure = None if patched_case is None else "the command exited with code 1"
    status, _checks, comparison = compare(
        _outcome("o"), _outcome("p", exit_code=1, failure=failure), original, patched
    )
    assert status == ValidationStatus.FAILED
    assert comparison["regressions"] == [{"test": "t::b", "patched": patched_case or "missing"}]


def test_failing_original_suite_is_inconclusive_unless_the_patch_breaks_more() -> None:
    original = parse_junit_bytes(_junit(a="passed", b="failed"))
    failing = _outcome("o", exit_code=1, failure="the command exited with code 1")
    same = _outcome("p", exit_code=1, failure="the command exited with code 1")
    status, _c, _d = compare(failing, same, original, original)
    assert status == ValidationStatus.INCONCLUSIVE
    worse = parse_junit_bytes(_junit(a="failed", b="failed"))
    status, _c, _d = compare(failing, same, original, worse)
    assert status == ValidationStatus.FAILED


def test_timeouts_decide_by_which_side_timed_out() -> None:
    report = parse_junit_bytes(_junit(a="passed"))
    timed = _outcome("o", failure="the run exceeded its wall-clock limit", timed_out=True)
    status, _c, _d = compare(timed, _outcome("p"), None, report)
    assert status == ValidationStatus.INCONCLUSIVE
    status, _c, _d = compare(_outcome("o"), timed, report, None)
    assert status == ValidationStatus.FAILED


def test_no_passing_test_is_inconclusive() -> None:
    report = parse_junit_bytes(_junit(a="skipped"))
    status, _c, _d = compare(_outcome("o"), _outcome("p"), report, report)
    assert status == ValidationStatus.INCONCLUSIVE


def test_pytest_command_only_adds_a_report() -> None:
    command = pytest_command(["python", "-m", "pytest", "-q", "tests", "-k", "sync"])
    assert command["kind"] == "pytest"
    assert command["target"] == "tests"
    assert command["args"] == ["-q", "-k", "sync", "--junitxml=/out/junit.xml"]
    assert command["stats_json"] is True
    with pytest.raises(ConflictError):
        pytest_command(["python", "benchmarks/run_workload.py"])


def test_function_first_line_includes_decorators() -> None:
    source = (
        "import functools\n\n\n@functools.wraps(print)\n@staticmethod\ndef f():\n    return 1\n"
    )
    assert function_first_line(source, "f", 7) == 4
    moved = "PATTERN = 1\n\n\ndef g():\n    pass\n\n\ndef f():\n    return 1\n"
    assert function_first_line(moved, "f", 5) == 8
    with pytest.raises(ValueError, match="not found"):
        function_first_line(moved, "h", 5)


# ----------------------------------------------------------------------------- benchmark plan


def test_plan_is_paired_seeded_and_mixes_the_order() -> None:
    steps = plan_order(1, 10, seed=3)
    assert steps == plan_order(1, 10, seed=3)
    assert len(steps) == 22
    assert [s.warmup for s in steps[:2]] == [True, True]
    pairs = [steps[i : i + 2] for i in range(2, 22, 2)]
    assert all({a.variant, b.variant} == {"baseline", "candidate"} for a, b in pairs)
    assert all(a.pair_index == b.pair_index for a, b in pairs)
    firsts = {a.variant for a, _b in pairs}
    assert firsts == {"baseline", "candidate"}
    assert plan_order(0, 10, seed=4) != plan_order(0, 10, seed=3)


def test_timing_command_disables_every_profiler() -> None:
    command = timing_command(["python", "benchmarks/run_workload.py"])
    assert command == {
        "kind": "script",
        "target": "benchmarks/run_workload.py",
        "args": [],
        "trace_memory": False,
        "stats_json": False,
        "profile": False,
    }


def test_inputs_must_differ_only_in_the_patched_file(tmp_path: Path, snapshot: Path) -> None:
    scope = CommandScope(
        approval_id="a",
        argv=["python", "benchmarks/run_workload.py"],
        workdir_rel=".",
        image="perfpilot-sandbox:latest",
        limits=SandboxLimits(),
    )
    patched = tmp_path / "patched"
    shutil.copytree(snapshot, patched)
    (patched / INVENTORY).write_text((snapshot / INVENTORY).read_text() + "\n# patched\n")
    ok = check_inputs(snapshot, patched, INVENTORY, None, scope)
    assert ok.identical
    assert ok.record["differing_files"] == [INVENTORY]
    assert ok.record["baseline"]["argv"] == ok.record["candidate"]["argv"]

    (patched / "benchmarks/run_workload.py").write_text("print('fast')\n")
    refused = check_inputs(snapshot, patched, INVENTORY, None, scope)
    assert not refused.identical
    assert "benchmarks/run_workload.py" in refused.reasons[0]
