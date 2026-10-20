"""Runs the rule set over parsed modules and turns drafts into :class:`Finding` contracts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from backend.audit.rules import BaseRule, FindingDraft, ScopeTracker, default_rules
from backend.indexing.source import ParsedModule
from backend.models.audit_schemas import CodeLocation, Finding
from backend.models.enums import Provenance

MAX_SNIPPET_LINES = 12
CONTEXT_LINES = 2
_SUPPRESSION = re.compile(r"#\s*perfpilot:\s*ignore(?:\[(?P<ids>[A-Za-z0-9_,\s-]+)\])?")


@dataclass
class RuleFinding:
    """A finding plus the context prioritization needs (not part of the public contract)."""

    finding: Finding
    module_name: str
    loop_depth: int


@dataclass
class RuleRunResult:
    findings: list[RuleFinding] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    suppressed: int = 0


def finding_id(rule_id: str, file_path: str, line: int, code: str) -> str:
    normalized = " ".join(code.split())
    digest = hashlib.sha256(f"{rule_id}|{file_path}|{line}|{normalized}".encode()).hexdigest()
    return digest[:24]


def build_snippet(lines: list[str], line_start: int, line_end: int) -> str:
    """Exact source lines covering the finding: up to two lines of leading context, at most
    ``MAX_SNIPPET_LINES`` lines, always starting no later than ``line_start``."""
    span = line_end - line_start + 1
    first = (
        max(1, line_start - CONTEXT_LINES)
        if span + CONTEXT_LINES <= MAX_SNIPPET_LINES
        else line_start
    )
    last = min(line_end, first + MAX_SNIPPET_LINES - 1, len(lines))
    return "\n".join(lines[first - 1 : last])


def _suppressed(lines: list[str], draft: FindingDraft) -> bool:
    for number in {draft.line_start, draft.line_end}:
        if 1 <= number <= len(lines) and (match := _SUPPRESSION.search(lines[number - 1])):
            ids = match.group("ids")
            if ids is None or draft.meta.rule_id in {i.strip() for i in ids.split(",")}:
                return True
    return False


def _to_finding(module: ParsedModule, draft: FindingDraft) -> Finding:
    line_end = min(draft.line_end, max(len(module.lines), draft.line_start))
    return Finding(
        id=finding_id(draft.meta.rule_id, module.path, draft.line_start, draft.code),
        rule_id=draft.meta.rule_id,
        title=draft.meta.title,
        category=draft.meta.category,
        description=draft.description,
        severity=draft.severity,
        confidence=draft.confidence,
        provenance=Provenance.STATIC,
        location=CodeLocation(
            file_path=module.path,
            line_start=draft.line_start,
            line_end=line_end,
            symbol=draft.symbol,
        ),
        snippet=build_snippet(module.lines, draft.line_start, line_end),
        suggestion=draft.suggestion,
        tradeoffs=draft.tradeoffs,
        complexity_note=draft.complexity_note,
    )


def run_rules(
    modules: Iterable[ParsedModule],
    rules: list[BaseRule] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RuleRunResult:
    rules = rules if rules is not None else default_rules()
    parsed = [m for m in modules if m.tree is not None]
    result = RuleRunResult()
    seen: set[str] = set()
    for index, module in enumerate(parsed):
        try:
            drafts = ScopeTracker(module, rules).run()
        except RecursionError:
            result.errors[module.path] = "rules skipped: AST too deeply nested"
            continue
        for draft in drafts:
            if _suppressed(module.lines, draft):
                result.suppressed += 1
                continue
            finding = _to_finding(module, draft)
            if finding.id in seen:
                continue
            seen.add(finding.id)
            result.findings.append(RuleFinding(finding, module.module_name, draft.loop_depth))
        if progress and (index % 25 == 0 or index == len(parsed) - 1):
            progress(index + 1, len(parsed))
    return result
