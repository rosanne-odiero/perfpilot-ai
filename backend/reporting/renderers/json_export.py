"""JSON renderer: the ``AuditReport`` model dump (``schema_version`` included at the top level)."""

from __future__ import annotations

import json

from backend.reporting.model import AuditReport
from backend.reporting.redaction import redact_value


def render_json(report: AuditReport) -> tuple[str, int]:
    """Return the JSON text and the number of values redacted in this final pass."""
    data, count = redact_value(report.model_dump(mode="json"))
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", count
