from __future__ import annotations

from typing import Any

import pytest

from frontend.components.benchmark_charts import repo_samples_as_timing, timing_frame
from frontend.components.diff_view import parse_unified_diff, unified_diff_html
from frontend.components.findings_table import execution_path_html, measured_cards
from frontend.components.optimization import (
    allowed_actions,
    benchmark_block_reason,
    checks_frame,
    lifecycle_steps,
    repo_lifecycle_steps,
)
from frontend.components.report_export import latest_per_format
from frontend.components.runtime import (
    LooserLimitError,
    approval_body,
    commands_frame,
    limits_override,
)
from frontend.views.audit_ai import usage_cards
from frontend.views.history import experiments_frame
from frontend.views.repository_audit import ai_disclosure_html

DEFAULTS = {
    "cpus": 2.0,
    "memory_mb": 2048,
    "pids": 256,
    "timeout_seconds": 600,
    "tmpfs_mb": 256,
    "output_max_bytes": 16777216,
}
COMMAND = {"id": "c1", "argv": ["python", "-m", "pytest", "-q"], "kind": "pytest"}


def test_approval_body_pins_argv_and_omits_unchanged_limits() -> None:
    body = approval_body(
        COMMAND,
        approved_by="me",
        limits=dict(DEFAULTS),
        defaults=DEFAULTS,
        allow_dependency_install=False,
        allow_sdist_builds=True,
        trace_memory=False,
    )
    assert body == {
        "command_id": "c1",
        "argv": ["python", "-m", "pytest", "-q"],
        "approved_by": "me",
        "allow_dependency_install": False,
        "allow_sdist_builds": False,  # only meaningful with the install step
        "trace_memory": False,
    }
    assert body["argv"] is not COMMAND["argv"]


def test_limits_can_only_be_tightened() -> None:
    tighter = dict(DEFAULTS, cpus=1.0, memory_mb=512)
    assert limits_override(tighter, DEFAULTS) == {"cpus": 1.0, "memory_mb": 512}
    assert limits_override(dict(DEFAULTS), DEFAULTS) is None
    with pytest.raises(LooserLimitError):
        limits_override(dict(DEFAULTS, timeout_seconds=601), DEFAULTS)


def test_commands_frame() -> None:
    frame = commands_frame([{**COMMAND, "evidence": ["pyproject.toml:14"], "runnable": True}])
    assert frame.iloc[0]["Command"] == "python -m pytest -q"
    assert frame.iloc[0]["Evidence"] == "pyproject.toml:14"


def _candidate(state: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "approval": {"status": "PENDING"}, **extra}


def test_allowed_actions_never_offer_benchmarking_unvalidated_candidates() -> None:
    assert allowed_actions(_candidate("AWAITING_APPROVAL")) == {"approve", "reject"}
    approved = _candidate("AWAITING_APPROVAL", approval={"status": "APPROVED"})
    assert allowed_actions(approved) == {"validate", "reject"}
    assert allowed_actions(_candidate("VALIDATED", validation_status="PASSED")) == {"benchmark"}
    assert allowed_actions(_candidate("VALIDATED", validation_status="INCONCLUSIVE")) == set()
    for state in ("FAILED", "REJECTED", "VALIDATING", "BENCHMARKING", "COMPLETED"):
        assert "benchmark" not in allowed_actions(_candidate(state, validation_status="PASSED"))
    assert benchmark_block_reason(_candidate("VALIDATED", validation_status="PASSED")) is None
    reason = benchmark_block_reason(_candidate("AWAITING_APPROVAL", validation_status="NOT_RUN"))
    assert reason is not None
    assert "AWAITING_APPROVAL" in reason


def _states(steps: list[tuple[str, str]]) -> dict[str, str]:
    return dict(steps)


def test_workload_lifecycle_steps() -> None:
    assert _states(lifecycle_steps(None, None))["Profile"] == "active"
    analysis = {"state": "RECOMMENDED", "state_history": []}
    assert _states(lifecycle_steps(analysis, None))["Patch"] == "active"
    approved = _candidate("AWAITING_APPROVAL", approval={"status": "APPROVED"})
    steps = _states(lifecycle_steps(analysis, approved))
    assert steps["Approve"] == "done"
    assert steps["Validate"] == "active"
    assert set(_states(lifecycle_steps(analysis, _candidate("COMPLETED"))).values()) == {"done"}
    failed = {"state": "FAILED", "state_history": [{"to_state": "PROFILING"}]}
    assert _states(lifecycle_steps(failed, None))["Profile"] == "failed"
    rejected = _candidate("REJECTED", approval={"status": "REJECTED", "rejected_by": "me"})
    assert _states(lifecycle_steps(analysis, rejected))["Approve"] == "failed"


def test_repo_lifecycle_steps() -> None:
    pending = _states(repo_lifecycle_steps(_candidate("AWAITING_APPROVAL")))
    assert list(pending) == ["Finding", "Patch", "Approve", "Validate", "Benchmark", "Done"]
    assert pending["Finding"] == pending["Patch"] == "done"
    assert pending["Approve"] == "active"
    no_patch = _states(repo_lifecycle_steps(_candidate("FAILED", state_history=[])))
    assert no_patch["Patch"] == "failed"
    validating = {"from_state": "VALIDATING", "to_state": "FAILED"}
    failed_validation = _candidate("FAILED", state_history=[validating])
    assert _states(repo_lifecycle_steps(failed_validation))["Validate"] == "failed"
    assert set(_states(repo_lifecycle_steps(_candidate("COMPLETED"))).values()) == {"done"}


