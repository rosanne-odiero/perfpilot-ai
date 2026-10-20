"""Hierarchical LLM pipeline (optional; runs only with consent and a configured endpoint).

Steps, each one structured-output call (or a few batched calls) validated by Pydantic models in
:mod:`backend.ai.schemas`:

1. **Unit summaries** — only functions with findings or on their execution paths; cached on disk
   by (model, prompt versions, file sha256, unit).
2. **Dependency context** — the import-graph neighborhood of the modules involved.
3. **Likely hot paths** — static execution paths ranked by the model (hypotheses).
4. **Cross-module correlation** — architectural insights spanning modules.
5. **Prioritized recommendations** — for the top finding groups, with retrieved function source.

Every step degrades on its own: malformed output gets one corrective retry and then the
deterministic substitute (counted in ``fallbacks``); an exhausted audit budget or an unavailable
endpoint stops the remaining steps (``budget_exhausted`` / ``stage_status``). All model output
passes the citation guard before it is kept.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from backend.ai.budget import BudgetExhaustedError, TokenBudget, estimate_messages
from backend.ai.citation_guard import CitationGuard
from backend.ai.deterministic_advisor import FindingGroup, first_sentence
from backend.ai.llm_client import ChatCompletion, LLMError
from backend.ai.outbound import ChatMessage
from backend.ai.prompt_loader import load_prompt, pipeline_version
from backend.ai.retrieval import ContextRetriever
from backend.ai.schemas import (
    Correlations,
    DependencyContext,
    HotPaths,
    RecommendationDrafts,
    UnitSummaries,
    UnitSummary,
)
from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    CodeLocation,
    Finding,
    LLMUsage,
    Recommendation,
)
from backend.models.enums import Provenance

STEP_OUTPUT_TOKENS = {
    "unit_summaries": 1500,
    "dependency_context": 1200,
    "hot_paths": 1200,
    "correlation": 1500,
    "recommendations": 2500,
}
STRUCTURED_RETRIES = 1
MAX_UNITS = 16
UNITS_PER_CALL = 4
MAX_MODULES = 10
MAX_PATHS = 8
MAX_HOT_PATH_INSIGHTS = 3
MAX_CORRELATION_INSIGHTS = 5
MAX_LLM_GROUPS = 8
GROUPS_PER_CALL = 4
FINDINGS_PER_GROUP = 6
MAX_GROUP_LINES = 30

Model = TypeVar("Model", bound=BaseModel)
ProgressFn = Callable[[float, str], None]


class ChatClient(Protocol):
    model: str
    redactions: int

    def complete(self, messages: list[ChatMessage], *, max_tokens: int) -> ChatCompletion: ...


class StepFailedError(Exception):
    """The model's output for a step could not be validated after the bounded retries."""


class _AbortError(Exception):
    """Stop all remaining model calls (budget spent or endpoint unavailable)."""


def parse_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the response")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("the response is not a JSON object")
    return value


def _rejection_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first.get("loc", ()))
        return f"{exc.error_count()} schema error(s); first at '{location}': {first.get('msg')}"
    return "the reply was not a single valid JSON object"


