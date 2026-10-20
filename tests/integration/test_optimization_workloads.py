from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from backend.audit.rule_engine import run_rules
from backend.core.config import Settings
from backend.core.exceptions import SecurityViolationError, ValidationFailedError
from backend.indexing.source import parse_file
from backend.models.orm.workloads import Workload
from backend.models.workloads import WorkloadSpec
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_generator import PatchTarget, generate_patch
from backend.optimization.state_machine import ValidationStatus
from backend.optimization.validation import ValidationInputs, run_validation
from backend.optimization.workspace import (
    OriginalTreeChangedError,
    candidate_dir,
    prepare_workspace,
)
from backend.services.source_snapshots import hash_tree
from backend.services.workloads import build_spec, load_manifest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SLUGS = ["duplicate_counter", "list_lookup", "nested_loop", "regex_matching", "string_builder"]


def _spec(root: Path) -> WorkloadSpec:
    manifest = load_manifest(root)
    return build_spec(
        Workload(
            id="w",
            project_id="p",
            slug=root.name,
            name=manifest.name,
            description="",
            workspace_path=str(root),
            manifest=manifest.model_dump(mode="json"),
        )
    )


def _proposal(root: Path) -> tuple[str, str | None, str]:
    finding = run_rules([parse_file(root, "workload.py")]).findings[0].finding
    proposal = generate_patch(
        PatchTarget(
            source_root=root,
            target_file="workload.py",
            target_function=finding.location.symbol,
            line_start=finding.location.line_start,
            line_end=finding.location.line_end,
            rule_id=finding.rule_id,
        )
    )
    assert proposal.status == "proposed", proposal.reason
    return proposal.diff, proposal.patched_sha256, str(proposal.target_function)


@pytest.mark.parametrize("slug", SLUGS)
def test_generated_patches_pass_full_validation(settings: Settings, slug: str) -> None:
    root = (EXAMPLES / slug).resolve()
    tree_before, _ = hash_tree(root)
    diff, patched_sha, function = _proposal(root)
    workspace = prepare_workspace(
        settings,
        candidate_id=str(uuid.uuid4()),
        slug=slug,
        original_root=root,
        original_tree_sha256=tree_before,
        diff=diff,
        allowed_files=["workload.py"],
        patched_file_sha256=patched_sha,
        extra_protected=["fixtures.py", "tests"],
    )
    assert workspace.root.is_relative_to(settings.workspaces_dir)
    assert workspace.patched_tree_sha256 != tree_before
    assert hash_tree(workspace.baseline_root)[0] == tree_before
    outcome = run_validation(
        ValidationInputs(
            spec=_spec(root),
            baseline_root=workspace.baseline_root,
            patched_root=workspace.patched_root,
            target_file="workload.py",
            target_function=function,
            run_dir=workspace.root / "run",
        )
    )
    assert outcome.status == ValidationStatus.PASSED, outcome.summary
    functional = next(c for c in outcome.checks if c.name == "functional_tests")
    assert functional.data["candidate"]["counts"]["passed"] >= 10
    assert hash_tree(root)[0] == tree_before


@pytest.fixture
def copied_workload(tmp_path: Path) -> Path:
    root = tmp_path / "examples" / "duplicate_counter"
    shutil.copytree(EXAMPLES / "duplicate_counter", root)
    return root


def test_original_tree_changes_are_detected(settings: Settings, copied_workload: Path) -> None:
    tree, _ = hash_tree(copied_workload)
    diff, patched_sha, _ = _proposal(copied_workload)
    (copied_workload / "fixtures.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(OriginalTreeChangedError):
        prepare_workspace(
            settings,
            candidate_id=str(uuid.uuid4()),
            slug="duplicate_counter",
            original_root=copied_workload,
            original_tree_sha256=tree,
            diff=diff,
            allowed_files=["workload.py"],
            patched_file_sha256=patched_sha,
        )


def test_workspace_refuses_invalid_patches_and_ids(
    settings: Settings, copied_workload: Path
) -> None:
    tree, _ = hash_tree(copied_workload)
    test_file = copied_workload / "tests" / "test_workload.py"
    text = test_file.read_text()
    weakened = make_unified_diff(
        "tests/test_workload.py", text, text.replace("assert ", "assert 1 or ")
    )
    with pytest.raises(ValidationFailedError):
        prepare_workspace(
            settings,
            candidate_id=str(uuid.uuid4()),
            slug="duplicate_counter",
            original_root=copied_workload,
            original_tree_sha256=tree,
            diff=weakened,
            allowed_files=["tests/test_workload.py"],
            patched_file_sha256=None,
        )
    assert test_file.read_text() == text
    for bad in ("../x", "not-a-uuid", "/abs"):
        with pytest.raises(SecurityViolationError):
            candidate_dir(settings, bad)
