from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_validator import protected_category, validate_patch

ORIGINAL = '''"""Workload."""

from __future__ import annotations


def count(items: list[str]) -> int:
    total = 0
    for item in items:
        total += len(item)
    return total
'''
GOOD = ORIGINAL.replace(
    "    total = 0\n    for item in items:\n        total += len(item)\n    return total\n",
    "    return sum(len(item) for item in items)\n",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "tests").mkdir(parents=True)
    (root / "workload.py").write_text(ORIGINAL)
    (root / "fixtures.py").write_text("def make_input(seed=0):\n    return ['a']\n")
    (root / "tests" / "test_workload.py").write_text("def test_x():\n    assert True\n")
    (root / "workload.json").write_text("{}\n")
    (root / ".env").write_text("TOKEN=x\n")
    (root / "helper.py").write_text("X = 1\n")
    return root


def _codes(
    diff: str, root: Path, allowed: tuple[str, ...] = ("workload.py",), **kw: object
) -> set[str]:
    result = validate_patch(diff, workspace_root=root, allowed_files=allowed, **kw)  # type: ignore[arg-type]
    if result.report.valid:
        return set()
    return {v.code for v in result.report.violations}


def _rewrite(path: str, old: str, new: str) -> str:
    return make_unified_diff(path, old, new)


def test_valid_patch_passes_and_reports_patched_text(workspace: Path) -> None:
    diff = make_unified_diff("workload.py", ORIGINAL, GOOD)
    result = validate_patch(diff, workspace_root=workspace, allowed_files=["workload.py"])
    assert result.report.valid, result.report.violations
    assert result.patched["workload.py"] == GOOD
    assert result.report.files == ["workload.py"]
    assert set(result.report.checks.values()) == {"passed"}
    assert (workspace / "workload.py").read_text() == ORIGINAL  # never modified


@pytest.mark.parametrize(
    ("path", "category"),
    [
        (".env", "secrets"),
        ("config/.env.production", "secrets"),
        ("deploy/server.pem", "secrets"),
        ("id_rsa", "secrets"),
        (".github/workflows/ci.yml", "ci"),
        (".gitlab-ci.yml", "ci"),
        ("CODEOWNERS", "security_config"),
        (".pre-commit-config.yaml", "security_config"),
        (".git/config", "vcs"),
        ("workload.json", "benchmark_harness"),
        ("conftest.py", "benchmark_harness"),
        ("benchmarks/run.py", "benchmark_harness"),
        ("pkg/sitecustomize.py", "benchmark_harness"),
        ("fixtures.py", "fixtures"),
        ("tests/fixtures/data.json", "fixtures"),
        ("reference/optimized.py", "fixtures"),
        ("tests/test_workload.py", "tests"),
        ("pkg/test_utils.py", "tests"),
        ("pkg/utils_test.py", "tests"),
        ("pyproject.toml", "build_config"),
        ("requirements-dev.txt", "build_config"),
        ("uv.lock", "build_config"),
        ("workload.py", None),
        ("pkg/service.py", None),
    ],
)
def test_protected_categories(path: str, category: str | None) -> None:
    assert protected_category(path) == category


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("tests/test_workload.py", "def test_x():\n    assert True\n"),
        ("fixtures.py", "def make_input(seed=0):\n    return ['a']\n"),
        ("workload.json", "{}\n"),
        (".env", "TOKEN=x\n"),
    ],
)
def test_protected_files_are_rejected_even_when_allowed(
    workspace: Path, relative: str, content: str
) -> None:
    diff = _rewrite(relative, content, content + "# changed\n")
    assert "protected_path" in _codes(diff, workspace, allowed=(relative,))


def test_files_outside_the_allow_list_are_rejected(workspace: Path) -> None:
    diff = _rewrite("helper.py", "X = 1\n", "X = 2\n")
    assert _codes(diff, workspace) == {"not_allowed"}


@pytest.mark.parametrize(
    "header_path",
    [
        "/etc/passwd",
        "../outside.py",
        "pkg/../../x.py",
        "a\\b.py",
        "./workload.py",
        "x//y.py",
        "~/x.py",
    ],
)
def test_unsafe_paths(workspace: Path, header_path: str) -> None:
    diff = f"--- a/{header_path}\n+++ b/{header_path}\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"
    if header_path.startswith("/"):
        diff = f"--- {header_path}\n+++ {header_path}\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"
        assert "malformed_diff" in _codes(diff, workspace)
        return
    assert "unsafe_path" in _codes(diff, workspace, allowed=(header_path,))


