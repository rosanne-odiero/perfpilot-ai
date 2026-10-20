from __future__ import annotations

import io
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from report_fixtures import COMMIT_SHA, FAKE_GITHUB_TOKEN, AuditFactory

from backend.core.config import Settings
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.enums import AuditStatus
from backend.models.orm.audit import AuditRun, ReportRecord
from backend.models.orm.platform import Job
from backend.reporting.renderers.pdf import available_pdf_engine

SHA7 = COMMIT_SHA[:7]


def _request(client: TestClient, audit_id: str, formats: list[str]) -> dict[str, Any]:
    response = client.post(f"/api/audits/{audit_id}/reports", json={"formats": formats})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def test_generate_list_get_download_and_preview(
    client: TestClient, settings: Settings, make_audit: AuditFactory
) -> None:
    audit_id = make_audit()
    accepted = _request(client, audit_id, ["html", "md", "json", "csv"])
    assert accepted["audit_id"] == audit_id
    assert [r["format"] for r in accepted["reports"]] == ["html", "md", "json", "csv"]
    assert all(
        r["status"] == "QUEUED" and r["report_status"] == "PENDING" for r in accepted["reports"]
    )
    assert len({r["job_id"] for r in accepted["reports"]}) == 4

    Worker(settings, isolate=False).drain()

    listed = client.get(f"/api/audits/{audit_id}/reports").json()
    assert {r["status"] for r in listed} == {"SUCCEEDED"}
    by_format = {r["format"]: r for r in listed}

    html_report = client.get(f"/api/reports/{by_format['html']['id']}").json()
    assert html_report["artifact_id"]
    assert html_report["files"][0]["role"] == "primary"
    assert html_report["files"][0]["filename"] == f"perfpilot-acme-shopfront-{SHA7}-audit.html"
    assert html_report["meta"]["redaction_count"] >= 2
    assert html_report["meta"]["schema_version"] == "1.0"

    download = client.get(f"/api/reports/{by_format['html']['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/html")
    assert (
        f'filename="perfpilot-acme-shopfront-{SHA7}-audit.html"'
        in download.headers["content-disposition"]
    )
    assert "Executive summary" in download.text
    assert FAKE_GITHUB_TOKEN not in download.text

    md_download = client.get(f"/api/reports/{by_format['md']['id']}/download")
    assert md_download.headers["content-type"].startswith("text/markdown")
    json_download = client.get(f"/api/reports/{by_format['json']['id']}/download")
    assert json_download.headers["content-type"] == "application/json"
    assert json.loads(json_download.content)["schema_version"] == "1.0"
    csv_download = client.get(f"/api/reports/{by_format['csv']['id']}/download")
    assert csv_download.headers["content-type"].startswith("text/csv")
    assert csv_download.text.startswith("rank,id,rule_id")
    samples = client.get(
        f"/api/reports/{by_format['csv']['id']}/download", params={"part": "benchmark_samples"}
    )
    assert samples.status_code == 404  # static audit: no benchmark samples

    preview = client.get(f"/api/reports/{by_format['html']['id']}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("text/plain")
    assert preview.headers["x-report-format"] == "html"
    assert preview.text.startswith("<!DOCTYPE html>")
    md_preview = client.get(f"/api/reports/{by_format['md']['id']}/preview")
    assert md_preview.text.startswith("# Performance audit")
    json_preview = client.get(f"/api/reports/{by_format['json']['id']}/preview")
    assert json_preview.status_code == 422


@pytest.mark.skipif(available_pdf_engine() is None, reason="no PDF engine available")
def test_pdf_report_via_api(
    client: TestClient, settings: Settings, make_audit: AuditFactory
) -> None:
    audit_id = make_audit()
    report_id = _request(client, audit_id, ["pdf"])["reports"][0]["report_id"]
    Worker(settings, isolate=False).drain()
    meta = client.get(f"/api/reports/{report_id}").json()
    assert meta["status"] == "SUCCEEDED"
    assert meta["meta"]["pdf_engine"] in {"weasyprint", "xhtml2pdf"}
    download = client.get(f"/api/reports/{report_id}/download")
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF")
    assert f"perfpilot-acme-shopfront-{SHA7}-audit.pdf" in download.headers["content-disposition"]
    assert len(PdfReader(io.BytesIO(download.content)).pages) >= 5


def test_requests_are_idempotent_per_audit_version(
    client: TestClient, settings: Settings, make_audit: AuditFactory
) -> None:
    audit_id = make_audit()
    first = _request(client, audit_id, ["html", "html", "json"])
    assert len(first["reports"]) == 2  # duplicates collapsed
    again = _request(client, audit_id, ["json", "html"])
    assert {r["report_id"] for r in again["reports"]} == {r["report_id"] for r in first["reports"]}
    assert {r["job_id"] for r in again["reports"]} == {r["job_id"] for r in first["reports"]}

    Worker(settings, isolate=False).drain()
    with session_scope() as session:
        audit = session.get(AuditRun, audit_id)
        assert audit is not None
        audit.warnings = ["re-run after config change"]  # bumps updated_at -> new version
    third = _request(client, audit_id, ["html"])
    assert third["reports"][0]["report_id"] not in {r["report_id"] for r in first["reports"]}
    assert len(client.get(f"/api/audits/{audit_id}/reports").json()) == 3


def test_failed_report_is_recorded_and_can_be_retried(
    client: TestClient,
    settings: Settings,
    make_audit: AuditFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_id = make_audit()
    import backend.reporting.service as report_service

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(report_service, "render_report", boom)
    report_id = _request(client, audit_id, ["md"])["reports"][0]["report_id"]
    Worker(settings, isolate=False).drain()
    failed = client.get(f"/api/reports/{report_id}").json()
    assert failed["status"] == "FAILED"
    assert "renderer exploded" in failed["error"]
    assert client.get(f"/api/reports/{report_id}/download").status_code == 409

    monkeypatch.undo()
    retry = _request(client, audit_id, ["md"])["reports"][0]
    assert retry["report_id"] != report_id
    Worker(settings, isolate=False).drain()
    assert client.get(f"/api/reports/{retry['report_id']}").json()["status"] == "SUCCEEDED"


def test_unknown_ids_return_404(client: TestClient) -> None:
    for path in (
        "/api/reports/nope",
        "/api/reports/nope/download",
        "/api/reports/nope/preview",
        "/api/audits/nope/reports",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"
    response = client.post("/api/audits/nope/reports", json={"formats": ["html"]})
    assert response.status_code == 404


@pytest.mark.parametrize("status", [AuditStatus.PENDING, AuditStatus.RUNNING, AuditStatus.FAILED])
def test_unfinished_audit_returns_409(
    client: TestClient, make_audit: AuditFactory, status: AuditStatus
) -> None:
    audit_id = make_audit(status=status)
    response = client.post(f"/api/audits/{audit_id}/reports", json={"formats": ["html"]})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == status.value


def test_invalid_formats_are_rejected(client: TestClient, make_audit: AuditFactory) -> None:
    audit_id = make_audit()
    for body in ({"formats": ["docx"]}, {"formats": []}):
        response = client.post(f"/api/audits/{audit_id}/reports", json=body)
        assert response.status_code == 422


def test_pending_report_cannot_be_downloaded(client: TestClient, make_audit: AuditFactory) -> None:
    audit_id = make_audit()
    report_id = _request(client, audit_id, ["html"])["reports"][0]["report_id"]
    response = client.get(f"/api/reports/{report_id}/download")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "PENDING"


def test_job_with_invalid_payload_fails_cleanly(settings: Settings) -> None:
    from backend.jobs import service as jobs

    with session_scope() as session:
        job_id = jobs.enqueue_job(session, "report.generate", {"audit_id": "x", "format": "exe"}).id
    Worker(settings, isolate=False).drain()
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "FAILED"
        assert job.error_details is not None
        assert job.error_details["code"] == "validation_failed"
        assert session.query(ReportRecord).count() == 0
