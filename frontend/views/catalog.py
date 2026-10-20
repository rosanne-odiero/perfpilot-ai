"""Joins audits with their snapshot and repository so lists can show human labels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frontend import data
from frontend.api_client import ApiError
from frontend.formatting import SOURCE_LABELS, fmt_dt, short_sha


@dataclass(frozen=True)
class AuditRow:
    id: str
    snapshot_id: str
    repository_id: str | None
    repo_name: str
    source_type: str | None
    source_locator: str | None
    commit_sha: str | None
    branch: str | None
    status: str
    stage: str
    mode: str
    use_llm: bool
    findings_total: int | None
    created_at: str
    job_id: str | None

    @property
    def label(self) -> str:
        sha = f" @ {short_sha(self.commit_sha)}" if self.commit_sha else ""
        return f"{self.repo_name}{sha} · {self.status.lower()} · {fmt_dt(self.created_at)}"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_type or "", self.source_type or "unknown source")


def _repo_index() -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in data.repositories()}


def to_row(audit: dict[str, Any], repos: dict[str, dict[str, Any]]) -> AuditRow:
    try:
        snapshot = data.snapshot(audit["snapshot_id"])
    except ApiError:
        snapshot = {}
    repo = repos.get(snapshot.get("repository_id", ""), {})
    summary = audit.get("summary") or {}
    return AuditRow(
        id=audit["id"],
        snapshot_id=audit["snapshot_id"],
        repository_id=snapshot.get("repository_id"),
        repo_name=repo.get("name") or snapshot.get("root_name") or "unknown repository",
        source_type=repo.get("source_type"),
        source_locator=repo.get("source_locator"),
        commit_sha=snapshot.get("commit_sha"),
        branch=snapshot.get("branch"),
        status=audit["status"],
        stage=audit["stage"],
        mode=audit["mode"],
        use_llm=bool(audit.get("use_llm")),
        findings_total=summary.get("findings_total"),
        created_at=audit["created_at"],
        job_id=audit.get("job_id"),
    )


def audit_rows(limit: int = 100) -> list[AuditRow]:
    repos = _repo_index()
    return [to_row(a, repos) for a in data.audits(limit)]


def audit_row(audit_id: str) -> AuditRow:
    return to_row(data.audit(audit_id), _repo_index())
