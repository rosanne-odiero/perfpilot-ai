from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from backend.audit.rule_engine import run_rules
from backend.core.config import Settings
from backend.indexing.source import parse_file
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization import patch_generator
from backend.optimization.diffs import apply_file_patch, parse_unified_diff
from backend.optimization.patch_generator import (
    DeterministicPatchProposer,
    PatchProposal,
    PatchRequest,
    PatchTarget,
    SourceChangedError,
    generate_patch,
    register_patch_proposer,
    select_patch_proposer,
)
from backend.optimization.patch_validator import validate_patch

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
EXPECTED = {
    "duplicate_counter": ("PP-COLL-001", "list_count_to_counter", "items_counts[item]"),
    "list_lookup": ("PP-LOOP-001", "list_membership_to_set", "known_ids_set = set(known_ids)"),
    "nested_loop": (
        "PP-LOOP-002",
        "nested_loop_join_to_index",
        'orders_by_customer_id.get(customer["id"], ())',
    ),
    "string_builder": ("PP-STR-001", "string_concat_to_join", '"".join(report_parts)'),
    "regex_matching": (
        "PP-REGEX-001",
        "regex_compile_hoist",
        "_EXTRACT_ERROR_CODES_PATTERN.findall(line)",
    ),
}


def _target_from_rules(root: Path, relative: str = "workload.py") -> PatchTarget:
    module = parse_file(root, relative)
    findings = run_rules([module]).findings
    assert len(findings) == 1
    finding = findings[0].finding
    return PatchTarget(
        source_root=root,
        target_file=relative,
        target_function=finding.location.symbol,
        line_start=finding.location.line_start,
        line_end=finding.location.line_end,
        rule_id=finding.rule_id,
        expected_sha256=sha256_bytes((root / relative).read_bytes()),
    )


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_bundled_workloads_get_reviewable_patches(slug: str) -> None:
    root = EXAMPLES / slug
    original = (root / "workload.py").read_text()
    target = _target_from_rules(root)
    rule_id, transform, marker = EXPECTED[slug]
    assert target.rule_id == rule_id
    proposal = generate_patch(target)
    assert proposal.status == "proposed", proposal.reason
    assert proposal.transform == transform
    assert proposal.generated_by == "deterministic"
    assert proposal.original_sha256 == sha256_bytes(original.encode())
    assert proposal.original_source == original
    assert marker in proposal.patched_source
    # The diff applies to the ORIGINAL workload code and reproduces the patched file.
    (file_patch,) = parse_unified_diff(proposal.diff)
    assert apply_file_patch(original, file_patch) == proposal.patched_source
    assert proposal.patched_sha256 == sha256_bytes(proposal.patched_source.encode())
    compile(proposal.patched_source, "workload.py", "exec")
    # Produced by the platform, not copied from the bundled reference implementation.
    assert proposal.patched_source != (root / "reference" / "optimized.py").read_text()
    assert "REFERENCE IMPLEMENTATION" not in proposal.patched_source
    validated = validate_patch(proposal.diff, workspace_root=root, allowed_files=["workload.py"])
    assert validated.report.valid, validated.report.violations
    assert (root / "workload.py").read_text() == original
    assert proposal.notes
    assert proposal.summary


