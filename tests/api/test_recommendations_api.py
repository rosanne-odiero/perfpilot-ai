"""Recommendations / insights / LLM-usage / AI-status endpoints (deterministic path, no LLM)."""

from __future__ import annotations

import pytest
from audit_support import SHOPFAST, copy_into_workspace, create_snapshot, scan_inventory
from fastapi.testclient import TestClient

from backend.core.config import Settings, reset_settings_cache
from backend.jobs.worker import Worker


def _audit(client: TestClient, settings: Settings) -> str:
    workspace = copy_into_workspace(settings, SHOPFAST, "shopfast")
    snapshot_id = create_snapshot(workspace, scan_inventory(workspace))
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id})
    assert response.status_code == 202
    Worker(settings, isolate=False).drain()
    return str(response.json()["subject_id"])


def test_recommendations_insights_and_usage(client: TestClient, settings: Settings) -> None:
    audit_id = _audit(client, settings)
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    advisor = audit["summary"]["advisor"]
    assert advisor["mode"] == "deterministic"
    assert advisor["suggested_diffs"] == 2
    assert "AI_ANALYSIS" in audit["summary"]["stage_seconds"]

    recs = client.get(f"/api/audits/{audit_id}/recommendations").json()
    assert len(recs) == advisor["recommendations"] > 0
    assert [r["priority_rank"] for r in recs] == list(range(1, len(recs) + 1))
    assert {r["provenance"] for r in recs} == {"STATIC"}
    assert all(r["generated_by"] == "deterministic" for r in recs)
    assert recs[0]["kind"] == "unblock-event-loop"
    static = client.get(f"/api/audits/{audit_id}/recommendations", params={"provenance": "STATIC"})
    assert len(static.json()) == len(recs)
    ai = client.get(
        f"/api/audits/{audit_id}/recommendations", params={"provenance": "AI_HYPOTHESIS"}
    )
    assert ai.json() == []
    invalid = client.get(f"/api/audits/{audit_id}/recommendations", params={"provenance": "X"})
    assert invalid.status_code == 422

    findings = client.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()
    finding_ids = {f["id"] for f in findings["items"]}
    assert {fid for r in recs for fid in r["finding_ids"]} == finding_ids
    top = findings["items"][0]
    assert top["code_role"] == "REQUEST_HANDLER"
    assert top["execution_path"]

    insights = client.get(f"/api/audits/{audit_id}/insights").json()
    assert insights
    assert all(i["provenance"] == "STATIC" for i in insights)
    usage = client.get(f"/api/audits/{audit_id}/llm-usage").json()
    assert usage["calls"] == 0
    assert usage["stage_status"] == {"llm": "not_requested"}


@pytest.mark.parametrize("path", ["recommendations", "insights", "llm-usage"])
def test_unknown_audit_returns_404(client: TestClient, path: str) -> None:
    response = client.get(f"/api/audits/missing/{path}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_ai_status_unconfigured(client: TestClient) -> None:
    status = client.get("/api/ai/status").json()
    assert status["configured"] is False
    assert status["model"] is None
    assert status["base_url_host"] is None
    assert status["consent_required"] is True
    assert status["default_advisor"] == "deterministic"
    assert "redacted" in status["data_sent"]
    assert status["budget"]["max_tokens_per_audit"] > 0
    assert "recommendations@1" in status["prompt_version"]


def test_ai_status_configured_shows_host_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "sk-statuscheck-0123456789abcdefghij"
    monkeypatch.setenv("PERFPILOT_LLM_BASE_URL", "https://user:pw@llm.example.test:8443/v1?k=1")
    monkeypatch.setenv("PERFPILOT_LLM_MODEL", "gpt-test")
    monkeypatch.setenv("PERFPILOT_LLM_API_KEY", key)
    reset_settings_cache()
    response = client.get("/api/ai/status")
    status = response.json()
    assert status["configured"] is True
    assert status["model"] == "gpt-test"
    assert status["base_url_host"] == "llm.example.test:8443"
    assert status["api_key_set"] is True
    assert key not in response.text
    assert "pw" not in status["base_url_host"]
