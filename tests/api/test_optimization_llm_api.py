"""Workload deep dive with the LLM patch proposer: explicit ``use_llm`` consent per request.

The model endpoint is a fake (``httpx.MockTransport``); nothing leaves the machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_support import FakeLLM, client_factory

from backend.ai import llm_client
from backend.core.config import Settings, get_settings, reset_settings_cache
from backend.jobs.worker import Worker
from backend.optimization.patch_generator import (
    PatchProposal,
    PatchRequest,
    register_patch_proposer,
)

REWRITE = """def count_duplicates(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    duplicates: dict[str, int] = {}
    for item in items:
        if counts[item] > 1:
            duplicates[item] = counts[item]
    return duplicates
"""


def _recommendation(client: TestClient, settings: Settings, slug: str) -> dict[str, Any]:
    project = client.get("/api/projects").json()[0]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    workload = next(w for w in workloads if w["slug"] == slug)
    accepted = client.post("/api/analyses", json={"workload_id": workload["id"], "size": "small"})
    assert accepted.status_code == 202, accepted.text
    Worker(settings, isolate=False).drain()
    analysis_id = accepted.json()["subject_id"]
    rec: dict[str, Any] = client.get(f"/api/analyses/{analysis_id}/recommendations").json()[0]
    return rec


class _Refusing:
    """TEST DOUBLE: a base proposer that never finds a safe patch."""

    name = "refusing-double"

    def propose(self, request: PatchRequest) -> PatchProposal:
        return PatchProposal(
            status="no_safe_patch",
            rule_id=request.target.rule_id,
            target_file=request.target.target_file,
            target_function=request.target.target_function,
            generated_by=self.name,
            original_sha256="0" * 64,
            reason="no safe deterministic patch: refused by the test double",
        )


@pytest.fixture
def fake_llm(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeLLM, Settings]]:
    monkeypatch.setenv("PERFPILOT_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("PERFPILOT_LLM_MODEL", "fake-model")
    reset_settings_cache()
    fake = FakeLLM(lambda _step, _user: {"replacement": REWRITE, "summary": "count once"})
    monkeypatch.setattr(llm_client, "create_llm_client", client_factory(fake))
    yield fake, get_settings()
    register_patch_proposer(None)


def test_use_llm_requires_a_configured_model(client: TestClient, settings: Settings) -> None:
    rec = _recommendation(client, settings, "duplicate_counter")
    response = client.post(
        "/api/optimizations", json={"recommendation_id": rec["id"], "use_llm": True}
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_configured"


def test_deterministic_patch_wins_and_the_model_is_not_asked(
    client: TestClient, fake_llm: tuple[FakeLLM, Settings]
) -> None:
    fake, settings = fake_llm
    rec = _recommendation(client, settings, "duplicate_counter")
    response = client.post(
        "/api/optimizations", json={"recommendation_id": rec["id"], "use_llm": True}
    )
    assert response.status_code == 201, response.text
    assert response.json()["generated_by"] == "deterministic"
    assert fake.calls == 0


def test_model_is_asked_only_with_consent_when_no_safe_patch_exists(
    client: TestClient, fake_llm: tuple[FakeLLM, Settings]
) -> None:
    fake, settings = fake_llm
    rec = _recommendation(client, settings, "duplicate_counter")
    register_patch_proposer(lambda _settings: _Refusing())

    without = client.post("/api/optimizations", json={"recommendation_id": rec["id"]}).json()
    assert without["state"] == "FAILED"
    assert fake.calls == 0

    with_consent = client.post(
        "/api/optimizations", json={"recommendation_id": rec["id"], "use_llm": True}
    ).json()
    assert fake.calls >= 1
    assert with_consent["generated_by"] == "llm:fake-model", with_consent["error"]
    assert with_consent["state"] == "AWAITING_APPROVAL"
    assert any("refusing-double" in note for note in with_consent["notes"])
    sent = fake.sent_text()
    assert "profiler_evidence (MEASURED)" in sent
