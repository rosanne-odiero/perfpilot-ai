"""Citation guard: model output is accepted only when it points at real, indexed code.

* Every cited file must be an indexed module (sensitive files are never indexed); the line range
  must lie within the file; a cited symbol must exist in that file and overlap the range.
  Invalid citations are dropped and counted (``LLMUsage.rejected_citations``).
* Unit ids (``module:qualname``) and module names must exist in the index.
* The model never ran the code, so statements claiming runtime measurements (milliseconds,
  seconds, MB, "% faster", "3x speedup", …) are stripped sentence by sentence and counted
  (``rejected_claims``). An item whose required text or citations disappear is rejected
  (``rejected_items``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from backend.ai.schemas import Citation, InsightDraft, RecommendationDraft
from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    CodeLocation,
    ModuleInfo,
    Recommendation,
    SymbolInfo,
)
from backend.models.enums import Provenance

RUNTIME_CLAIM = re.compile(
    r"""(?ix)
    (?<![\w.])\d[\d,]*(?:\.\d+)?\s*-?\s*
    (?:
        ms|msecs?|milliseconds?|µs|us|microseconds?|ns|nanoseconds?
      | s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?
      | [kmgt]i?b|bytes?|kilobytes?|megabytes?|gigabytes?
      | %|percent
      | x|\u00d7
      | (?:times|fold)\s+(?:faster|slower|quicker|less|more|fewer|speed\w*)
      | fold
      | rps|qps|req(?:uests)?/s(?:ec)?|ops/s(?:ec)?|requests\s+per\s+second
    )(?![a-z0-9])
    """
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def contains_runtime_claim(text: str) -> bool:
    return bool(RUNTIME_CLAIM.search(text))


@dataclass
class GuardStats:
    rejected_citations: int = 0
    rejected_claims: int = 0
    rejected_items: int = 0


@dataclass
class _ModuleEntry:
    module: ModuleInfo
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)


class CitationGuard:
    def __init__(self, index: CodeIndex, finding_ids: Iterable[str] = ()) -> None:
        self.stats = GuardStats()
        self.finding_ids = set(finding_ids)
        self.by_path: dict[str, _ModuleEntry] = {}
        self.module_names = {m.module_name for m in index.modules}
        for module in index.modules:
            if module.parse_error:
                continue
            entry = _ModuleEntry(module, {s.qualname: s for s in module.symbols})
            self.by_path[module.file_path] = entry
        self.unit_ids = {
            f"{e.module.module_name}:{q}" for e in self.by_path.values() for q in e.symbols
        }

    # -------------------------------------------------------------- primitives

    def _symbol(self, entry: _ModuleEntry, name: str) -> SymbolInfo | None:
        if ":" in name:
            module_name, _, name = name.partition(":")
            if module_name != entry.module.module_name:
                return None
        if name in entry.symbols:
            return entry.symbols[name]
        matches = [s for q, s in entry.symbols.items() if q.rsplit(".", 1)[-1] == name]
        return matches[0] if len(matches) == 1 else None

    def location(self, citation: Citation) -> CodeLocation | None:
        """The citation as a verified :class:`CodeLocation`, or ``None`` (counted) if invalid."""
        path = citation.file_path.strip().removeprefix("./")
        pure = PurePosixPath(path)
        entry = self.by_path.get(pure.as_posix())
        valid = (
            entry is not None
            and "\\" not in path
            and not pure.is_absolute()
            and ".." not in pure.parts
            and 1 <= citation.line_start <= citation.line_end <= entry.module.line_count
        )
        symbol_name: str | None = None
        if valid and entry is not None and citation.symbol:
            symbol = self._symbol(entry, citation.symbol)
            valid = symbol is not None and (
                citation.line_start <= symbol.line_end and citation.line_end >= symbol.line_start
            )
            symbol_name = symbol.qualname if symbol else None
        if not valid or entry is None:
            self.stats.rejected_citations += 1
            return None
        return CodeLocation(
            file_path=entry.module.file_path,
            line_start=citation.line_start,
            line_end=citation.line_end,
            symbol=symbol_name,
        )

    def locations(self, citations: Iterable[Citation]) -> list[CodeLocation]:
        seen: dict[tuple[str, int, int, str | None], CodeLocation] = {}
        for citation in citations:
            loc = self.location(citation)
            if loc is not None:
                seen.setdefault((loc.file_path, loc.line_start, loc.line_end, loc.symbol), loc)
        return list(seen.values())

    def units(self, unit_ids: Iterable[str]) -> list[str]:
        valid = []
        for unit in unit_ids:
            if unit in self.unit_ids:
                valid.append(unit)
            else:
                self.stats.rejected_citations += 1
        return valid

    def modules(self, names: Iterable[str]) -> list[str]:
        valid = []
        for name in names:
            if name in self.module_names:
                valid.append(name)
            else:
                self.stats.rejected_citations += 1
        return list(dict.fromkeys(valid))

    def text(self, text: str) -> str:
        """``text`` without the sentences that claim runtime measurements."""
        kept: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(text):
            if not sentence.strip():
                continue
            if contains_runtime_claim(sentence):
                self.stats.rejected_claims += 1
            else:
                kept.append(sentence.strip())
        return " ".join(kept)

    # -------------------------------------------------------------- items

    def recommendation(self, draft: RecommendationDraft, model: str) -> Recommendation | None:
        citations = self.locations(draft.citations)
        fields = {
            name: self.text(getattr(draft, name))
            for name in (
                "title",
                "summary",
                "rationale",
                "proposed_strategy",
                "uncertainty",
                "expected_effect",
                "correctness_risks",
            )
        }
        required = ("title", "summary", "proposed_strategy", "uncertainty", "expected_effect")
        if not citations or any(not fields[name] for name in required):
            self.stats.rejected_items += 1
            return None
        finding_ids = [f for f in dict.fromkeys(draft.finding_ids) if f in self.finding_ids]
        digest = hashlib.sha256(
            "|".join(
                [model, fields["title"], *(f"{c.file_path}:{c.line_start}" for c in citations)]
            ).encode()
        ).hexdigest()[:20]
        return Recommendation(
            id=f"ai-{digest}",
            title=fields["title"],
            summary=fields["summary"],
            rationale=fields["rationale"] or fields["summary"],
            proposed_strategy=fields["proposed_strategy"],
            finding_ids=finding_ids,
            citations=citations,
            provenance=Provenance.AI_HYPOTHESIS,
            effort=draft.effort,
            uncertainty=fields["uncertainty"],
            expected_effect=fields["expected_effect"],
            correctness_risks=fields["correctness_risks"],
            suggested_diff=None,
            generated_by=model,
            kind="ai-hypothesis",
        )

    def insight(self, draft: InsightDraft, model: str) -> ArchitectureInsight | None:
        citations = self.locations(draft.citations)
        modules = self.modules(draft.modules)
        title = self.text(draft.title)
        description = self.text(draft.description)
        if not title or not description or not (citations or modules):
            self.stats.rejected_items += 1
            return None
        digest = hashlib.sha256(f"{model}|{title}".encode()).hexdigest()[:20]
        return ArchitectureInsight(
            id=f"ai-ins-{digest}",
            title=title,
            description=description,
            modules=modules,
            citations=citations,
            provenance=Provenance.AI_HYPOTHESIS,
            generated_by=model,
        )
