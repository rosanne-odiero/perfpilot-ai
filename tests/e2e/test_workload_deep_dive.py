"""Base-spec end-to-end demo of the workload deep dive, including restart persistence.

Driven only through the public HTTP API and the persistent job worker (every job in its own child
process, as with ``make worker``), for the ``duplicate_counter`` and ``nested_loop`` workloads:

 list projects/workloads -> inspect source -> profile -> hotspots -> analysis -> recommendation
 -> patch candidate + diff -> explicit approval -> validation (PASSED) -> A/B benchmark
 -> distributions, speedup, CI, labeled memory -> experiment reports (MD, JSON, HTML, PDF, CSV)
 -> restart (engine disposed, fresh app on the same data dir) -> everything is still there.

A second test drives a deliberately wrong candidate: it fails validation, is never benchmarked,
and its experiment report says so without any benchmark numbers. A patch that touches a
protected file is rejected by the patch validator and reported as rejected.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from backend.core.config import Settings
from backend.infrastructure.artifact_store import sha256_bytes
from backend.jobs.worker import Worker
from backend.main import create_app
from backend.models.database import get_engine, init_engine
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_generator import (
    PatchProposal,
    PatchRequest,
    register_patch_proposer,
)

BENCHMARK_SETTINGS = {
    "warmups": 2,
    "runs": 8,
    "memory_runs": 2,
    "seed": 12345,
    "bootstrap_resamples": 2000,
    "threshold_percent": 5.0,
}
ALL_FORMATS = ["md", "json", "html", "pdf", "csv"]
REQUIRED_CHECKS = {
    "isolation",
    "compile",
    "functional_tests",
    "output_equivalence",
    "input_mutation",
    "patched_code_exercised",
}
EXPECTED = {
    "duplicate_counter": {
        "function": "count_duplicates",
        "source_marker": "items.count(item)",
        "rule": "PP-COLL-001",
        "explains": re.compile(r"rescans the whole list", re.IGNORECASE),
    },
    "nested_loop": {
        "function": "customer_totals",
        "source_marker": "for order in orders",
        "rule": "PP-LOOP-002",
        "explains": re.compile(r"inner loop scans the whole inner collection", re.IGNORECASE),
    },
}


def _drain(settings: Settings) -> None:
    Worker(settings, isolate=True).drain()


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _job_succeeded(client: TestClient, job_id: str) -> None:
    job = _ok(client.get(f"/api/jobs/{job_id}"))
    assert job["status"] == "SUCCEEDED", job


def _bundled_workload(client: TestClient, slug: str) -> dict[str, Any]:
    projects = _ok(client.get("/api/projects"))
    project = next(p for p in projects if p["kind"] == "bundled_examples")
    workloads = _ok(client.get(f"/api/projects/{project['id']}/workloads"))
    workload: dict[str, Any] = next(w for w in workloads if w["slug"] == slug)
    return workload


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _assert_patch_is_algorithmic(slug: str, patched: str, function: str) -> None:
    """Check the patched text by its structure, never by comparing with examples/*/reference."""
    fn = _function(ast.parse(patched), function)
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For | ast.While)]
    if slug == "duplicate_counter":
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        uses_counter = any(isinstance(c.func, ast.Name) and c.func.id == "Counter" for c in calls)
        uses_dict_counting = any(
            isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Subscript)
            for n in ast.walk(fn)
        ) or any(isinstance(c.func, ast.Attribute) and c.func.attr == "get" for c in calls)
        assert uses_counter or uses_dict_counting, patched
        rescans = [
            c
            for loop in loops
            for c in ast.walk(loop)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "count"
        ]
        assert not rescans, "list.count() is still called inside a loop"
    else:
        nested = [
            inner
            for outer in loops
            for inner in ast.walk(outer)
            if inner is not outer
            and isinstance(inner, ast.For)
            and isinstance(inner.iter, ast.Name)
            and inner.iter.id == "orders"
        ]
        assert not nested, "orders is still scanned inside the customer loop"
        assert re.search(r"\{\}|dict\(|setdefault|defaultdict", ast.unparse(fn)), patched


def _download(client: TestClient, report_id: str, part: str = "primary") -> bytes:
    response = client.get(f"/api/reports/{report_id}/download", params={"part": part})
    assert response.status_code == 200, response.text
    return response.content


def _run_deep_dive(client: TestClient, settings: Settings, slug: str) -> dict[str, Any]:
    expected = EXPECTED[slug]
    workload = _bundled_workload(client, slug)

    # Inspect the registered source.
    detail = _ok(client.get(f"/api/workloads/{workload['id']}"))
    assert expected["source_marker"] in detail["entrypoint_source"]["content"]
    assert detail["fixture_description"]

    # Profile -> hotspots.
    accepted = _ok(
        client.post("/api/profiles", json={"workload_id": workload["id"], "size": "small"}), 202
    )
    _drain(settings)
    _job_succeeded(client, accepted["job_id"])
    profile_id = accepted["subject_id"]
    profile = _ok(client.get(f"/api/profiles/{profile_id}"))
    assert profile["status"] == "SUCCEEDED"
    assert profile["provenance"] == "MEASURED"
    hotspots = _ok(client.get(f"/api/profiles/{profile_id}/hotspots", params={"scope": "user"}))
    assert hotspots[0]["function_name"] == expected["function"]
    assert hotspots[0]["cumulative_time_ms"] >= hotspots[0]["internal_time_ms"] > 0

    # Analysis -> recommendation that explains the repeated scans.
    accepted = _ok(
        client.post(
            "/api/analyses", json={"workload_id": workload["id"], "profile_id": profile_id}
        ),
        202,
    )
    _drain(settings)
    _job_succeeded(client, accepted["job_id"])
    analysis = _ok(client.get(f"/api/analyses/{accepted['subject_id']}"))
    assert analysis["state"] == "RECOMMENDED"
    recommendation = _ok(client.get(f"/api/analyses/{analysis['id']}/recommendations"))[0]
    assert recommendation["rule_id"] == expected["rule"]
    assert recommendation["target_function"] == expected["function"]
    assert expected["explains"].search(recommendation["suspected_root_cause"])
    assert recommendation["profiler_evidence"]["provenance"] == "MEASURED"
    assert "[MEASURED]" in recommendation["observed_bottleneck"]

    # Patch candidate -> diff.
    candidate = _ok(
        client.post("/api/optimizations", json={"recommendation_id": recommendation["id"]}), 201
    )
    assert candidate["state"] == "AWAITING_APPROVAL"
    diff = _ok(client.get(f"/api/optimizations/{candidate['id']}/diff"))
    assert diff["unified_diff"].startswith("--- a/workload.py")
    _assert_patch_is_algorithmic(slug, diff["files"][0]["patched"], expected["function"])

    # Explicit approval -> validation.
    approved = _ok(
        client.post(
            f"/api/optimizations/{candidate['id']}/approve", json={"approved_by": "e2e-tester"}
        )
    )
    assert approved["approval"]["status"] == "APPROVED"
    assert approved["approval"]["approved_by"] == "e2e-tester"
    accepted = _ok(client.post(f"/api/optimizations/{candidate['id']}/validate"), 202)
    _drain(settings)
    _job_succeeded(client, accepted["job_id"])
    validation = _ok(client.get(f"/api/optimizations/{candidate['id']}/validation"))
    assert validation["status"] == "PASSED", validation
    checks = {c["name"]: c for c in validation["checks"]}
    required = {name for name, c in checks.items() if c["required"]}
    assert required >= REQUIRED_CHECKS
    assert all(checks[name]["status"] == "PASSED" for name in required)

    # A/B benchmark of the validated patch.
    accepted = _ok(
        client.post(
            "/api/benchmarks",
            json={
                "workload_id": workload["id"],
                "candidate": {"candidate_id": candidate["id"]},
                **BENCHMARK_SETTINGS,
            },
        ),
        202,
    )
    _drain(settings)
    _job_succeeded(client, accepted["job_id"])
    benchmark_id = accepted["subject_id"]
    benchmark = _ok(client.get(f"/api/benchmarks/{benchmark_id}"))
    assert benchmark["status"] == "SUCCEEDED"
    assert benchmark["provenance"] == "MEASURED"
    summary = benchmark["summary"]
    for variant in ("baseline", "candidate"):
        wall = summary[variant]["wall_ms"]
        assert wall["n"] == BENCHMARK_SETTINGS["runs"]
        assert wall["min"] <= wall["median"] <= wall["max"]
        assert wall["p95_warning"]  # n < 100: p95 is flagged as unstable
    assert benchmark["speedup"] > 0
    assert benchmark["speedup_ci_low"] <= benchmark["speedup"] <= benchmark["speedup_ci_high"]
    assert benchmark["classification"] in {"IMPROVED", "NO_CLEAR_DIFFERENCE", "REGRESSED"}
    memory = benchmark["memory"]
    assert set(memory["definitions"]) >= {"tracemalloc_peak_bytes", "rss_peak_bytes"}
    assert memory["baseline"]["tracemalloc_peak_bytes_median"] is not None
    samples = _ok(client.get(f"/api/benchmarks/{benchmark_id}/results"))
    timing = [s for s in samples if s["kind"] == "timing"]
    assert len(timing) == 2 * BENCHMARK_SETTINGS["runs"]
    assert all(s["ok"] and s["wall_ns"] > 0 for s in timing)
    final = _ok(client.get(f"/api/optimizations/{candidate['id']}"))
    assert final["state"] == "COMPLETED"

    # Experiment reports in every format (idempotent per candidate version).
    request = {"formats": ALL_FORMATS}
    first = _ok(client.post(f"/api/optimizations/{candidate['id']}/reports", json=request), 202)
    again = _ok(client.post(f"/api/optimizations/{candidate['id']}/reports", json=request), 202)
    assert [r["report_id"] for r in first["reports"]] == [r["report_id"] for r in again["reports"]]
    _drain(settings)
    listed = _ok(client.get(f"/api/optimizations/{candidate['id']}/reports"))
    assert {r["format"] for r in listed} == set(ALL_FORMATS)
    assert all(r["status"] == "SUCCEEDED" for r in listed), listed
    assert all(r["subject_type"] == "optimization" for r in listed)
    reports = {r["format"]: r for r in listed}

    report_json = json.loads(_download(client, reports["json"]["id"]))
    assert report_json["schema_version"] == "1.0"
    assert report_json["kind"] == "optimization"
    assert report_json["patch"]["unified_diff"] == diff["unified_diff"]
    assert report_json["validation"]["status"] == "PASSED"
    assert set(report_json["validation"]["required_checks"]) >= REQUIRED_CHECKS
    bench = report_json["benchmark"]
    assert bench["provenance"] == "MEASURED"
    assert bench["speedup"] == pytest.approx(benchmark["speedup"])
    assert bench["ci_low"] == pytest.approx(benchmark["speedup_ci_low"])
    stats = {(s["variant"], s["metric"]): s for s in bench["stats"]}
    assert stats[("baseline", "wall_ms")]["median"] == pytest.approx(
        summary["baseline"]["wall_ms"]["median"]
    )
    assert stats[("candidate", "wall_ms")]["small_n_warning"]
    assert {row["tool"] for row in bench["memory"]["rows"]} == {
        "tracemalloc",
        "rss",
        "ru_maxrss",
    }
    assert len([s for s in bench["samples"] if s["kind"] == "timing"]) == len(timing)
    assert report_json["recommendation"]["profiler_evidence_provenance"] == "MEASURED"
    assert report_json["recommendation"]["expected_effect"]["provenance"] == "STATIC"
    assert report_json["approval"]["approved_by"] == "e2e-tester"
    assert report_json["workload"]["input_sha256"] == benchmark["input_sha256"]
    assert report_json["environment"]["provenance"] == "MEASURED"
    assert any("optimize_workload.py" in c for c in report_json["reproducibility"]["cli"])

    markdown = _download(client, reports["md"]["id"]).decode()
    assert "### Measured vs hypothesis" in markdown
    assert "**[MEASURED]**" in markdown
    assert "**[STATIC]**" in markdown
    assert "```diff" in markdown
    assert f"**{benchmark['classification']}**" in markdown
    assert f"{benchmark['speedup']:,.2f}" in markdown
    preview = client.get(f"/api/reports/{reports['md']['id']}/preview")
    assert preview.status_code == 200
    assert preview.text == markdown

    page = _download(client, reports["html"]["id"]).decode()
    assert "<svg" in page
    assert "Raw wall-time samples" in page
    assert 'class="badge prov prov-measured"' in page
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")

    pdf = _download(client, reports["pdf"]["id"])
    assert pdf.startswith(b"%PDF")
    pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Measured vs hypothesis" in pdf_text

    samples_csv = list(
        csv.DictReader(io.StringIO(_download(client, reports["csv"]["id"]).decode()))
    )
    assert len([r for r in samples_csv if r["kind"] == "timing"]) == len(timing)
    checks_csv = list(
        csv.DictReader(
            io.StringIO(_download(client, reports["csv"]["id"], "validation_checks").decode())
        )
    )
    assert {r["check"] for r in checks_csv} == set(checks)

    history = _ok(client.get("/api/experiments", params={"kind": "optimization"}))
    entry = next(e for e in history if e["id"] == candidate["id"])
    assert entry["classification"] == benchmark["classification"]
    assert entry["speedup"] == pytest.approx(benchmark["speedup"])
    assert entry["report_count"] == len(ALL_FORMATS)

    print(
        f"\n[e2e] {slug}: validation {validation['status']} "
        f"({len(required)} required checks), median baseline "
        f"{summary['baseline']['wall_ms']['median']:.3f} ms, candidate "
        f"{summary['candidate']['wall_ms']['median']:.3f} ms, speedup "
        f"{benchmark['speedup']:.2f}x, 95% CI [{benchmark['speedup_ci_low']:.2f}, "
        f"{benchmark['speedup_ci_high']:.2f}], {benchmark['classification']}, tracemalloc "
        f"{memory['baseline']['tracemalloc_peak_bytes_median']:.0f} -> "
        f"{memory['candidate']['tracemalloc_peak_bytes_median']:.0f} B, RSS "
        f"{memory['baseline']['rss_peak_bytes_median']:.0f} -> "
        f"{memory['candidate']['rss_peak_bytes_median']:.0f} B"
    )
    return {
        "workload_id": workload["id"],
        "candidate": final,
        "benchmark": benchmark,
        "samples": samples,
        "reports": {
            r["id"]: {
                "files": r["files"],
                "bytes": {f["role"]: _download(client, r["id"], f["role"]) for f in r["files"]},
            }
            for r in listed
        },
    }


@pytest.mark.parametrize("slug", ["duplicate_counter", "nested_loop"])
def test_workload_deep_dive_survives_restart(settings: Settings, slug: str) -> None:
    with TestClient(create_app()) as client:
        before = _run_deep_dive(client, settings, slug)

    # Simulated restart: drop every pooled connection and start a new app on the same data dir.
    get_engine().dispose()
    init_engine(settings)
    with TestClient(create_app()) as client:
        assert _bundled_workload(client, slug)["id"] == before["workload_id"]
        candidate = _ok(client.get(f"/api/optimizations/{before['candidate']['id']}"))
        assert candidate["state"] == "COMPLETED"
        assert candidate["state_history"] == before["candidate"]["state_history"]
        assert candidate["benchmark_run_id"] == before["benchmark"]["id"]
        benchmark = _ok(client.get(f"/api/benchmarks/{before['benchmark']['id']}"))
        assert benchmark["speedup"] == before["benchmark"]["speedup"]
        assert benchmark["summary"] == before["benchmark"]["summary"]
        assert _ok(client.get(f"/api/benchmarks/{benchmark['id']}/results")) == before["samples"]
        history = _ok(client.get("/api/experiments"))
        assert candidate["id"] in {e["id"] for e in history}
        listed = _ok(client.get(f"/api/optimizations/{candidate['id']}/reports"))
        assert {r["id"] for r in listed} == set(before["reports"])
        for report in listed:
            saved = before["reports"][report["id"]]
            assert report["status"] == "SUCCEEDED"
            assert report["files"] == saved["files"]
            for file in report["files"]:
                data = _download(client, report["id"], file["role"])
                assert data == saved["bytes"][file["role"]]
                assert hashlib.sha256(data).hexdigest() == file["sha256"]
                metadata = _ok(client.get(f"/api/reports/{report['id']}"))
                assert file["sha256"] in {f["sha256"] for f in metadata["files"]}


class _WrongProposer:
    """Stands in for a model that proposes a plausible but wrong patch."""

    name = "e2e-wrong-model"

    def __init__(self, path: str, transform: Any) -> None:
        self.path = path
        self.transform = transform

    def propose(self, request: PatchRequest) -> PatchProposal:
        root = request.target.source_root
        original = (root / self.path).read_text()
        return PatchProposal(
            status="proposed",
            rule_id=request.target.rule_id,
            target_file=request.target.target_file,
            target_function=request.target.target_function,
            generated_by=self.name,
            original_sha256=sha256_bytes((root / request.target.target_file).read_bytes()),
            diff=make_unified_diff(self.path, original, self.transform(original)),
            summary="deliberately wrong proposal",
        )


@pytest.fixture
def wrong_proposer() -> Iterator[Any]:
    def install(path: str, transform: Any) -> None:
        register_patch_proposer(lambda _settings: _WrongProposer(path, transform))

    yield install
    register_patch_proposer(None)


def _report_json(client: TestClient, settings: Settings, candidate_id: str) -> dict[str, Any]:
    accepted = _ok(
        client.post(f"/api/optimizations/{candidate_id}/reports", json={"formats": ["json", "md"]}),
        202,
    )
    _drain(settings)
    listed = {r["format"]: r for r in _ok(client.get(f"/api/optimizations/{candidate_id}/reports"))}
    assert {r["report_id"] for r in accepted["reports"]} == {r["id"] for r in listed.values()}
    report: dict[str, Any] = json.loads(_download(client, listed["json"]["id"]))
    report["_markdown"] = _download(client, listed["md"]["id"]).decode()
    return report


def test_invalid_candidates_are_reported_without_benchmark_numbers(
    api: TestClient, settings: Settings, wrong_proposer: Any
) -> None:
    workload = _bundled_workload(api, "duplicate_counter")
    accepted = _ok(
        api.post("/api/analyses", json={"workload_id": workload["id"], "size": "small"}), 202
    )
    _drain(settings)
    recommendation = _ok(api.get(f"/api/analyses/{accepted['subject_id']}/recommendations"))[0]

    # Plausible but wrong: iterating a set loses the first-occurrence key order.
    wrong_proposer(
        "workload.py",
        lambda text: text.replace("    for item in items:\n", "    for item in set(items):\n"),
    )
    candidate = _ok(
        api.post("/api/optimizations", json={"recommendation_id": recommendation["id"]}), 201
    )
    _ok(api.post(f"/api/optimizations/{candidate['id']}/approve", json={"approved_by": "e2e"}))
    _ok(api.post(f"/api/optimizations/{candidate['id']}/validate"), 202)
    _drain(settings)
    failed = _ok(api.get(f"/api/optimizations/{candidate['id']}"))
    assert failed["state"] == "FAILED"
    assert failed["validation_status"] == "FAILED"
    blocked = api.post(
        "/api/benchmarks",
        json={"workload_id": workload["id"], "candidate": {"candidate_id": candidate["id"]}},
    )
    assert blocked.status_code == 409

    report = _report_json(api, settings, candidate["id"])
    assert report["outcome"]["state"] == "FAILED"
    assert report["outcome"]["succeeded"] is False
    assert "validation FAILED" in report["outcome"]["headline"]
    assert any("functional_tests FAILED" in r for r in report["outcome"]["reasons"])
    assert report["validation"]["status"] == "FAILED"
    failing = {c["name"]: c for c in report["validation"]["checks"]}["functional_tests"]
    assert failing["status"] == "FAILED"
    assert failing["failures"]
    assert report["benchmark"] is None
    assert "No benchmark ran" in report["benchmark_absent_reason"]
    assert report["patch"]["generated_by"] == "e2e-wrong-model"
    labels = {item["label"] for item in report["summary_box"]["measured"]}
    assert labels == {"Profiled bottleneck", "Correctness validation"}
    assert "never benchmarked" in report["summary_box"]["verdict"]
    markdown = report["_markdown"]
    assert "No benchmark ran" in markdown
    for absent in ("Median speedup", "Median wall time", "Bootstrap CI", "Per-variant statistics"):
        assert absent not in markdown

    # A diff that edits the tests is rejected by the patch validator before anyone approves it.
    wrong_proposer(
        "tests/test_workload.py", lambda text: text.replace("assert ", "assert True or ", 1)
    )
    rejected = _ok(
        api.post("/api/optimizations", json={"recommendation_id": recommendation["id"]}), 201
    )
    assert rejected["state"] == "REJECTED"
    report = _report_json(api, settings, rejected["id"])
    assert report["outcome"]["state"] == "REJECTED"
    assert "patch-validator" in report["outcome"]["headline"]
    assert report["approval"]["status"] == "REJECTED"
    assert report["approval"]["rejection_reasons"]
    assert report["patch"]["patch_validator_valid"] is False
    assert report["validation"] is None
    assert report["benchmark"] is None
    assert "REJECTED" in report["benchmark_absent_reason"]
    assert "Median wall time" not in report["_markdown"]
