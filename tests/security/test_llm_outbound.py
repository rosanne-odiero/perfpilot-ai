"""LLM outbound safety: redaction before sending, sensitive files never sent, consent gating."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from audit_support import SHOPFAST, copy_into_workspace, create_snapshot, scan_inventory
from fastapi.testclient import TestClient
from llm_support import FAKE_KEY, FakeLLM, client_factory, llm_settings

from backend.ai import llm_client
from backend.ai.analysis import LLM_NOT_CONFIGURED_WARNING, run_ai_analysis
from backend.audit.pipeline import STATIC_STAGES, AuditState, run_pipeline
from backend.core.config import Settings
from backend.jobs.worker import Worker

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
DB_PASSWORD = "postgres://svc:Sup3rSecretPassw0rd@db.internal:5432/app"


def _secret_repo(root: Path) -> None:
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "__init__.py").write_text("")
    (root / "svc" / "sync.py").write_text(
        textwrap.dedent(
            f'''
            def build_payload(items):
                token = "{GITHUB_TOKEN}"
                dsn = "{DB_PASSWORD}"
                out = ""
                for item in items:
                    out += token + dsn + item
                return out
            '''
        )
    )
    (root / ".env").write_text("LLM_LEAK_CANARY=env-secret-value-0987654321\n")


def _state(root: Path) -> AuditState:
    state = AuditState(root=root, inventory=scan_inventory(root), settings=Settings())
    run_pipeline(state, STATIC_STAGES)
    return state


def _analyze(
    state: AuditState, cfg: Settings, fake: FakeLLM, *, use_llm: bool, consent: bool
) -> list[str]:
    build = state.require_build()
    result = run_ai_analysis(
        findings=state.findings,
        index=build.index,
        sources={p: m.source for p, m in build.parsed.items()},
        settings=cfg,
        use_llm=use_llm,
        llm_consent=consent,
        root=state.root,
        client_factory=client_factory(fake),
    )
    return result.warnings


def test_secrets_in_snippets_never_reach_the_transport(tmp_path: Path, settings: Settings) -> None:
    _secret_repo(tmp_path)
    state = _state(tmp_path)
    assert any(f.location.file_path == "svc/sync.py" for f in state.findings)
    fake = FakeLLM()
    _analyze(state, llm_settings(settings), fake, use_llm=True, consent=True)
    sent = fake.sent_text()
    assert fake.calls > 0
    assert "build_payload" in sent  # the snippet itself was sent...
    assert "[REDACTED]" in sent  # ...with its secrets replaced
    for secret in (GITHUB_TOKEN, "Sup3rSecretPassw0rd", "env-secret-value-0987654321", FAKE_KEY):
        assert secret not in sent
    for headers in fake.headers:
        assert headers["authorization"] == f"Bearer {FAKE_KEY}"  # only ever in the auth header


@pytest.mark.parametrize(("use_llm", "consent"), [(False, False), (False, True), (True, False)])
def test_no_http_without_request_and_consent(
    tmp_path: Path, settings: Settings, use_llm: bool, consent: bool
) -> None:
    _secret_repo(tmp_path)
    fake = FakeLLM()
    _analyze(_state(tmp_path), llm_settings(settings), fake, use_llm=use_llm, consent=consent)
    assert fake.calls == 0


def test_no_http_when_not_configured(tmp_path: Path, settings: Settings) -> None:
    _secret_repo(tmp_path)
    fake = FakeLLM()
    warnings = _analyze(_state(tmp_path), settings, fake, use_llm=True, consent=True)
    assert fake.calls == 0
    assert warnings == [LLM_NOT_CONFIGURED_WARNING]


def test_audit_job_without_consent_never_creates_a_client(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_cfg: Settings) -> llm_client.LLMClient:
        raise AssertionError("an LLM client must not be created without use_llm + consent")

    monkeypatch.setattr(llm_client, "create_llm_client", forbidden)
    workspace = copy_into_workspace(settings, SHOPFAST, "shopfast")
    snapshot_id = create_snapshot(workspace, scan_inventory(workspace))
    response = client.post("/api/audits", json={"snapshot_id": snapshot_id})
    assert response.status_code == 202
    Worker(llm_settings(settings), isolate=False).drain()
    audit_id = response.json()["subject_id"]
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    usage = client.get(f"/api/audits/{audit_id}/llm-usage").json()
    assert usage["calls"] == 0
    assert usage["stage_status"] == {"llm": "not_requested"}
    rejected = client.post("/api/audits", json={"snapshot_id": snapshot_id, "use_llm": True})
    assert rejected.status_code == 422
