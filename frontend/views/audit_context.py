"""Everything the audit result sections share, loaded once per rerun."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frontend import data
from frontend.api_client import ApiError
from frontend.components.findings_table import SourceContext
from frontend.formatting import github_permalink
from frontend.views.catalog import AuditRow

FINDINGS_MODULE_KEY = "findings_module_filter"
TAB_KEY = "audit_tab"


@dataclass(frozen=True)
class AuditContext:
    audit: dict[str, Any]
    row: AuditRow
    snapshot: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.audit["id"])

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self.audit.get("summary") or {})

    def findings(self) -> tuple[list[dict[str, Any]], int]:
        return data.findings(self.id)

    def load_source(self, path: str) -> dict[str, Any] | None:
        try:
            return data.file_content(self.row.snapshot_id, path)
        except ApiError:
            return None

    def permalink(self, finding: dict[str, Any]) -> str | None:
        loc = finding["location"]
        return github_permalink(
            self.row.source_type,
            self.row.source_locator,
            self.row.commit_sha,
            loc["file_path"],
            loc["line_start"],
            loc["line_end"],
        )

    def source_context(self) -> SourceContext:
        return SourceContext(load_source=self.load_source, permalink=self.permalink)
