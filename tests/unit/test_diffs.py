from __future__ import annotations

import random

import pytest

from backend.optimization.diffs import (
    DiffParseError,
    PatchApplyError,
    apply_file_patch,
    make_unified_diff,
    parse_unified_diff,
)


def _roundtrip(original: str, patched: str, context: int = 3) -> str:
    diff = make_unified_diff("f.py", original, patched, context=context)
    (patch,) = parse_unified_diff(diff)
    return apply_file_patch(original, patch)


def test_randomized_roundtrip() -> None:
    rng = random.Random(7)
    words = ["a", "b", "c", "", "x = 1", "  indented"]
    checked = 0
    for _ in range(2000):
        texts = []
        for _side in range(2):
            text = "\n".join(rng.choice(words) for _ in range(rng.randint(0, 8)))
            if text and rng.random() < 0.5:
                text += "\n"
            texts.append(text)
        original, patched = texts
        if original == patched:
            continue
        assert _roundtrip(original, patched, context=rng.randint(0, 3)) == patched
        checked += 1
    assert checked > 1000


def test_no_newline_markers() -> None:
    assert _roundtrip("a\nb", "a\nc") == "a\nc"
    assert _roundtrip("a\nb", "a\nb\n") == "a\nb\n"
    assert _roundtrip("a\nb\n", "a\nb") == "a\nb"
    diff = make_unified_diff("f.py", "a\nb", "a\nc")
    assert "\\ No newline at end of file" in diff


def test_git_headers_and_paths() -> None:
    diff = "diff --git a/x.py b/x.py\nindex abc1234..def5678\n" + make_unified_diff(
        "x.py", "1\n", "2\n"
    )
    (patch,) = parse_unified_diff(diff)
    assert (patch.old_path, patch.new_path) == ("x.py", "x.py")
    assert patch.git_paths == ("a/x.py", "b/x.py")
    assert not patch.mode_lines
    with pytest.raises(DiffParseError):
        parse_unified_diff("--- x.py\n+++ x.py\n@@ -1 +1 @@\n-1\n+2\n")


def test_apply_rejects_mismatch_and_overlap() -> None:
    (patch,) = parse_unified_diff(make_unified_diff("f.py", "a\nb\nc\n", "a\nB\nc\n"))
    with pytest.raises(PatchApplyError):
        apply_file_patch("a\nX\nc\n", patch)
    overlapping = "--- a/f.py\n+++ b/f.py\n@@ -2 +2 @@\n-b\n+B\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
    (bad,) = parse_unified_diff(overlapping)
    with pytest.raises(PatchApplyError):
        apply_file_patch("a\nb\nc\n", bad)
