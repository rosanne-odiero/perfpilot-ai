"""AppTest coverage of the AI, runtime and optimization sections against captured responses."""

from __future__ import annotations

import copy
from typing import Any

from streamlit.testing.v1 import AppTest
from ui_fixtures import APP, FakeApi, load

from frontend.api_client import ApiError


def app(page: str, tab_key: str, tab: str, **query: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    at.switch_page(page)
    at.session_state[tab_key] = tab
    at.run()
    return at


def rerun(at: AppTest, tab_key: str, tab: str) -> AppTest:
    """AppTest does not keep the active tab between runs."""
    at.session_state[tab_key] = tab
    return at.run()


def text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [str(e.value) for e in (*at.info, *at.warning, *at.error, *at.success)]
    parts += [str(c.value) for c in at.code]
    return "\n".join(parts)


def assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def button(at: AppTest, label: str) -> Any:
    return next(b for b in at.button if b.label == label)


# --- AI ---------------------------------------------------------------------------------------


def test_ai_tab_shows_captured_recommendations_usage_and_insights(
    fake_api: FakeApi, audit_id: str
) -> None:
    at = app("app_pages/audit_results.py", "audit_tab", "AI insights", audit=audit_id)
    assert_clean(at)
    body = text(at)
    recs = fake_api.routes[f"/audits/{audit_id}/recommendations"]
    assert recs, "fixture must contain recommendations"
    assert recs[0]["title"] in body
    assert "deterministic advisor" in body
    assert "Derived deterministically from the static findings" in body
    assert "Hypothesis — not measured" not in body  # no model output without an LLM
    assert "Architecture insights" in body
    assert "LLM usage for this audit" in body
    assert "Llm: not requested" in body
    assert any(e.label.startswith("Suggested diff") for e in at.expander)


def test_citation_opens_the_file_viewer_at_the_cited_lines(
    fake_api: FakeApi, audit_id: str
) -> None:
    at = app("app_pages/audit_results.py", "audit_tab", "AI insights", audit=audit_id)
    cite = next(b for b in at.button if b.label.startswith("shopfast/api.py:19"))
    cite.click().run()
    assert at.session_state["audit_tab"] == "Files"
    assert at.session_state["files_focus"]["path"] == "shopfast/api.py"
    assert_clean(at)
    assert "Opened from a citation" in text(at)


# --- runtime ----------------------------------------------------------------------------------


def test_runtime_tab_shows_commands_runs_and_measured_results(
    fake_api: FakeApi, audit_id: str
) -> None:
    at = app("app_pages/audit_results.py", "audit_tab", "Runtime", audit=audit_id)
    assert_clean(at)
    body = text(at)
    assert "SANDBOX AVAILABLE" in body
    assert "Discovered commands (3)" in body
    assert "Findings confirmed by measurements" in body
    assert "Static claim" in body
    assert "Measured evidence" in body
    assert "Runtime-only hotspots" in body
    assert "PEAK RSS" in body.upper()
    assert "tracemalloc peak (Python allocations)" in body
    assert "section 7" in body
    assert len(at.dataframe) >= 4  # commands, runs, hotspots, runtime-only


def test_approval_pins_the_exact_argv_and_sends_only_tighter_limits(
    fake_api: FakeApi, audit_id: str
) -> None:
    commands = fake_api.routes[f"/audits/{audit_id}/runtime/commands"]
    script = next(c for c in commands if c["kind"] == "script")
    fake_api.post_responses[f"/audits/{audit_id}/runtime/approvals"] = {
        "job_id": "rt-job",
        "status": "QUEUED",
        "subject_type": "runtime_run",
        "subject_id": "rt-run",
    }
    fake_api.overrides["/jobs/rt-job"] = {
        "id": "rt-job",
        "job_type": "runtime.profile",
        "status": "RUNNING",
        "stage": "profiling",
        "progress": 0.4,
        "message": "running in the sandbox",
    }
    at = app("app_pages/audit_results.py", "audit_tab", "Runtime", audit=audit_id)
    at.selectbox(key=f"runtime_command_{audit_id}").set_value(script["id"])
    rerun(at, "audit_tab", "Runtime")
    key = f"approve_{script['id']}"
    assert "['python', 'benchmarks/run_workload.py']" in text(at)
    assert button(at, "Approve and run").disabled
    at.number_input(key=f"{key}_timeout_seconds").set_value(120)
    at.checkbox(key=f"{key}_confirm").check()
    rerun(at, "audit_tab", "Runtime")
    button(at, "Approve and run").click()
    rerun(at, "audit_tab", "Runtime")
    path, body = fake_api.posts[-1]
    assert path == f"/audits/{audit_id}/runtime/approvals"
    assert body == {
        "command_id": script["id"],
        "argv": script["argv"],
        "approved_by": "local-user",
        "allow_dependency_install": False,
        "allow_sdist_builds": False,
        "trace_memory": True,
        "limits_override": {"timeout_seconds": 120},
    }
    assert at.session_state[f"runtime_job_{audit_id}"] == "rt-job"


def test_dependency_install_warns_about_the_network(fake_api: FakeApi, audit_id: str) -> None:
    commands = fake_api.routes[f"/audits/{audit_id}/runtime/commands"]
    command = commands[0]
    at = app("app_pages/audit_results.py", "audit_tab", "Runtime", audit=audit_id)
    at.selectbox(key=f"runtime_command_{audit_id}").set_value(command["id"])
    rerun(at, "audit_tab", "Runtime")
    at.checkbox(key=f"approve_{command['id']}_deps").check()
    rerun(at, "audit_tab", "Runtime")
    assert "uses the network" in text(at)


def test_approval_rejection_is_shown(fake_api: FakeApi, audit_id: str) -> None:
    command = fake_api.routes[f"/audits/{audit_id}/runtime/commands"][0]
    fake_api.post_responses[f"/audits/{audit_id}/runtime/approvals"] = ApiError(
        409, "conflict", "a run is already queued or running for this audit"
    )
    at = app("app_pages/audit_results.py", "audit_tab", "Runtime", audit=audit_id)
    at.selectbox(key=f"runtime_command_{audit_id}").set_value(command["id"])
    rerun(at, "audit_tab", "Runtime")
    at.checkbox(key=f"approve_{command['id']}_confirm").check()
    rerun(at, "audit_tab", "Runtime")
    button(at, "Approve and run").click()
    rerun(at, "audit_tab", "Runtime")
    assert any("already queued or running" in e.value for e in at.error)


def test_static_audit_offers_a_runtime_audit_instead_of_commands(
    fake_api: FakeApi, audit_id: str
) -> None:
    audit = copy.deepcopy(fake_api.routes[f"/audits/{audit_id}"])
    audit["mode"] = "STATIC"
    fake_api.overrides[f"/audits/{audit_id}"] = audit
    at = app("app_pages/audit_results.py", "audit_tab", "Runtime", audit=audit_id)
    assert_clean(at)
    assert "This is a static audit: nothing was executed." in text(at)
    assert any(b.label == "Start a runtime audit of this snapshot" for b in at.button)


# --- optimization deep dive -------------------------------------------------------------------


def _deep_dive() -> tuple[str, list[dict[str, Any]]]:
    manifest = load("manifest.json")
    for key in manifest:
        if key.startswith("/workloads/") and key.endswith("/optimizations"):
            candidates = load(manifest[key])
            if candidates:
                return key.split("/")[2], candidates
    raise AssertionError("fixtures must contain a workload with candidates")


def _optimize(fake_api: FakeApi, candidate: dict[str, Any] | None = None) -> AppTest:
    workload_id, candidates = _deep_dive()
    if candidate is not None:
        fake_api.overrides[f"/workloads/{workload_id}/optimizations"] = [candidate]
        fake_api.overrides[f"/optimizations/{candidate['id']}"] = candidate
    analyses = [k for k in fake_api.routes if k.startswith("/analyses/") and k.count("/") == 2]
    owner = next(
        k
        for k in analyses
        if (candidate or candidates[0])["recommendation_id"]
        in fake_api.routes[k]["recommendation_ids"]
    )
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["workload"] = workload_id
    at.run()
    at.switch_page("app_pages/workload_lab.py")
    at.session_state[f"opt_analysis_{workload_id}"] = owner.split("/")[2]
    return rerun(at, "lab_tab", "Optimize")


def test_completed_deep_dive_shows_every_stage(fake_api: FakeApi) -> None:
    _, candidates = _deep_dive()
    completed = next(c for c in candidates if c["state"] == "COMPLETED")
    at = _optimize(fake_api, completed)
    assert_clean(at)
    body = text(at)
    assert "Profiler evidence" in body
    assert "Qualitative impact" in body
    assert "Proposed diff" in body
    assert "required checks passed" in body
    assert "Benchmark result" in body
    assert "linked to this candidate" in body
    assert 'class="pp-step done">Done' in body
    assert not [b for b in at.button if b.label == "Benchmark candidate vs baseline"]


def _pending(fake_api: FakeApi) -> dict[str, Any]:
    _, candidates = _deep_dive()
    candidate = copy.deepcopy(next(c for c in candidates if c["state"] == "COMPLETED"))
    candidate.update(
        state="AWAITING_APPROVAL",
        approval={"status": "PENDING", "approved_by": None, "approved_at": None},
        validation=None,
        validation_status="NOT_RUN",
        validation_run_id=None,
        benchmark=None,
        benchmark_run_id=None,
    )
    diff = fake_api.routes[f"/optimizations/{_deep_dive()[1][0]['id']}/diff"]
    fake_api.overrides[f"/optimizations/{candidate['id']}/diff"] = diff
    return candidate


def test_approval_is_explicit_and_benchmarking_is_blocked_before_validation(
    fake_api: FakeApi,
) -> None:
    candidate = _pending(fake_api)
    at = _optimize(fake_api, candidate)
    assert_clean(at)
    bench = button(at, "Benchmark candidate vs baseline")
    assert bench.disabled
    assert "Only a VALIDATED candidate can be benchmarked" in text(at)
    approve = button(at, "Approve candidate")
    assert approve.disabled
    fake_api.post_responses[f"/optimizations/{candidate['id']}/approve"] = candidate
    at.checkbox(key=f"reviewed_{candidate['id']}").check()
    rerun(at, "lab_tab", "Optimize")
    button(at, "Approve candidate").click()
    rerun(at, "lab_tab", "Optimize")
    assert fake_api.posts[-1] == (
        f"/optimizations/{candidate['id']}/approve",
        {"approved_by": "local-user"},
    )


def test_rejection_requires_a_reason(fake_api: FakeApi) -> None:
    candidate = _pending(fake_api)
    at = _optimize(fake_api, candidate)
    assert button(at, "Reject candidate").disabled
    fake_api.post_responses[f"/optimizations/{candidate['id']}/reject"] = candidate
    at.text_area(key=f"reason_{candidate['id']}").input("changes semantics for NaN")
    rerun(at, "lab_tab", "Optimize")
    button(at, "Reject candidate").click()
    rerun(at, "lab_tab", "Optimize")
    assert fake_api.posts[-1] == (
        f"/optimizations/{candidate['id']}/reject",
        {"reason": "changes semantics for NaN"},
    )


def test_benchmark_conflict_from_the_api_is_surfaced(fake_api: FakeApi) -> None:
    _, candidates = _deep_dive()
    candidate = copy.deepcopy(next(c for c in candidates if c["state"] == "COMPLETED"))
    candidate.update(state="VALIDATED", benchmark=None, benchmark_run_id=None)
    fake_api.post_responses["/benchmarks"] = ApiError(
        409,
        "invalid_state_transition",
        "transition AWAITING_APPROVAL -> BENCHMARKING is not allowed",
    )
    at = _optimize(fake_api, candidate)
    bench = button(at, "Benchmark candidate vs baseline")
    assert not bench.disabled
    bench.click()
    rerun(at, "lab_tab", "Optimize")
    assert fake_api.posts[-1][0] == "/benchmarks"
    assert fake_api.posts[-1][1]["candidate"] == {"candidate_id": candidate["id"]}
    assert any("BENCHMARKING is not allowed" in e.value for e in at.error)
