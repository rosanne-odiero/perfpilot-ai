from __future__ import annotations

from dataclasses import replace
from typing import Any

from frontend.views.audit_context import AuditContext
from frontend.views.catalog import AuditRow

ROW = AuditRow(
    id="a1",
    snapshot_id="s1",
    repository_id="r1",
    repo_name="pallets/itsdangerous",
    source_type="GITHUB_PUBLIC",
    source_locator="https://github.com/pallets/itsdangerous",
    commit_sha="0123456789abcdef0123456789abcdef01234567",
    branch="main",
    status="SUCCEEDED",
    stage="COMPLETED",
    mode="STATIC",
    use_llm=False,
    findings_total=1,
    created_at="2026-10-20T17:40:28",
    job_id="j1",
)


def finding(path: str, start: int, end: int) -> dict[str, Any]:
    return {"location": {"file_path": path, "line_start": start, "line_end": end}}


def test_findings_link_to_github_at_the_audited_commit() -> None:
    context = AuditContext(audit={"id": "a1"}, row=ROW, snapshot={})
    assert context.permalink(finding("src/itsdangerous/signer.py", 10, 12)) == (
        "https://github.com/pallets/itsdangerous/blob/"
        "0123456789abcdef0123456789abcdef01234567/src/itsdangerous/signer.py#L10-L12"
    )


def test_no_link_for_local_sources_or_missing_commit() -> None:
    local = AuditContext(
        audit={"id": "a1"},
        row=replace(ROW, source_type="LOCAL_DIRECTORY", source_locator="/srv/x"),
        snapshot={},
    )
    assert local.permalink(finding("a.py", 1, 1)) is None
    no_sha = AuditContext(audit={"id": "a1"}, row=replace(ROW, commit_sha=None), snapshot={})
    assert no_sha.permalink(finding("a.py", 1, 1)) is None


def test_labels() -> None:
    assert ROW.label == "pallets/itsdangerous @ 01234567 · succeeded · 2026-10-20 17:40 UTC"
    assert ROW.source_label == "GitHub"
