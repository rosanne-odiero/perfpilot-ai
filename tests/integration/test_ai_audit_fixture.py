"""End-to-end audit of ShopFast with a mocked LLM: only valid model output is persisted."""

from __future__ import annotations

import json
from typing import Any

import pytest
from audit_support import SHOPFAST, copy_into_workspace, create_snapshot, scan_inventory
from fastapi.testclient import TestClient
from llm_support import EMPTY_OUTPUT, FakeLLM, client_factory, finding_ids_in, llm_settings
from sqlalchemy import select

from backend.ai import llm_client
from backend.core.config import Settings
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.orm.audit import RecommendationRecord


def responder(step: str, user: str) -> Any:
    if step != "recommendations":
        return EMPTY_OUTPUT[step]
    ids = finding_ids_in(user, "shopfast/api.py")
    if not ids:
        return EMPTY_OUTPUT[step]
    return {
        "recommendations": [
            {
                "title": "Replace time.sleep with asyncio.sleep in get_product",
                "summary": "get_product blocks the event loop with time.sleep.",
                "rationale": "PP-ASYNC-001 flags a blocking sleep inside async def get_product.",
                "proposed_strategy": "Use `await asyncio.sleep(...)` as get_product_polite does.",
                "finding_ids": ids[:1],
                "citations": [
                    {
                        "file_path": "shopfast/api.py",
                        "line_start": 18,
                        "line_end": 20,
                        "symbol": "get_product",
                    }
                ],
                "effort": "SMALL",
                "uncertainty": "Request concurrency in production is unknown.",
                "expected_effect": "Other requests keep running while the handler waits.",
                "correctness_risks": "None beyond rate-limiting semantics.",
            },
            {
                "title": "Cache payment lookups",
                "summary": "charge_card repeats provider lookups.",
                "rationale": "The payments module calls the provider per item.",
                "proposed_strategy": "Memoize the provider lookup.",
                "finding_ids": ["does-not-exist"],
                "citations": [
                    {
                        "file_path": "shopfast/payments.py",
                        "line_start": 10,
                        "line_end": 22,
                        "symbol": "charge_card",
                    }
                ],
                "effort": "SMALL",
                "uncertainty": "Unknown.",
                "expected_effect": "Saves 300ms per checkout.",
                "correctness_risks": "Stale cache.",
            },
        ]
    }


def test_audit_with_mocked_llm_keeps_only_valid_recommendations(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM(responder)
    monkeypatch.setattr(llm_client, "create_llm_client", client_factory(fake))
    workspace = copy_into_workspace(settings, SHOPFAST, "shopfast")
    secret = (workspace / ".env").read_text().split("=", 1)[1].strip()
    snapshot_id = create_snapshot(workspace, scan_inventory(workspace))
    response = client.post(
        "/api/audits", json={"snapshot_id": snapshot_id, "use_llm": True, "llm_consent": True}
    )
    assert response.status_code == 202
    Worker(llm_settings(settings), isolate=False).drain()
    audit_id = response.json()["subject_id"]

    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    assert audit["summary"]["advisor"]["mode"] == "deterministic+llm"
    assert fake.calls >= 5
    assert secret not in fake.sent_text()

    ai = client.get(
        f"/api/audits/{audit_id}/recommendations", params={"provenance": "AI_HYPOTHESIS"}
    ).json()
    assert len(ai) == 1
    rec = ai[0]
    assert rec["title"].startswith("Replace time.sleep")
    assert rec["generated_by"] == "fake-model"
    assert rec["citations"][0]["file_path"] == "shopfast/api.py"
    assert rec["citations"][0]["symbol"] == "get_product"
    assert rec["suggested_diff"] is None
    everything = client.get(f"/api/audits/{audit_id}/recommendations").json()
    assert any(r["provenance"] == "STATIC" for r in everything)
    assert all("payments" not in json.dumps(r["citations"]) for r in everything)
    with session_scope() as session:
        rows = session.scalars(
            select(RecommendationRecord).where(RecommendationRecord.audit_id == audit_id)
        ).all()
        assert {r.provenance for r in rows} == {"STATIC", "AI_HYPOTHESIS"}
        assert len(rows) == len(everything)

    usage = client.get(f"/api/audits/{audit_id}/llm-usage").json()
    assert usage["model"] == "fake-model"
    assert usage["calls"] == fake.calls
    assert usage["rejected_citations"] >= 1
    assert usage["rejected_items"] >= 1
    assert usage["fallbacks"] == 0
    assert not usage["budget_exhausted"]

    report = client.post(f"/api/audits/{audit_id}/reports", json={"formats": ["json"]})
    assert report.status_code == 202
    Worker(llm_settings(settings), isolate=False).drain()
    report_id = report.json()["reports"][0]["report_id"]
    document = client.get(f"/api/reports/{report_id}/download").json()
    strategies = document["strategies"]["entries"]
    assert any(
        s["provenance"] == "AI_HYPOTHESIS" and s["generated_by"] == "fake-model" for s in strategies
    )
    assert any(s["provenance"] == "STATIC" for s in strategies)