class SummaryCache:
    """Unit summaries on disk, keyed by model, prompt versions, file sha256 and unit id."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _path(self, key: str) -> Path | None:
        return self.directory / key[:2] / f"{key}.json" if self.directory else None

    def get(self, key: str) -> UnitSummary | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None
        try:
            return UnitSummary.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def put(self, key: str, summary: UnitSummary) -> None:
        path = self._path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary.model_dump_json(), encoding="utf-8")


@dataclass
class PipelineResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    insights: list[ArchitectureInsight] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    warnings: list[str] = field(default_factory=list)


class HierarchicalPipeline:
    def __init__(
        self,
        client: ChatClient,
        *,
        index: CodeIndex,
        findings: list[Finding],
        groups: list[FindingGroup],
        sources: dict[str, str],
        budget: TokenBudget,
        cache: SummaryCache,
        input_cost_per_1k: float = 0.0,
        output_cost_per_1k: float = 0.0,
        progress: ProgressFn | None = None,
    ) -> None:
        self.client = client
        self.index = index
        self.findings = findings
        self.groups = groups
        self.sources = sources
        self.budget = budget
        self.cache = cache
        self.costs = (input_cost_per_1k, output_cost_per_1k)
        self.progress = progress
        self.retriever = ContextRetriever(index, sources)
        self.guard = CitationGuard(index, (f.id for f in findings))
        self.version = pipeline_version()
        self.usage = LLMUsage(
            model=client.model, budget_tokens=budget.total, prompt_version=self.version
        )
        self.result = PipelineResult(usage=self.usage)
        self.aborted = False
        self.context_redactions = 0
        self.symbols = {
            f"{m.module_name}:{s.qualname}": (m, s) for m in index.modules for s in m.symbols
        }
        self.file_sha = {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in sources.items()
        }

    # ------------------------------------------------------------------ plumbing

    def _report(self, fraction: float, message: str) -> None:
        if self.progress:
            self.progress(fraction, message)

    def _messages(self, step: str, payload: str, schema: type[BaseModel]) -> list[ChatMessage]:
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        user = (
            f"{load_prompt(step).text}\n\nRespond with one JSON object matching this JSON "
            f"schema:\n{schema_json}\n\nINPUT\n{payload}"
        )
        return [ChatMessage("system", load_prompt("system").text), ChatMessage("user", user)]

    def context_allowance(self, step: str, schema: type[BaseModel]) -> int:
        overhead = estimate_messages(m.content for m in self._messages(step, "", schema))
        return self.budget.context_allowance(STEP_OUTPUT_TOKENS[step], overhead)

    def _call(self, step: str, payload: str, schema: type[Model]) -> Model:
        messages = self._messages(step, payload, schema)
        for attempt in range(STRUCTURED_RETRIES + 1):
            max_tokens = self.budget.plan_call(
                estimate_messages(m.content for m in messages), STEP_OUTPUT_TOKENS[step]
            )
            completion = self.client.complete(messages, max_tokens=max_tokens)
            self.budget.record(completion.input_tokens, completion.output_tokens)
            self.usage.calls += 1
            self.usage.input_tokens += completion.input_tokens
            self.usage.output_tokens += completion.output_tokens
            try:
                return schema.model_validate(parse_json_object(completion.content))
            except (ValueError, ValidationError) as exc:
                if attempt == STRUCTURED_RETRIES:
                    break
                retry = (
                    f"{messages[1].content}\n\nYour previous reply was rejected: "
                    f"{_rejection_reason(exc)}. Reply again with only the JSON object."
                )
                messages = [messages[0], ChatMessage("user", retry)]
        raise StepFailedError(step)

    def _attempt(self, step: str, payload: str, schema: type[Model]) -> Model | None:
        """One guarded call: returns ``None`` (and records why) when it cannot be used."""
        status = self.usage.stage_status
        if self.aborted:
            status.setdefault(step, "skipped")
            return None
        try:
            output = self._call(step, payload, schema)
        except StepFailedError:
            self.usage.fallbacks += 1
            status[step] = "fallback"
            return None
        except BudgetExhaustedError as exc:
            self.usage.fallbacks += 1
            if exc.scope == "audit":
                self.usage.budget_exhausted = True
                self.aborted = True
                status[step] = "budget_exhausted"
                self.result.warnings.append(
                    "The LLM token budget was exhausted; remaining AI steps used deterministic "
                    "results."
                )
            else:
                status[step] = "skipped_call_too_large"
            return None
        except LLMError as exc:
            self.usage.fallbacks += 1
            self.aborted = True
            status[step] = "unavailable"
            self.result.warnings.append(f"AI analysis stopped: {exc.message}")
            return None
        status.setdefault(step, "ok")
        return output

    # ------------------------------------------------------------------ step 1

    def _unit_ids(self) -> list[str]:
        units: list[str] = []
        for group in self.groups[:MAX_LLM_GROUPS]:
            for finding in group.findings[:FINDINGS_PER_GROUP]:
                unit = self.retriever.unit_id(
                    finding.location.file_path, finding.location.line_start
                )
                for candidate in [unit, *finding.execution_path]:
                    if candidate and candidate in self.symbols and candidate not in units:
                        units.append(candidate)
        return units[:MAX_UNITS]

    def _static_unit_summary(self, unit: str) -> str:
        module, symbol = self.symbols[unit]
        rules = sorted(
            {
                f.rule_id
                for f in self.findings
                if f.location.file_path == module.file_path
                and symbol.line_start <= f.location.line_start <= symbol.line_end
            }
        )
        listed = f"; findings: {', '.join(rules)}" if rules else ""
        return (
            f"{symbol.kind} {symbol.qualname} in {module.file_path} "
            f"(lines {symbol.line_start}-{symbol.line_end}){listed}"
        )

    def _unit_payload(self, unit: str) -> str | None:
        module, symbol = self.symbols[unit]
        findings = [
            f
            for f in self.findings
            if f.location.file_path == module.file_path
            and symbol.line_start <= f.location.line_start <= symbol.line_end
        ]
        focus = [f.location.line_start for f in findings] or [symbol.line_start]
        block = self.retriever.code_block(module.file_path, focus)
        if block is None:
            return None
        code, redactions = block
        self.context_redactions += redactions
        listed = "\n".join(
            f"  - {f.rule_id} line {f.location.line_start}: {f.title}" for f in findings[:6]
        )
        return f"UNIT {unit}\nfindings:\n{listed or '  (none; on an execution path)'}\n{code}"

    def _batches(
        self, payloads: list[tuple[str, str]], cap: int, size: int
    ) -> Iterator[list[tuple[str, str]]]:
        batch: list[tuple[str, str]] = []
        tokens = 0
        for unit, payload in payloads:
            cost = len(payload) // 4 + 1
            if batch and (len(batch) >= size or tokens + cost > cap):
                yield batch
                batch, tokens = [], 0
            if cost <= cap:
                batch.append((unit, payload))
                tokens += cost
        if batch:
            yield batch

    def step_units(self) -> dict[str, str]:
        summaries: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        keys: dict[str, str] = {}
        for unit in self._unit_ids():
            module, _ = self.symbols[unit]
            sha = self.file_sha.get(module.file_path, "")
            keys[unit] = SummaryCache.key(self.client.model, self.version, sha, unit)
            cached = self.cache.get(keys[unit])
            if cached is not None:
                self.usage.cache_hits += 1
                summaries[unit] = f"{cached.summary} {cached.performance_notes}".strip()
            elif payload := self._unit_payload(unit):
                pending.append((unit, payload))
        cap = self.context_allowance("unit_summaries", UnitSummaries)
        for batch in self._batches(pending, cap, UNITS_PER_CALL):
            allowed = {unit for unit, _ in batch}
            output = self._attempt(
                "unit_summaries", "\n\n".join(p for _, p in batch), UnitSummaries
            )
            for item in output.summaries if output else []:
                if item.unit_id not in allowed:
                    self.guard.stats.rejected_citations += 1
                    continue
                item.summary = self.guard.text(item.summary)
                item.performance_notes = self.guard.text(item.performance_notes)
                if item.summary:
                    self.cache.put(keys[item.unit_id], item)
                    summaries[item.unit_id] = f"{item.summary} {item.performance_notes}".strip()
        for unit in keys:
            summaries.setdefault(unit, self._static_unit_summary(unit))
        return summaries

    # ------------------------------------------------------------------ step 2

    def step_modules(self, summaries: dict[str, str]) -> dict[str, str]:
        modules: list[str] = []
        for unit in summaries:
            name = unit.split(":", 1)[0]
            if name not in modules:
                modules.append(name)
        by_name = {m.module_name: m for m in self.index.modules}
        static = {
            name: self.retriever.neighborhood(by_name[name])
            for name in modules[:MAX_MODULES]
            if name in by_name
        }
        if not static:
            return {}
        payload = "\n\n".join(
            static[name]
            + "".join(
                f"\n  unit {u}: {s}" for u, s in summaries.items() if u.startswith(f"{name}:")
            )
            for name in static
        )
        output = self._attempt("dependency_context", payload, DependencyContext)
        context = dict(static)
        for item in output.modules if output else []:
            if item.module not in static:
                self.guard.stats.rejected_citations += 1
                continue
            text = self.guard.text(f"{item.role} {item.concerns}".strip())
            if text:
                context[item.module] = f"{static[item.module]}\n  role: {text}"
        return context

    # ------------------------------------------------------------------ step 3

    def _unit_location(self, unit: str) -> CodeLocation:
        module, symbol = self.symbols[unit]
        return CodeLocation(
            file_path=module.file_path,
            line_start=symbol.line_start,
            line_end=symbol.line_end,
            symbol=symbol.qualname,
        )

    def step_hot_paths(self, summaries: dict[str, str], modules: dict[str, str]) -> list[str]:
        paths: list[tuple[list[str], Finding]] = []
        for finding in self.findings:
            path = [p for p in finding.execution_path if p in self.symbols]
            if path and all(path != existing for existing, _ in paths):
                paths.append((path, finding))
            if len(paths) >= MAX_PATHS:
                break
        static_notes = [
            f"{' -> '.join(path)} ({finding.code_role.value if finding.code_role else 'UNKNOWN'};"
            f" {finding.rule_id} at {finding.location.file_path}:{finding.location.line_start})"
            for path, finding in paths
        ]
        if not paths:
            return []
        involved = {u for path, _ in paths for u in path}
        payload = "\n".join(f"PATH {i + 1}: {note}" for i, note in enumerate(static_notes))
        payload += "\n\nUNIT SUMMARIES\n" + "\n".join(
            f"- {u}: {summaries.get(u, self._static_unit_summary(u))}" for u in sorted(involved)
        )
        payload += "\n\nMODULES\n" + "\n".join(modules.values())
        output = self._attempt("hot_paths", payload, HotPaths)
        notes = list(static_notes)
        for item in (output.paths if output else [])[:MAX_HOT_PATH_INSIGHTS]:
            units = self.guard.units(item.units)
            rationale = self.guard.text(item.rationale)
            if not units or len(units) != len(item.units) or not rationale:
                self.guard.stats.rejected_items += 1
                continue
            citations = self.guard.locations(item.citations) or [
                self._unit_location(u) for u in units
            ]
            notes.append(f"model hypothesis: {' -> '.join(units)}: {rationale}")
            self.result.insights.append(
                ArchitectureInsight(
                    id="ai-path-"
                    + hashlib.sha256("|".join([self.client.model, *units]).encode()).hexdigest()[
                        :20
                    ],
                    title=f"Likely hot path: {units[0]} -> {units[-1]}"
                    if len(units) > 1
                    else f"Likely hot path: {units[0]}",
                    description=rationale,
                    modules=list(dict.fromkeys(u.split(":", 1)[0] for u in units)),
                    citations=citations,
                    provenance=Provenance.AI_HYPOTHESIS,
                    generated_by=self.client.model,
                )
            )
        return notes

    # ------------------------------------------------------------------ step 4

    def _group_line(self, group: FindingGroup) -> str:
        top = group.top
        role = top.code_role.value if top.code_role else "UNKNOWN"
        return (
            f"- {group.rule_id} ({group.advice.title}): {len(group.findings)} finding(s) in "
            f"{group.file_path} [{', '.join(group.symbols[:4])}]; role {role}; first at line "
            f"{top.location.line_start}"
        )

    def step_correlation(self, modules: dict[str, str], hot_paths: list[str]) -> None:
        if len(self.groups) < 2:
            return
        payload = "FINDING GROUPS\n" + "\n".join(
            self._group_line(g) for g in self.groups[:MAX_GROUP_LINES]
        )
        payload += "\n\nMODULES\n" + "\n".join(modules.values())
        payload += "\n\nHOT PATHS\n" + "\n".join(hot_paths)
        output = self._attempt("correlation", payload, Correlations)
        for draft in (output.insights if output else [])[:MAX_CORRELATION_INSIGHTS]:
            insight = self.guard.insight(draft, self.client.model)
            if insight is not None:
                self.result.insights.append(insight)

    # ------------------------------------------------------------------ step 5

    def _group_payload(
        self,
        number: int,
        group: FindingGroup,
        summaries: dict[str, str],
        modules: dict[str, str],
        cap: int,
    ) -> str:
        findings = group.findings[:FINDINGS_PER_GROUP]
        context = self.retriever.for_findings(findings, cap)
        self.context_redactions += context.redactions
        lines = [
            f"GROUP {number}: {group.rule_id} — {group.advice.title}",
            f"role: {group.top.code_role.value if group.top.code_role else 'UNKNOWN'}",
            "findings:",
            *(
                f"  - id {f.id} at {f.location.file_path}:{f.location.line_start}-"
                f"{f.location.line_end} ({f.location.symbol or 'module level'}): "
                f"{first_sentence(f.description)}"
                for f in findings
            ),
        ]
        module_names: list[str] = []
        for unit in context.units:
            if unit in summaries:
                lines.append(f"summary {unit}: {summaries[unit]}")
            name = unit.split(":", 1)[0]
            if name in modules and name not in module_names:
                module_names.append(name)
                lines.append(f"module context: {modules[name]}")
        lines.append(context.text or "(source not available)")
        return "\n".join(lines)

    def step_recommendations(
        self, summaries: dict[str, str], modules: dict[str, str], hot_paths: list[str]
    ) -> None:
        top = self.groups[:MAX_LLM_GROUPS]
        allowance = self.context_allowance("recommendations", RecommendationDrafts)
        for start in range(0, len(top), GROUPS_PER_CALL):
            batch = top[start : start + GROUPS_PER_CALL]
            cap = max(0, allowance // max(1, len(batch)) - 200)
            payload = "\n\n".join(
                self._group_payload(start + i + 1, g, summaries, modules, cap)
                for i, g in enumerate(batch)
            )
            payload += "\n\nHOT PATH NOTES\n" + "\n".join(hot_paths[:MAX_PATHS])
            output = self._attempt("recommendations", payload, RecommendationDrafts)
            for draft in output.recommendations if output else []:
                recommendation = self.guard.recommendation(draft, self.client.model)
                if recommendation is not None:
                    self.result.recommendations.append(recommendation)

    # ------------------------------------------------------------------ run

    def run(self) -> PipelineResult:
        self._report(0.0, "AI step 1/5: summarizing relevant functions")
        summaries = self.step_units()
        self._report(0.2, "AI step 2/5: dependency context")
        modules = self.step_modules(summaries)
        self._report(0.4, "AI step 3/5: likely hot paths")
        hot_paths = self.step_hot_paths(summaries, modules)
        self._report(0.6, "AI step 4/5: cross-module correlation")
        self.step_correlation(modules, hot_paths)
        self._report(0.8, "AI step 5/5: recommendations")
        self.step_recommendations(summaries, modules, hot_paths)
        usage = self.usage
        stats = self.guard.stats
        usage.rejected_citations = stats.rejected_citations
        usage.rejected_claims = stats.rejected_claims
        usage.rejected_items = stats.rejected_items
        usage.redactions = self.client.redactions + self.context_redactions
        input_cost, output_cost = self.costs
        usage.estimated_cost_usd = round(
            usage.input_tokens / 1000 * input_cost + usage.output_tokens / 1000 * output_cost, 6
        )
        self._report(1.0, f"AI analysis: {len(self.result.recommendations)} recommendations")
        return self.result
