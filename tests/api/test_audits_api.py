"""Audit API: start, run (worker), and browse results; error and security cases."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from audit_support import SHOPFAST, copy_into_workspace, create_snapshot, scan_inventory
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.config import Settings
from backend.jobs.service import enqueue_job
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.enums import SnapshotStatus
from backend.models.orm.audit import AuditRun, FindingRecord
from backend.models.orm.platform import Job


def _ready_snapshot(settings: Settings, name: str = "shopfast") -> tuple[str, Path]:
    workspace = copy_into_workspace(settings, SHOPFAST, name)
    return create_snapshot(workspace, scan_inventory(workspace)), workspace


def _run_audit(client: TestClient, settings: Settings, snapshot_id: str, **extra: object) -> str:
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id, **extra})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["subject_type"] == "audit"
    assert body["status"] == "QUEUED"
    Worker(settings, isolate=False).drain()
    return str(body["subject_id"])


def test_static_audit_end_to_end(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    audit_id = _run_audit(client, settings, snapshot_id)

    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    assert audit["stage"] == "COMPLETED"
    assert audit["job_status"] == "SUCCEEDED"
    assert audit["job_progress"] == 1.0
    summary = audit["summary"]
    assert summary["provenance"] == "STATIC"
    assert summary["parse_errors"] == 0
    expected = json.loads((SHOPFAST / "EXPECTED_FINDINGS.json").read_text())["findings"]
    assert summary["findings_total"] == len(expected)

    listed = client.get("/api/audits", params={"snapshot_id": snapshot_id}).json()
    assert [a["id"] for a in listed] == [audit_id]
    assert client.get("/api/audits", params={"snapshot_id": "other"}).json() == []

    page = client.get(f"/api/audits/{audit_id}/findings").json()
    assert page["total"] == len(expected)
    scores = [f["priority_score"] for f in page["items"]]
    assert scores == sorted(scores, reverse=True)
    assert {
        (f["rule_id"], f["location"]["file_path"], f["location"]["line_start"])
        for f in page["items"]
    } == {(e["rule_id"], e["file_path"], e["line_start"]) for e in expected}

    first = page["items"][0]
    detail = client.get(f"/api/audits/{audit_id}/findings/{first['id']}").json()
    assert detail == first

    graph = client.get(f"/api/audits/{audit_id}/graph").json()
    assert any(
        e["source"] == "shopfast.api" and e["target"] == "shopfast.app" for e in graph["edges"]
    )
    assert sum(n["finding_count"] for n in graph["nodes"]) == len(expected)

    modules = client.get(f"/api/audits/{audit_id}/modules", params={"path": "shopfast/"}).json()
    assert modules
    assert all(m["file_path"].startswith("shopfast/") for m in modules)
    api_module = next(m for m in modules if m["module_name"] == "shopfast.api")
    assert api_module["is_entrypoint"]
    assert api_module["symbol_count"] >= 4
    assert "symbols" not in api_module
    assert "imports" not in api_module

    counts = client.get(f"/api/audits/{audit_id}/files/findings-count").json()
    by_path = {c["file_path"]: c for c in counts}
    assert by_path["shopfast/inventory_sync.py"]["total"] == 5
    assert sum(c["total"] for c in counts) == len(expected)
    assert by_path["shopfast/orders.py"]["by_severity"]["HIGH"] == 2


def test_findings_filters(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    audit_id = _run_audit(client, settings, snapshot_id)
    url = f"/api/audits/{audit_id}/findings"

    def get(**params: Any) -> dict[str, Any]:
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    high_low = get(severity=["HIGH", "LOW"])
    assert {f["severity"] for f in high_low["items"]} == {"HIGH", "LOW"}
    db = get(category=["DATABASE"])
    assert db["total"] == 2
    assert get(rule_id="PP-LOOP-001")["total"] == 4
    assert get(file_path="shopfast/api")["total"] == 3
    assert get(file_path="shopfast/a%")["total"] == 0  # LIKE wildcards are escaped
    assert get(provenance="MEASURED")["total"] == 0
    assert get(provenance="STATIC")["total"] == get()["total"]
    top = get(min_priority=30)
    assert all(f["priority_score"] >= 30 for f in top["items"])
    paged = get(limit=5, offset=5)
    assert len(paged["items"]) == 5
    assert paged["items"][0] == get()["items"][5]
    assert client.get(url, params={"severity": "NOPE"}).status_code == 422
    assert client.get(url, params={"limit": 0}).status_code == 422


def test_rerunning_the_job_replaces_findings(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    audit_id = _run_audit(client, settings, snapshot_id)
    with session_scope() as session:
        enqueue_job(session, "audit.run", {"audit_id": audit_id})
    Worker(settings, isolate=False).drain()
    with session_scope() as session:
        rows = session.scalars(select(FindingRecord).where(FindingRecord.audit_id == audit_id))
        ids = [r.id for r in rows]
    assert (
        len(ids)
        == len(set(ids))
        == client.get(f"/api/audits/{audit_id}").json()["summary"]["findings_total"]
    )


def test_parse_errors_make_the_audit_partial(client: TestClient, settings: Settings) -> None:
    workspace = copy_into_workspace(settings, SHOPFAST, "broken")
    (workspace / "shopfast" / "broken.py").write_text("def nope(:\n")
    snapshot_id = create_snapshot(workspace, scan_inventory(workspace))
    audit_id = _run_audit(client, settings, snapshot_id)
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "PARTIAL"
    assert audit["summary"]["parse_errors"] == 1
    assert any("could not be parsed" in w for w in audit["warnings"])
    modules = client.get(f"/api/audits/{audit_id}/modules").json()
    broken = next(m for m in modules if m["file_path"] == "shopfast/broken.py")
    assert broken["parse_error"].startswith("SyntaxError")


def test_secrets_never_reach_the_database(client: TestClient, settings: Settings) -> None:
    snapshot_id, workspace = _ready_snapshot(settings)
    secret = (workspace / ".env").read_text().split("=", 1)[1].strip()
    audit_id = _run_audit(client, settings, snapshot_id)
    with session_scope() as session:
        audit = session.get(AuditRun, audit_id)
        assert audit is not None
        blob = json.dumps([audit.code_index, audit.summary, audit.warnings])
        rows = list(session.scalars(select(FindingRecord.data)))
    assert secret not in blob
    assert secret not in json.dumps(rows)


def test_llm_flags(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id, "use_llm": True})
    assert response.status_code == 422
    assert "llm_consent" in response.json()["error"]["message"]
    audit_id = _run_audit(client, settings, snapshot_id, use_llm=True, llm_consent=True)
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    assert audit["use_llm"] is True
    assert any("no LLM endpoint is configured" in w for w in audit["warnings"])
    assert audit["summary"]["advisor"]["mode"] == "deterministic"
    usage = client.get(f"/api/audits/{audit_id}/llm-usage").json()
    assert usage["calls"] == 0
    assert usage["stage_status"] == {"llm": "not_configured"}


def test_runtime_mode_is_accepted(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id, "mode": "RUNTIME"})
    assert response.status_code == 202, response.text
    audit = client.get(f"/api/audits/{response.json()['subject_id']}").json()
    assert audit["mode"] == "RUNTIME"
    assert audit["stage"] == "QUEUED"


def test_request_validation_and_missing_resources(client: TestClient, settings: Settings) -> None:
    assert client.post("/api/audits", json={"snapshot_id": "nope"}).status_code == 404
    assert client.post("/api/audits", json={}).status_code == 422
    extra = client.post("/api/audits", json={"snapshot_id": "x", "surprise": 1})
    assert extra.status_code == 422
    for path in ("", "/findings", "/findings/abc", "/graph", "/modules", "/files/findings-count"):
        response = client.get(f"/api/audits/missing{path}")
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


def test_snapshot_must_be_ready(client: TestClient, settings: Settings) -> None:
    pending = create_snapshot(None, None, status=SnapshotStatus.INGESTING)
    response = client.post("/api/audits", json={"snapshot_id": pending})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["snapshot_status"] == "INGESTING"


def test_unfinished_audit_has_no_graph(client: TestClient, settings: Settings) -> None:
    snapshot_id, _ = _ready_snapshot(settings)
    audit_id = client.post("/api/audits", json={"snapshot_id": snapshot_id}).json()["subject_id"]
    response = client.get(f"/api/audits/{audit_id}/graph")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "PENDING"
    assert client.get(f"/api/audits/{audit_id}/findings").json()["total"] == 0


def test_workspace_outside_data_dir_is_rejected(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    snapshot_id = create_snapshot(SHOPFAST, scan_inventory(SHOPFAST))
    audit_id = client.post("/api/audits", json={"snapshot_id": snapshot_id}).json()["subject_id"]
    Worker(settings, isolate=False).drain()
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "FAILED"
    assert "outside the workspaces directory" in audit["error"]
    with session_scope() as session:
        job = session.get(Job, audit["job_id"])
        assert job is not None
        assert job.status == "FAILED"
        assert job.error_details is not None
        assert job.error_details["code"] == "security_violation"


def test_deleted_workspace_fails_cleanly(client: TestClient, settings: Settings) -> None:
    snapshot_id, workspace = _ready_snapshot(settings, "vanishing")
    audit_id = client.post("/api/audits", json={"snapshot_id": snapshot_id}).json()["subject_id"]
    shutil.rmtree(workspace)
    Worker(settings, isolate=False).drain()
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "FAILED"
    assert "no longer exists" in audit["error"]
