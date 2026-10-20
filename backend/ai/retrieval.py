"""Selective context retrieval for LLM calls: only what a group of findings needs.

For each finding the retriever returns the enclosing function (numbered lines so the model can
cite them; long functions are cut to windows around the flagged lines) plus a small summary of the
module's import neighborhood. Blocks are added in priority order until the token cap is reached;
the repository is never sent wholesale. Sensitive files are never included and every block is
redacted (the client redacts again right before sending).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from backend.ai.budget import estimate_tokens
from backend.ai.outbound import may_send_file
from backend.core.redaction import redact_text
from backend.models.audit_schemas import CodeIndex, Finding, ModuleInfo, SymbolInfo

MAX_FUNCTION_LINES = 80
WINDOW_RADIUS = 12
MODULE_WINDOW_RADIUS = 8
MAX_NEIGHBORS = 6


@dataclass
class RetrievedContext:
    text: str = ""
    tokens: int = 0
    redactions: int = 0
    truncated: bool = False
    units: list[str] = field(default_factory=list)


def _merge_windows(lines: Iterable[int], radius: int, low: int, high: int) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        start, end = max(low, line - radius), min(high, line + radius)
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


class ContextRetriever:
    def __init__(self, index: CodeIndex, sources: Mapping[str, str]) -> None:
        self.index = index
        self.sources = sources
        self.modules = {m.file_path: m for m in index.modules}
        self.by_name = {m.module_name: m for m in index.modules}
        self.nodes = {n.module_name: n for n in index.graph.nodes}
        self.importers: dict[str, list[str]] = {}
        for edge in index.graph.edges:
            self.importers.setdefault(edge.target, []).append(edge.source)

    def enclosing_symbol(self, file_path: str, line: int) -> SymbolInfo | None:
        module = self.modules.get(file_path)
        if module is None:
            return None
        candidates = [
            s for s in module.symbols if s.kind != "class" and s.line_start <= line <= s.line_end
        ]
        return min(candidates, key=lambda s: s.line_end - s.line_start, default=None)

    def unit_id(self, file_path: str, line: int) -> str | None:
        module = self.modules.get(file_path)
        symbol = self.enclosing_symbol(file_path, line)
        if module is None or symbol is None:
            return None
        return f"{module.module_name}:{symbol.qualname}"

    def code_block(self, file_path: str, focus: list[int]) -> tuple[str, int] | None:
        """Numbered source of the function around ``focus`` lines (redacted), or ``None``."""
        text = self.sources.get(file_path)
        module = self.modules.get(file_path)
        if text is None or module is None or not focus or not may_send_file(file_path):
            return None
        lines = text.split("\n")
        symbol = self.enclosing_symbol(file_path, focus[0])
        if symbol is not None:
            low, high = symbol.line_start, min(symbol.line_end, len(lines))
            header = f"# {file_path} :: {symbol.qualname} ({symbol.kind}, lines {low}-{high})"
            if high - low + 1 <= MAX_FUNCTION_LINES:
                windows = [(low, high)]
            else:
                windows = _merge_windows([low, *focus], WINDOW_RADIUS, low, high)
        else:
            header = f"# {file_path} :: module level"
            windows = _merge_windows(focus, MODULE_WINDOW_RADIUS, 1, len(lines))
        rendered = [header]
        for index, (start, end) in enumerate(windows):
            if index:
                rendered.append("      | ...")
            rendered.extend(f"{n:>5} | {lines[n - 1]}" for n in range(start, end + 1))
        result = redact_text("\n".join(rendered))
        return result.text, result.count

    def neighborhood(self, module: ModuleInfo) -> str:
        node = self.nodes.get(module.module_name)
        imports = sorted(
            {i.resolved_module for i in module.imports if i.is_internal and i.resolved_module}
        )
        importers = sorted(self.importers.get(module.module_name, []))
        parts = [
            f"module {module.module_name} ({module.file_path}): "
            f"fan_in={node.fan_in if node else 0}, fan_out={node.fan_out if node else 0}"
        ]
        if module.entrypoint_reasons:
            parts.append(f"  entry points: {'; '.join(module.entrypoint_reasons[:3])}")
        if imports:
            parts.append(f"  imports: {', '.join(imports[:MAX_NEIGHBORS])}")
        if importers:
            listed = ", ".join(importers[:MAX_NEIGHBORS])
            more = (
                f" (+{len(importers) - MAX_NEIGHBORS} more)"
                if len(importers) > MAX_NEIGHBORS
                else ""
            )
            parts.append(f"  imported by: {listed}{more}")
        return "\n".join(parts)

    def for_findings(self, findings: list[Finding], token_cap: int) -> RetrievedContext:
        """Function bodies and neighborhoods for ``findings`` (already in priority order)."""
        context = RetrievedContext()
        focus: dict[tuple[str, str | None], list[int]] = {}
        for finding in findings:
            symbol = self.enclosing_symbol(finding.location.file_path, finding.location.line_start)
            key = (finding.location.file_path, symbol.qualname if symbol else None)
            focus.setdefault(key, []).append(finding.location.line_start)
        blocks: list[str] = []
        seen_modules: set[str] = set()
        for (file_path, _), lines in focus.items():
            block = self.code_block(file_path, lines)
            if block is None:
                continue
            code, redactions = block
            module = self.modules[file_path]
            extra = ""
            if module.module_name not in seen_modules:
                extra = self.neighborhood(module) + "\n"
            piece = extra + code
            cost = estimate_tokens(piece)
            if context.tokens + cost > token_cap:
                context.truncated = True
                continue
            seen_modules.add(module.module_name)
            blocks.append(piece)
            context.tokens += cost
            context.redactions += redactions
            if unit := self.unit_id(file_path, lines[0]):
                context.units.append(unit)
        context.text = "\n\n".join(blocks)
        return context
