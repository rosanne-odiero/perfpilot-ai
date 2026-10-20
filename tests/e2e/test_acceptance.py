"""End-to-end acceptance test for the Repository Audit feature.

Mirrors the acceptance scenario in the feature specification, driven only through the public HTTP
API and the persistent job worker:

 1. Select a public Python repository fixture (``tests/fixtures/repos/shopfast``).
 2. Import the repository (ZIP upload — the path an untrusted, external repository takes).
 3. Index source files.
 4. Parse the modules and dependency graph.
 5. Run static analysis.
 6. Generate prioritized findings with valid file and line references.
 7. Create an HTML and PDF report.
 8. Verify the reports contain real findings and no fabricated runtime metrics.
 9. Select a trusted runnable workload (from the discovered runtime commands).
10. Run optional isolated profiling after approval (real Docker sandbox).
11. Export a report containing measured performance data.

Steps 1-8 always run. Steps 9-11 need Docker + ``perfpilot-sandbox:latest`` and are marked
``docker``; they skip with the reason otherwise.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from audit_support import SHOPFAST
from fastapi.testclient import TestClient
from pypdf import PdfReader

from backend.core.config import Settings
from backend.jobs.worker import Worker
from backend.runtime.backends import set_backend_factory
from backend.sandbox.docker_backend import DockerSandboxBackend

WORKLOAD_ARGV = ["python", "benchmarks/run_workload.py"]
MEASURED_BADGE = 'class="badge prov prov-measured"'
FAKE_ENV_SECRET = (SHOPFAST / ".env").read_text().split("=", 1)[1].strip()

# A number followed by a time/memory unit, a speedup factor or a percentage.
MEASUREMENT_LIKE = re.compile(
    r"\d[\d,]*(?:\.\d+)?[ \u00a0]?(?:(?:ms|µs|us|ns|s|MB|MiB|KB|KiB|GB|GiB|x)\b|\u00d7|%)"
)


def _drain(settings: Settings) -> None:
    # isolate=True: every job runs in its own child process, exactly like `make worker`.
    Worker(settings, isolate=True).drain()


def _job_ok(api: TestClient, job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = api.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "SUCCEEDED", job
    return job


def _shopfast_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SHOPFAST.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, Path("shopfast") / path.relative_to(SHOPFAST))
    return buffer.getvalue()


def _visible_prose(document: str) -> str:
    """Report text a reader sees, minus code blocks (repository source may contain any numbers)."""
    document = re.sub(r"(?is)<(style|pre|code|svg)\b.*?</\1>", " ", document)
    document = re.sub(r"(?s)<[^>]+>", " ", document)
    return html.unescape(document)


def _expected_findings() -> set[tuple[str, str, int]]:
    expected = json.loads((SHOPFAST / "EXPECTED_FINDINGS.json").read_text())
    items = expected["findings"] if isinstance(expected, dict) else expected
    return {(f["rule_id"], f["file_path"], int(f["line_start"])) for f in items}


def _import_and_audit(api: TestClient, settings: Settings, mode: str) -> dict[str, str]:
    # Step 2: import (ZIP upload).
    accepted = api.post(
        "/api/repositories/upload",
        files={"file": ("shopfast.zip", _shopfast_zip(), "application/zip")},
        data={"display_name": "shopfast"},
    )
    assert accepted.status_code == 202, accepted.text
    _drain(settings)
    _job_ok(api, accepted.json()["job_id"])
    snapshot_id = accepted.json()["subject_id"]
    snapshot = api.get(f"/api/snapshots/{snapshot_id}").json()
    assert snapshot["status"] == "READY", snapshot

    # Step 3: inventory / indexing of source files.
    files = api.get(f"/api/snapshots/{snapshot_id}/files", params={"include_skipped": True}).json()
    entries = files["files"] if isinstance(files, dict) else files
    by_path = {f["path"]: f for f in entries}
    expected_py = {
        p.relative_to(SHOPFAST).as_posix()
        for p in SHOPFAST.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    indexed_py = {p for p, f in by_path.items() if f["is_python"] and not f["skipped"]}
    assert indexed_py == expected_py
    assert by_path[".env"]["skipped"]
    assert by_path[".env"]["skip_reason"] == "sensitive"
    withheld = api.get(f"/api/snapshots/{snapshot_id}/files/content", params={"path": ".env"})
    assert withheld.status_code in (400, 403)

    # Steps 4-6: audit job → modules, dependency graph, static findings with priorities.
    started = api.post("/api/audits", json={"snapshot_id": snapshot_id, "mode": mode})
    assert started.status_code == 202, started.text
    _drain(settings)
    _job_ok(api, started.json()["job_id"])
    return {"snapshot_id": snapshot_id, "audit_id": started.json()["subject_id"]}


def _check_findings(api: TestClient, audit_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    modules = api.get(f"/api/audits/{audit_id}/modules").json()
    assert {m["file_path"] for m in modules} >= {"shopfast/orders.py", "shopfast/catalog.py"}
    graph = api.get(f"/api/audits/{audit_id}/graph").json()
    assert graph["nodes"]
    assert graph["edges"]
    node_names = {n["module_name"] for n in graph["nodes"]}
    assert all(e["source"] in node_names and e["target"] in node_names for e in graph["edges"])

    page = api.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()
    findings: list[dict[str, Any]] = page["items"]
    assert page["total"] == len(findings) > 0
    scores = [f["priority_score"] for f in findings]
    assert scores == sorted(scores, reverse=True), "findings must be prioritized"
    found = {
        (f["rule_id"], f["location"]["file_path"], f["location"]["line_start"]) for f in findings
    }
    assert _expected_findings() <= found

    # Every file/line reference resolves to the real source, and every snippet is real code.
    for finding in findings:
        location = finding["location"]
        source = (SHOPFAST / location["file_path"]).read_text().splitlines()
        assert 1 <= location["line_start"] <= location["line_end"] <= len(source)
        served = api.get(
            f"/api/snapshots/{snapshot_id}/files/content", params={"path": location["file_path"]}
        )
        assert served.status_code == 200
        assert source[location["line_start"] - 1].strip() in finding["snippet"]
        assert finding["provenance"] == "STATIC"
    return findings


def _export(
    api: TestClient, settings: Settings, audit_id: str, formats: list[str]
) -> dict[str, bytes]:
    accepted = api.post(f"/api/audits/{audit_id}/reports", json={"formats": formats})
    assert accepted.status_code == 202, accepted.text
    _drain(settings)
    reports = api.get(f"/api/audits/{audit_id}/reports").json()
    latest: dict[str, dict[str, Any]] = {}
    for report in sorted(reports, key=lambda r: r["created_at"], reverse=True):
        latest.setdefault(report["format"], report)
    out: dict[str, bytes] = {}
    for fmt in formats:
        report = latest[fmt]
        assert report["status"] == "SUCCEEDED", report
        download = api.get(f"/api/reports/{report['id']}/download")
        assert download.status_code == 200
        out[fmt] = download.content
    return out


def _pdf_text(data: bytes) -> str:
    assert data.startswith(b"%PDF")
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def test_static_repository_audit_acceptance(client: TestClient, settings: Settings) -> None:
    """Steps 1-8: import → index → graph → findings → HTML/PDF, no fabricated runtime metrics."""
    ids = _import_and_audit(client, settings, "STATIC")
    audit = client.get(f"/api/audits/{ids['audit_id']}").json()
    assert audit["status"] == "SUCCEEDED"
    assert audit["stage"] == "COMPLETED"
    findings = _check_findings(client, ids["audit_id"], ids["snapshot_id"])

    recommendations = client.get(f"/api/audits/{ids['audit_id']}/recommendations").json()
    assert recommendations, "the deterministic advisor must work without an API key"
    assert all(r["provenance"] == "STATIC" for r in recommendations)
    finding_ids = {f["id"] for f in findings}
    assert all(set(r["finding_ids"]) <= finding_ids for r in recommendations)

    # Steps 7-8: HTML and PDF contain real findings and no runtime numbers.
    reports = _export(client, settings, ids["audit_id"], ["html", "pdf"])
    page = reports["html"].decode()
    pdf = _pdf_text(reports["pdf"])
    for finding in findings[:10]:
        assert f"{finding['location']['file_path']}:{finding['location']['line_start']}" in page
    top = findings[0]["location"]
    assert f"{top['file_path']}:{top['line_start']}" in pdf.replace("\n", "")
    assert MEASURED_BADGE not in page
    assert "Not measured" in page
    assert "Not measured" in pdf
    assert not MEASUREMENT_LIKE.findall(_visible_prose(page)), "static report shows a metric"
    for document in (page, pdf):
        assert FAKE_ENV_SECRET not in document


@pytest.mark.docker
def test_runtime_repository_audit_acceptance(client: TestClient, settings: Settings) -> None:
    """Steps 9-11: approve a trusted workload, profile it in the sandbox, export measured data."""
    set_backend_factory(None)  # the real Docker backend, never a test double
    status = DockerSandboxBackend(settings).availability()
    if not status.available:
        pytest.skip(f"Docker sandbox unavailable: {status.reason}")

    ids = _import_and_audit(client, settings, "RUNTIME")
    audit_id = ids["audit_id"]
    assert client.get(f"/api/audits/{audit_id}").json()["stage"] == "AWAITING_RUNTIME_APPROVAL"
    _check_findings(client, audit_id, ids["snapshot_id"])

    # Step 9: pick the trusted workload from the discovered (never auto-run) commands.
    commands = client.get(f"/api/audits/{audit_id}/runtime/commands").json()
    workload = next(c for c in commands if c["argv"] == WORKLOAD_ARGV)
    assert not client.get(f"/api/audits/{audit_id}/runtime").json().get("runs")

    # Step 10: explicit approval of that exact argv → isolated profiling in the sandbox.
    approved = client.post(
        f"/api/audits/{audit_id}/runtime/approvals",
        json={"command_id": workload["id"], "argv": workload["argv"], "approved_by": "acceptance"},
    )
    assert approved.status_code == 202, approved.text
    _drain(settings)
    run = client.get(f"/api/audits/{audit_id}/runtime/runs/{approved.json()['subject_id']}").json()
    assert run["status"] == "SUCCEEDED", (run.get("failure_reason"), run.get("stderr_tail"))
    assert run["provenance"] == "MEASURED"
    assert run["environment"]["network"] == "none"
    assert run["hotspots"]
    assert run["correlations"]

    items = client.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()["items"]
    measured = [f for f in items if f.get("measured")]
    assert measured
    assert all(f["measured"]["provenance"] == "MEASURED" for f in measured)
    assert all(f["provenance"] == "STATIC" for f in measured), "static claims stay STATIC"

    # Step 11: the exported report now contains measured performance data, clearly labeled.
    reports = _export(client, settings, audit_id, ["html", "pdf", "json"])
    section7 = reports["html"].decode().split('id="s7"')[1].split('id="s8"')[0]
    assert MEASURED_BADGE in section7
    assert "Not measured" not in section7
    data = json.loads(reports["json"])
    assert data["runtime"] is not None
    names = {h["function_name"] for p in data["runtime"]["profiles"] for h in p["hotspots"]}
    assert "reconcile" in names
    assert "MEASURED" in _pdf_text(reports["pdf"])