def test_repo_samples_exclude_warmups_and_pair_correctly() -> None:
    samples = [
        {"variant": "candidate", "warmup": True, "pair_index": 0, "sequence": 0, "wall_ns": 9e9,
         "cpu_ns": 1, "ok": True},
        {"variant": "baseline", "warmup": False, "pair_index": 0, "sequence": 2, "wall_ns": 2e6,
         "cpu_ns": 1e6, "ok": True},
        {"variant": "candidate", "warmup": False, "pair_index": 0, "sequence": 3, "wall_ns": 1e6,
         "cpu_ns": 5e5, "ok": True},
    ]  # fmt: skip
    frame = timing_frame(repo_samples_as_timing(samples))
    assert len(frame) == 2
    assert sorted(frame["wall_ms"]) == [1.0, 2.0]


def test_checks_frame_columns() -> None:
    frame = checks_frame(
        [{"name": "compile", "required": True, "status": "PASSED", "detail": "ok"}]
    )
    assert frame.iloc[0].to_dict()["Status"] == "PASSED"
    assert list(checks_frame([]).columns) == ["Check", "Required", "Status", "Detail", "Seconds"]


def test_parse_unified_diff_numbers_lines_and_handles_dash_content() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -3,3 +3,3 @@\n a\n--- b\n+++ c\n d\n"
    kinds = [(line.kind, line.old_no, line.new_no) for line in parse_unified_diff(diff)]
    assert kinds == [
        ("file", None, None),
        ("file", None, None),
        ("hunk", None, None),
        ("context", 3, 3),
        ("del", 4, None),
        ("add", None, 4),
        ("context", 5, 5),
    ]
    html = unified_diff_html(diff)
    assert "+1 / -1 lines" in html
    assert "x.py" in html
    assert "<script>" not in unified_diff_html("@@ -1 +1 @@\n-<script>\n+ok\n")


def test_finding_role_path_and_measured_cards() -> None:
    html = execution_path_html(
        {"code_role": "REQUEST_HANDLER", "execution_path": ["app:main", "<x>:y"]}
    )
    assert "Request handler" in html
    assert "<code>app:main</code> → <code>&lt;x&gt;:y</code>" in html
    assert execution_path_html({}) == ""
    cards = measured_cards(
        {"attributed_time_ms": 925.0, "attributed_percentage": 23.9, "internal_time_ms": 921.0,
         "cumulative_time_ms": 925.0, "call_count": 1, "measured_factor": 1.48}
    )  # fmt: skip
    assert [c.label for c in cards] == [
        "Attributed time",
        "Exclusive time",
        "Cumulative time",
        "Calls",
        "Priority factor",
    ]
    assert all(c.provenance == "MEASURED" for c in cards[:4])
    assert cards[4].provenance is None


def test_llm_usage_cards() -> None:
    cards = usage_cards(
        {"model": None, "calls": 0, "input_tokens": 0, "output_tokens": 0, "budget_tokens": 0,
         "rejected_citations": 2, "rejected_claims": 1, "rejected_items": 0, "fallbacks": 1}
    )  # fmt: skip
    by_label = {c.label: c for c in cards}
    assert by_label["Model"].value == "none"
    assert by_label["Rejected by guards"].value == "2 citations"
    assert by_label["Fallbacks"].value == "1"


def test_ai_disclosure_escapes_and_states_the_budget() -> None:
    html = ai_disclosure_html(
        {
            "model": "m<1>",
            "base_url_host": "llm.example.com",
            "data_sent": "snippets & summaries",
            "budget": {"max_tokens_per_audit": 60000, "max_tokens_per_call": 6000},
            "prompt_version": "system@1",
        }
    )
    assert "m&lt;1&gt; at llm.example.com" in html
    assert "snippets &amp; summaries" in html
    assert "60,000 tokens per audit" in html


def test_latest_report_per_format_in_canonical_order() -> None:
    reports = [
        {"format": "pdf", "created_at": "2026-01-01", "id": "old-pdf"},
        {"format": "pdf", "created_at": "2026-01-02", "id": "new-pdf"},
        {"format": "html", "created_at": "2026-01-01", "id": "html"},
    ]
    latest = latest_per_format(reports)
    assert list(latest) == ["html", "pdf"]
    assert latest["pdf"]["id"] == "new-pdf"


def test_experiments_frame() -> None:
    frame = experiments_frame(
        [
            {
                "kind": "reference_benchmark",
                "id": "b1",
                "workload_id": "w",
                "workload_name": "Duplicate counter",
                "title": "Reference",
                "state": "SUCCEEDED",
                "classification": "IMPROVED",
                "speedup": 33.1,
                "speedup_ci_low": 30.1,
                "speedup_ci_high": 34.9,
                "report_count": 2,
                "created_at": "2026-10-20T20:45:46",
            }
        ]
    )
    row = frame.iloc[0].to_dict()
    assert row["Kind"] == "reference benchmark"
    assert row["95% CI"] == "30.1\u00d7 to 34.9\u00d7"
    assert row["Validation"] == ""