def _write(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "src"
    root.mkdir(exist_ok=True)
    (root / "mod.py").write_text(textwrap.dedent(source).lstrip())
    return root


def _propose(
    root: Path, rule_id: str, function: str = "f", line: int | None = None
) -> PatchProposal:
    tree = ast.parse((root / "mod.py").read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    start = line or node.body[-2].lineno
    return generate_patch(
        PatchTarget(
            source_root=root,
            target_file="mod.py",
            target_function=function,
            line_start=start,
            line_end=line or node.end_lineno or start,
            rule_id=rule_id,
        )
    )


def test_formatting_and_comments_are_preserved(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """
        # header comment
        def f(known: list[int], queries: list[int]) -> list[int]:
            out = []  # results
            for q in queries:  # loop comment
                if q in known and q not in known:  # odd but valid
                    out.append(q)
            return out
        """,
    )
    proposal = _propose(root, "PP-LOOP-001", line=5)
    assert proposal.status == "proposed", proposal.reason
    assert "# header comment" in proposal.patched_source
    assert "for q in queries:  # loop comment" in proposal.patched_source
    assert "if q in known_set and q not in known_set:  # odd but valid" in proposal.patched_source


@pytest.mark.parametrize(
    ("rule_id", "source", "reason"),
    [
        (
            "PP-LOOP-001",
            """
            def f(known, queries):
                return [q for q in queries if q in known]
            """,
            "",
        ),
        (
            "PP-LOOP-001",
            """
            def f(known, queries: list[int]) -> list[int]:
                out = []
                for q in queries:
                    if q in known:
                        out.append(q)
                return out
            """,
            "cannot prove parameter `known` is a list",
        ),
        (
            "PP-LOOP-001",
            """
            def f(known: str, queries: list[str]) -> list[str]:
                out = []
                for q in queries:
                    if q in known:
                        out.append(q)
                return out
            """,
            "cannot prove parameter `known` is a list",
        ),
        (
            "PP-LOOP-001",
            """
            def f(known: list[int], queries: list[int]) -> list[int]:
                out = []
                for q in queries:
                    if q in known:
                        known.append(q + 1)
                    out.append(q)
                return out
            """,
            "used inside the loop in other ways",
        ),
        (
            "PP-LOOP-001",
            """
            KNOWN = [1, 2]

            def f(queries: list[int]) -> list[int]:
                out = []
                for q in queries:
                    if q in KNOWN:
                        out.append(q)
                return out
            """,
            "not a parameter or local variable",
        ),
        (
            "PP-COLL-001",
            """
            def f(items: list[str]) -> int:
                total = 0
                for item in items:
                    total += items.index(item)
                return total
            """,
            "only .count is rewritten",
        ),
        (
            "PP-STR-001",
            """
            def f(rows: list[str]) -> str:
                s = ""
                try:
                    for row in rows:
                        s += row
                except ValueError:
                    pass
                return s
            """,
            "inside try/with",
        ),
        (
            "PP-STR-001",
            """
            def f(rows: list[str]) -> str:
                s = ""
                for row in rows:
                    s += row
                    if len(s) > 10:
                        break
                return s
            """,
            "read or rebound inside the loop",
        ),
        (
            "PP-STR-001",
            """
            def f(rows: list[str], s: str) -> str:
                for row in rows:
                    s += row
                return s
            """,
            "not initialised to a string literal",
        ),
        (
            "PP-REGEX-001",
            """
            import re

            def f(lines: list[str], pattern: str) -> list[str]:
                out = []
                for line in lines:
                    out.extend(re.findall(pattern, line))
                return out
            """,
            "not a string literal",
        ),
        (
            "PP-LOOP-002",
            """
            def f(customers: list[dict], orders: list[dict]) -> list[float]:
                out = []
                for c in customers:
                    total = 0.0
                    for o in orders:
                        if o["cid"] == c["id"]:
                            total += o["amount"]
                        else:
                            total -= 0
                    out.append(total)
                return out
            """,
            "`else` branch",
        ),
        (
            "PP-LOOP-002",
            """
            def f(customers: list[dict], orders: list[dict]) -> list[float]:
                out = []
                for c in customers:
                    total = 0.0
                    for o in orders:
                        if o["cid"] == c["id"]:
                            o["cid"] = None
                    out.append(total)
                return out
            """,
            "modifies `o`",
        ),
        (
            "PP-LOOP-002",
            """
            def f(customers: list[dict], orders: list[dict]) -> list[float]:
                out = []
                for c in customers:
                    for o in orders:
                        if o["cid"] == c["id"]:
                            out.append(o)
                return out + [o]
            """,
            "read outside the inner loop",
        ),
        (
            "PP-LOOP-002",
            """
            def f(customers: list[dict], orders: list[dict]) -> list[float]:
                out = []
                for c in customers:
                    for o in orders:
                        if o.key() == c["id"]:
                            out.append(o)
                return out
            """,
            "join keys must be plain names",
        ),
        (
            "PP-LOOP-999",
            """
            def f(items: list[int]) -> int:
                return sum(items)
            """,
            "no deterministic transformation exists",
        ),
    ],
)
def test_unsafe_code_gets_no_patch(tmp_path: Path, rule_id: str, source: str, reason: str) -> None:
    root = _write(tmp_path, source)
    tree = ast.parse((root / "mod.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    proposal = generate_patch(
        PatchTarget(
            source_root=root,
            target_file="mod.py",
            target_function=function.name,
            line_start=function.lineno,
            line_end=function.end_lineno or function.lineno,
            rule_id=rule_id,
        )
    )
    assert proposal.status == "no_safe_patch"
    assert proposal.reason is not None
    assert proposal.reason.startswith("no safe deterministic patch")
    assert reason in proposal.reason
    assert proposal.diff == ""


def test_tabs_and_crlf_are_refused(tmp_path: Path) -> None:
    source = (
        "def f(a: list[int], q: list[int]) -> list[int]:\n\tout = []\n\tfor x in q:\n"
        "\t\tif x in a:\n\t\t\tout.append(x)\n\treturn out\n"
    )
    root = tmp_path / "tabs"
    root.mkdir()
    (root / "mod.py").write_text(source)
    proposal = _propose(root, "PP-LOOP-001", line=4)
    assert proposal.status == "no_safe_patch"
    assert "tab indentation" in (proposal.reason or "")
    (root / "mod.py").write_bytes(source.replace("\t", "    ").replace("\n", "\r\n").encode())
    assert "CR/CRLF" in (_propose(root, "PP-LOOP-001", line=4).reason or "")


def test_hash_is_verified_before_generating(tmp_path: Path) -> None:
    root = _write(tmp_path, "def f(items: list[int]) -> int:\n    return len(items)\n")
    target = PatchTarget(
        source_root=root,
        target_file="mod.py",
        line_start=1,
        line_end=2,
        rule_id="PP-LOOP-001",
        expected_sha256="0" * 64,
    )
    with pytest.raises(SourceChangedError):
        generate_patch(target)


@pytest.mark.parametrize("bad", ["../mod.py", "/etc/passwd", "a\\b.py"])
def test_target_paths_are_contained(tmp_path: Path, bad: str) -> None:
    root = _write(tmp_path, "X = 1\n")
    from backend.core.exceptions import SecurityViolationError

    with pytest.raises(SecurityViolationError):
        generate_patch(
            PatchTarget(source_root=root, target_file=bad, line_start=1, line_end=1, rule_id="x")
        )


def test_counter_import_reuses_existing_binding_and_avoids_collisions(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """
        import collections as col


        def f(items: list[str]) -> list[int]:
            out = []
            for item in items:
                out.append(items.count(item))
            return out
        """,
    )
    proposal = _propose(root, "PP-COLL-001", line=7)
    assert proposal.status == "proposed", proposal.reason
    assert "items_counts = col.Counter(items)" in proposal.patched_source
    assert proposal.patched_source.count("import") == 1


def test_string_builder_keeps_annotation_and_initial_value(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """
        def f(rows: list[str]) -> str:
            s: str = "header\\n"
            for row in rows:
                s = s + row
            return s
        """,
    )
    proposal = _propose(root, "PP-STR-001", line=4)
    assert proposal.status == "proposed", proposal.reason
    namespace: dict[str, object] = {}
    exec(compile(proposal.patched_source, "mod.py", "exec"), namespace)
    assert namespace["f"](["a", "b"]) == "header\nab"  # type: ignore[operator]
    assert 's: str = "".join(s_parts)' in proposal.patched_source


def test_regex_flags_move_into_compile(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """
        import re


        def f(lines: list[str]) -> list[str]:
            out = []
            for line in lines:
                out.append(re.sub(r"a+", "-", line, 1, flags=re.I | re.M))
            return out
        """,
    )
    proposal = _propose(root, "PP-REGEX-001", line=7)
    assert proposal.status == "proposed", proposal.reason
    assert '_F_PATTERN = re.compile(r"a+", re.I | re.M)' in proposal.patched_source
    assert '_F_PATTERN.sub("-", line, 1)' in proposal.patched_source
    namespace: dict[str, object] = {}
    exec(compile(proposal.patched_source, "mod.py", "exec"), namespace)
    assert namespace["f"](["AAb", "xaa"]) == ["-b", "x-"]  # type: ignore[operator]


def test_proposer_registry(settings: Settings) -> None:
    assert isinstance(select_patch_proposer(settings), DeterministicPatchProposer)

    class Fake:
        name = "fake-model"

        def propose(self, request: PatchRequest) -> PatchProposal:
            raise AssertionError("not called")

    try:
        register_patch_proposer(lambda _settings: Fake())
        assert select_patch_proposer(settings).name == "fake-model"
        register_patch_proposer(lambda _settings: None)
        assert select_patch_proposer(settings).name == "deterministic"
    finally:
        register_patch_proposer(None)
    assert patch_generator.DETERMINISTIC == "deterministic"
