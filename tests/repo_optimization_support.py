"""Shared helpers for repository-optimization tests (API flow on the ShopFast fixture)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from runtime_support import WORKLOAD_ARGV, approve, command_by_argv, drain

from backend.core.config import Settings
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_generator import PatchProposal, PatchRequest

PYTEST_ARGV = ["python", "-m", "pytest", "-q"]
VALID_CLASSIFICATIONS = {"IMPROVED", "REGRESSED", "NO_CLEAR_DIFFERENCE", "INCONCLUSIVE"}


def profile(client: TestClient, settings: Settings, audit_id: str, argv: list[str]) -> str:
    accepted = approve(client, audit_id, command_by_argv(client, audit_id, argv))
    drain(settings)
    run = client.get(f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}").json()
    assert run["status"] == "SUCCEEDED", (run["failure_reason"], run["stderr_tail"])
    return str(run["id"])


def approve_tests_and_workload(client: TestClient, settings: Settings, audit_id: str) -> None:
    """Profile the test suite first and the workload last, so the findings carry the workload's
    MEASURED evidence (the most recent successful run wins)."""
    profile(client, settings, audit_id, PYTEST_ARGV)
    profile(client, settings, audit_id, WORKLOAD_ARGV)


def finding(
    client: TestClient, audit_id: str, rule_id: str, file_path: str, line: int | None = None
) -> dict[str, Any]:
    items = client.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()["items"]
    found: dict[str, Any] = next(
        f
        for f in items
        if f["rule_id"] == rule_id
        and f["location"]["file_path"] == file_path
        and (line is None or f["location"]["line_start"] == line)
    )
    return found


def create(client: TestClient, audit_id: str, finding_id: str) -> dict[str, Any]:
    response = client.post(f"/api/audits/{audit_id}/optimizations", json={"finding_id": finding_id})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def candidate(client: TestClient, candidate_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/repo-optimizations/{candidate_id}").json()
    return body


def validate(client: TestClient, settings: Settings, candidate_id: str) -> dict[str, Any]:
    response = client.post(f"/api/repo-optimizations/{candidate_id}/validate")
    assert response.status_code == 202, response.text
    drain(settings)
    return candidate(client, candidate_id)


def benchmark(
    client: TestClient, settings: Settings, candidate_id: str, **body: Any
) -> dict[str, Any]:
    response = client.post(f"/api/repo-optimizations/{candidate_id}/benchmark", json=body)
    assert response.status_code == 202, response.text
    drain(settings)
    return candidate(client, candidate_id)


class FixedProposer:
    """TEST DOUBLE of a patch proposer (e.g. a misbehaving model): returns a hand-written diff of
    ``path`` produced by ``transform``. Installed with ``register_patch_proposer``."""

    name = "test-double-proposer"

    def __init__(self, path: str, transform: Callable[[str], str]) -> None:
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
            summary="hand-written test proposal",
        )


def reverse_reconcile(text: str) -> str:
    """Plausible but wrong: the matches come back in reverse order."""
    patched = text.replace(
        "    return matched\n\n\ndef reconcile_indexed",
        "    return matched[::-1]\n\n\ndef reconcile_indexed",
    )
    assert patched != text
    return patched