def test_symlinked_target_and_directories_are_rejected(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("X = 1\n")
    os.symlink(outside, workspace / "link.py")
    (tmp_path / "realdir").mkdir()
    (tmp_path / "realdir" / "mod.py").write_text("X = 1\n")
    os.symlink(tmp_path / "realdir", workspace / "linkdir")
    for path in ("link.py", "linkdir/mod.py"):
        diff = _rewrite(path, "X = 1\n", "X = 2\n")
        assert "symlink" in _codes(diff, workspace, allowed=(path,))
    assert outside.read_text() == "X = 1\n"


def test_binary_mode_rename_new_and_deleted_files(workspace: Path) -> None:
    binary = "diff --git a/workload.py b/workload.py\nGIT binary patch\nliteral 0\nHcmV?d00001\n"
    assert "binary_patch" in _codes(binary, workspace)
    mode = (
        "diff --git a/workload.py b/workload.py\nold mode 100644\nnew mode 100755\n"
        + make_unified_diff("workload.py", ORIGINAL, GOOD)
    )
    assert "mode_change" in _codes(mode, workspace)
    index_mode = "diff --git a/workload.py b/workload.py\nindex 1234567..89abcde 100755\n"
    assert "mode_change" in _codes(
        index_mode + make_unified_diff("workload.py", ORIGINAL, GOOD), workspace
    )
    rename = "--- a/helper.py\n+++ b/workload.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"
    assert "rename_or_copy" in _codes(rename, workspace)
    new_file = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+X = 1\n"
    assert "new_file" in _codes(new_file, workspace, allowed=("new.py",))
    deleted = "--- a/helper.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-X = 1\n"
    assert "deleted_file" in _codes(deleted, workspace, allowed=("helper.py",))
    assert _codes(new_file, workspace, allowed=("new.py",), allow_new_files=True) == set()
    assert _codes(deleted, workspace, allowed=("helper.py",), allow_deleted_files=True) == set()


@pytest.mark.parametrize(
    "diff",
    [
        "",
        "not a diff\n",
        "--- a/workload.py\n@@ -1 +1 @@\n-x\n+y\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1,2 +1,2 @@\n-x\n+y\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1 +1 @@\n-x\n+y\n+z\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1 +1 @@\n-x\n?y\n",
        "--- a/workload.py\n+++ b/workload.py\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1 +1 @@\r\n-x\r\n+y\r\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1 +1 @@\n-x\n+y\x00\n",
        "--- workload.py\n+++ workload.py\n@@ -1 +1 @@\n-x\n+y\n",
        "--- a/workload.py\n+++ b/workload.py\n@@ -1 +1 @@\n-x\n+y",
    ],
)
def test_malformed_diffs_are_rejected(workspace: Path, diff: str) -> None:
    assert "malformed_diff" in _codes(diff, workspace)


def test_diff_must_apply_exactly(workspace: Path) -> None:
    stale = make_unified_diff("workload.py", ORIGINAL.replace("total = 0", "total = 1"), GOOD)
    assert "does_not_apply" in _codes(stale, workspace)


def test_patched_python_must_compile_and_keep_public_signatures(workspace: Path) -> None:
    broken = make_unified_diff(
        "workload.py", ORIGINAL, ORIGINAL.replace("return total", "return (")
    )
    assert "compile_error" in _codes(broken, workspace)
    changed = make_unified_diff(
        "workload.py", ORIGINAL, ORIGINAL.replace("items: list[str])", "items, limit=None)")
    )
    assert "signature_changed" in _codes(changed, workspace)
    removed = make_unified_diff(
        "workload.py", ORIGINAL, ORIGINAL.replace("def count(", "def _count(")
    )
    assert "public_api_removed" in _codes(removed, workspace)


@pytest.mark.parametrize(
    "patched",
    [
        GOOD.replace("def count(", "import functools\n\n\n@functools.lru_cache\ndef count("),
        GOOD.replace("def count(", "_CACHE = {}\n\n\ndef count("),
        GOOD.replace("    return sum(", "    global _SEEN\n    return sum("),
        GOOD.replace(
            "from __future__ import annotations", "from __future__ import annotations\nimport time"
        ),
        GOOD.replace("    return sum(", "    setattr(items, 'x', 1)\n    return sum("),
        GOOD.replace(
            "from __future__ import annotations", "from __future__ import annotations\nimport gc"
        ).replace("    return sum(", "    gc.disable()\n    return sum("),
        GOOD.replace("    return sum(", "    exec('pass')\n    return sum("),
    ],
)
def test_benchmark_tampering_is_rejected(workspace: Path, patched: str) -> None:
    diff = make_unified_diff("workload.py", ORIGINAL, patched)
    assert "benchmark_tampering" in _codes(diff, workspace)


def test_existing_constructs_are_not_flagged(tmp_path: Path) -> None:
    original = "import time\n\n_CACHE = {}\n\n\ndef f(x: int) -> int:\n    return x + 1\n"
    (tmp_path / "mod.py").write_text(original)
    diff = make_unified_diff("mod.py", original, original.replace("x + 1", "1 + x"))
    assert _codes(diff, tmp_path, allowed=("mod.py",)) == set()


def test_extra_protected_paths_and_duplicates(workspace: Path) -> None:
    diff = make_unified_diff("helper.py", "X = 1\n", "X = 2\n")
    assert "protected_path" in _codes(
        diff, workspace, allowed=("helper.py",), extra_protected=("helper.py",)
    )
    twice = diff + diff
    assert "duplicate_file" in _codes(twice, workspace, allowed=("helper.py",))


def test_missing_and_non_text_files(workspace: Path) -> None:
    diff = make_unified_diff("missing.py", "X = 1\n", "X = 2\n")
    assert "missing_file" in _codes(diff, workspace, allowed=("missing.py",))
    (workspace / "blob.py").write_bytes(b"\xff\xfe\x00X = 1\n")
    blob = "--- a/blob.py\n+++ b/blob.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"
    assert "not_text" in _codes(blob, workspace, allowed=("blob.py",))
