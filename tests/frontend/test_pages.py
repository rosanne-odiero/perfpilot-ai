"""AppTest smoke tests: every page renders against captured API responses without exceptions."""

from __future__ import annotations

from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from ui_fixtures import APP, FakeApi

from frontend import api_client
from frontend.api_client import UNREACHABLE, ApiError

AUDIT_TABS = [
    "Overview",
    "Files",
    "Findings",
    "Dependency graph",
    "AI insights",
    "Runtime",
    "Export",
]


def app(page: str | None = None, **query: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    if page:
        at.switch_page(page)
        at.run()
    return at


def text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [str(e.value) for e in (*at.info, *at.warning, *at.error, *at.success)]
    return "\n".join(parts)


def assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_landing_page_is_repository_audit(fake_api: FakeApi) -> None:
    at = app()
    assert_clean(at)
    body = text(at)
    assert "Audit a Python repository for" in body
    assert "No API key required" in body
    assert [t.label for t in at.tabs] == ["GitHub URL", "ZIP upload", "Local directory"]
    assert "shopfast" in body  # recent audits
    toggle = at.toggle(key="use_llm")
    assert toggle.disabled  # no LLM configured on the captured server
    assert "no LLM configured" in body
    for name in ("MEASURED", "STATIC", "AI_HYPOTHESIS"):
        assert name in body


def test_local_directory_hidden_when_server_disables_it(fake_api: FakeApi) -> None:
    options = dict(fake_api.routes["/import/options"], local_directory_enabled=False)
    fake_api.overrides["/import/options"] = options
    at = app()
    assert [t.label for t in at.tabs] == ["GitHub URL", "ZIP upload"]
    assert "Local directory import is disabled" in text(at)


def test_runtime_mode_explains_approval_and_unavailability(fake_api: FakeApi) -> None:
    at = app()
    at.radio(key="audit_mode").set_value("Runtime Audit").run()
    body = text(at)
    assert "approve one exact command line" in body
    assert "not available in this API version" in body
    start = next(b for b in at.button if b.label == "Start audit")
    assert start.disabled


def _start_local_import(at: AppTest, path: str) -> AppTest:
    # AppTest does not keep the active tab between runs, so select it before each run.
    at.session_state["import_source"] = "Local directory"
    at.run()
    next(t for t in at.text_input if t.label.startswith("Directory")).input(path)
    next(b for b in at.button if b.label == "Start audit").click()
    at.session_state["import_source"] = "Local directory"
    return at.run()


def test_start_runs_import_then_audit_and_links_results(fake_api: FakeApi) -> None:
    fake_api.post_responses["/repositories"] = {
        "job_id": "import-job",
        "status": "QUEUED",
        "subject_type": "snapshot",
        "subject_id": "snap-new",
    }
    fake_api.post_responses["/audits"] = {
        "job_id": "audit-job",
        "status": "QUEUED",
        "subject_type": "audit",
        "subject_id": "audit-new",
    }
    done = {"progress": 1.0, "message": "completed", "error": None, "error_details": None}
    fake_api.overrides["/jobs/import-job"] = {
        "id": "import-job",
        "job_type": "repository.ingest",
        "status": "SUCCEEDED",
        "stage": "finalize",
        "result": {"status": "READY"},
        **done,
    }
    fake_api.overrides["/jobs/audit-job"] = {
        "id": "audit-job",
        "job_type": "audit.run",
        "status": "SUCCEEDED",
        "stage": "COMPLETED",
        "result": {},
        **done,
    }
    at = _start_local_import(at=app(), path="/srv/repos/shopfast")
    for _ in range(3):
        at.run()
    assert_clean(at)
    assert fake_api.posts[0] == (
        "/repositories",
        {"source_type": "LOCAL_DIRECTORY", "local_path": "/srv/repos/shopfast"},
    )
    assert fake_api.posts[1] == (
        "/audits",
        {"snapshot_id": "snap-new", "mode": "STATIC", "use_llm": False, "llm_consent": False},
    )
    assert "Audit finished." in text(at)
    assert any(b.label == "Open audit results" for b in at.button)


def test_rejected_import_shows_the_error_envelope(fake_api: FakeApi) -> None:
    fake_api.post_responses["/repositories"] = ApiError(
        400, "security_violation", "path is outside the allowlist", {"path": "/etc"}
    )
    at = _start_local_import(app(), "/etc")
    assert not at.exception
    assert "path is outside the allowlist" in "\n".join(e.value for e in at.error)
    assert "audit_pipeline" not in at.session_state


def test_github_url_is_validated_before_calling_the_api(fake_api: FakeApi) -> None:
    at = app()
    at.text_input(key=at.text_input[0].key).input("git@github.com:acme/shop.git")
    next(b for b in at.button if b.label == "Start audit").click()
    at.run()
    assert fake_api.posts == []
    assert "public repository URL" in text(at)


def test_audit_results_every_tab(fake_api: FakeApi, audit_id: str) -> None:
    at = app("app_pages/audit_results.py", audit=audit_id)
    assert_clean(at)
    assert [t.label for t in at.tabs] == AUDIT_TABS
    overview = text(at)
    assert "Top risks by priority" in overview
    assert "Detected technologies" in overview
    for tab in AUDIT_TABS[1:]:
        at.session_state["audit_tab"] = tab
        at.run()
        assert_clean(at)
    at.session_state["audit_tab"] = "Findings"
    at.run()
    assert "24 of 24 findings" in text(at)
    at.multiselect(key="f_sev").set_value(["HIGH"])
    at.session_state["audit_tab"] = "Findings"  # AppTest does not keep the active tab
    at.run()
    assert "5 of 24 findings" in text(at)


def test_pending_ai_and_runtime_areas_show_not_available(fake_api: FakeApi, audit_id: str) -> None:
    at = app("app_pages/audit_results.py", audit=audit_id)
    for tab in ("AI insights", "Runtime"):
        at.session_state["audit_tab"] = tab
        at.run()
        assert "This capability is not available in this API version" in text(at)


def _recommendation(provenance: str) -> dict[str, Any]:
    return {
        "id": f"rec-{provenance}",
        "title": "Batch the order queries",
        "summary": "One query per order id.",
        "rationale": "N+1 pattern in order_summaries.",
        "proposed_strategy": "Fetch all orders with WHERE id IN (...).",
        "finding_ids": ["f1"],
        "citations": [{"file_path": "shopfast/orders.py", "line_start": 13, "line_end": 15}],
        "provenance": provenance,
        "effort": "SMALL",
        "uncertainty": "Depends on the number of ids.",
        "expected_effect": "Fewer round-trips.",
        "priority_rank": 1,
        "generated_by": "deterministic",
    }


def test_ai_recommendations_are_labeled_as_hypotheses(fake_api: FakeApi, audit_id: str) -> None:
    fake_api.overrides[f"/audits/{audit_id}/recommendations"] = [
        _recommendation("AI_HYPOTHESIS"),
        _recommendation("STATIC"),
    ]
    fake_api.overrides[f"/audits/{audit_id}/insights"] = []
    at = app("app_pages/audit_results.py", audit=audit_id)
    at.session_state["audit_tab"] = "AI insights"
    at.run()
    assert_clean(at)
    body = text(at)
    assert "Hypothesis — not measured" in body
    assert "Derived deterministically from the static findings" in body
    assert "`shopfast/orders.py:13-15`" in body


def test_workload_lab_sections(fake_api: FakeApi) -> None:
    at = app("app_pages/workload_lab.py")
    assert_clean(at)
    assert [t.label for t in at.tabs] == ["Explorer", "Profile", "Benchmark", "Optimize"]
    assert "Baseline vs bundled reference implementation" in text(at)
    at.session_state["lab_tab"] = "Profile"
    at.run()
    assert_clean(at)
    assert "Exclusive" in text(at)
    assert at.dataframe  # hotspot table
    at.session_state["lab_tab"] = "Benchmark"
    at.run()
    assert_clean(at)
    body = text(at)
    assert "IMPROVED" in body
    assert "reference implementation (bundled, for testing)" in body
    at.session_state["lab_tab"] = "Optimize"
    at.run()
    assert "Optimization workflow" in text(at)
    assert "not available in this API version" in text(at)


@pytest.mark.parametrize("page", ["app_pages/reports_history.py", "app_pages/system_status.py"])
def test_history_and_status_pages(fake_api: FakeApi, page: str) -> None:
    at = app(page)
    assert_clean(at)
    assert at.dataframe


def test_unreachable_api_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_: object, **__: object) -> object:
        raise ApiError(0, UNREACHABLE, "cannot reach the API at http://127.0.0.1:9")

    monkeypatch.setattr(api_client, "request", offline)
    st.cache_data.clear()
    for page in (None, "app_pages/audit_results.py", "app_pages/workload_lab.py"):
        at = app(page)
        assert not at.exception
        assert any("cannot reach the API" in e.value for e in at.error)
