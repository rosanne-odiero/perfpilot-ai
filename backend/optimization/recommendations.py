"""Deterministic recommendations: static findings correlated with a profile's hotspots.

Numbers only ever come from the referenced ``ProfileRun`` and are labeled ``MEASURED`` inside
``profiler_evidence``. The root cause comes from the static rule (``STATIC``). The expected
effect and ``estimated_impact`` are qualitative hypotheses (``AI_HYPOTHESIS``: heuristic,
unverified) - only the benchmark measures an effect.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.models.audit_schemas import Finding
from backend.models.enums import Confidence, Provenance
from backend.models.workloads import HotspotLocation, WorkloadSpec
from backend.profiling.hotspots import HotspotRecord

COMPLEXITY_RULES = {"PP-LOOP-001", "PP-LOOP-002", "PP-COLL-001"}
RELATED_HOTSPOTS = 3
CPROFILE_NOTE = (
    "cProfile instruments every call, so absolute times are inflated; percentages show where "
    "time goes, not how fast the code runs unprofiled."
)


class RuleTemplate(BaseModel):
    title: str
    root_cause: str
    proposed: str
    expected_effect: str
    memory: str
    risks: list[str]
    transform: str | None


TEMPLATES: dict[str, RuleTemplate] = {
    "PP-LOOP-001": RuleTemplate(
        title="Test membership against a set built once instead of a list",
        root_cause=(
            "`x in some_list` scans the list element by element. Inside a loop this makes the "
            "function O(len(loop) x len(list))."
        ),
        proposed=(
            "Build `set(the_list)` once before the loop and test membership against the set "
            "(O(1) average per lookup)."
        ),
        expected_effect=(
            "Hypothesis: lookups drop from linear to constant time, so the loop should scale "
            "linearly; the gain grows with the list length."
        ),
        memory="One extra set holding the list's elements (hash table overhead per element).",
        risks=[
            "Elements must be hashable; set() raises TypeError for unhashable items.",
            "Set membership relies on __hash__ agreeing with __eq__.",
            "The set is built even when the loop runs zero times.",
        ],
        transform="list_membership_to_set",
    ),
    "PP-COLL-001": RuleTemplate(
        title="Count elements once with collections.Counter",
        root_cause=(
            "`items.count(x)` rescans the whole list on every call. Calling it for every "
            "element makes the function O(n^2)."
        ),
        proposed=(
            "Count every element in one pass with `collections.Counter(items)` before the loop "
            "and read the count from it."
        ),
        expected_effect=(
            "Hypothesis: the quadratic rescans disappear (O(n) total), which should dominate "
            "for inputs with thousands of items."
        ),
        memory="One dict entry per distinct element.",
        risks=[
            "Items must be hashable; Counter raises TypeError where list.count() worked.",
            "Counter matches by hash + ==; list.count() by == only.",
        ],
        transform="list_count_to_counter",
    ),
    "PP-LOOP-002": RuleTemplate(
        title="Replace the nested-loop join with a dict index",
        root_cause=(
            "For every outer item the inner loop scans the whole inner collection to find "
            "matching keys: O(outer x inner) comparisons."
        ),
        proposed=(
            "Group the inner collection by its join key once (dict of lists, original order "
            "kept) and visit only the matching items per outer item: O(outer + inner)."
        ),
        expected_effect=(
            "Hypothesis: the number of comparisons drops from the product to the sum of the "
            "sizes; matches are processed in the same order, so results stay identical."
        ),
        memory="The index holds a reference to every inner item plus one list per key.",
        risks=[
            "Join keys must be hashable with __hash__ consistent with __eq__.",
            "The inner collection is read even if the outer loop is empty, and the outer key is "
            "read even if the inner collection is empty (malformed records can fail earlier).",
        ],
        transform="nested_loop_join_to_index",
    ),
    "PP-STR-001": RuleTemplate(
        title='Build the string with a list and "".join',
        root_cause=(
            "`s += piece` in a loop can copy the growing string on each iteration (O(n^2) "
            "bytes copied) unless CPython can resize it in place."
        ),
        proposed='Append the pieces to a list and call `"".join(parts)` once after the loop.',
        expected_effect=(
            "Hypothesis: uncertain. CPython often resizes a uniquely referenced string in place, "
            "so `+=` may already be linear; the change can be neutral or slower."
        ),
        memory="All pieces stay alive until the join, so peak memory can rise.",
        risks=["A non-str piece raises TypeError at the join instead of at the `+=`."],
        transform="string_concat_to_join",
    ),
    "PP-REGEX-001": RuleTemplate(
        title="Compile the constant regex once at module level",
        root_cause=(
            "A constant pattern string is passed to `re.<function>` on every iteration, so each "
            "call resolves it through re's internal cache."
        ),
        proposed=(
            "Compile the pattern once at module level and call the compiled pattern's method "
            "inside the loop."
        ),
        expected_effect=(
            "Hypothesis: small. `re` already caches compiled patterns, so only the per-call "
            "cache lookup and dispatch are saved; the benefit may not be measurable."
        ),
        memory="One compiled pattern object kept for the module's lifetime.",
        risks=["An invalid pattern now fails at import time instead of at the first call."],
        transform="regex_compile_hoist",
    ),
}


class RecommendationDraft(BaseModel):
    rule_id: str
    finding_id: str
    title: str
    target_file: str
    target_file_sha256: str | None
    target_function: str | None
    source_lines: dict[str, int]
    observed_bottleneck: str
    profiler_evidence: dict[str, Any]
    suspected_root_cause: str
    proposed_optimization: str
    expected_effect: str
    correctness_risks: list[str]
    memory_tradeoffs: str
    confidence: Confidence
    validation_plan: list[str]
    estimated_impact: dict[str, Any]
    provenance: dict[str, str]
    generated_by: str = "deterministic"
    transform: str | None = None
    measured_share: float = Field(default=0.0, description="cumulative % used for ranking")


def match_hotspot(finding: Finding, hotspots: list[HotspotRecord]) -> HotspotRecord | None:
    """The workspace hotspot of the finding's function (same file, same function name)."""
    symbol = (finding.location.symbol or "").rsplit(".", 1)[-1]
    matches = [
        h
        for h in hotspots
        if h.is_user_code
        and h.file_path == finding.location.file_path
        and h.function_name == symbol
    ]
    return max(matches, key=lambda h: h.cumulative_time_ms) if matches else None


def _hotspot_dict(record: HotspotRecord) -> dict[str, Any]:
    return {
        "function_name": record.function_name,
        "file_path": record.file_path,
        "line_number": record.line_number,
        "location_kind": record.location_kind.value,
        "internal_time_ms": round(record.internal_time_ms, 3),
        "cumulative_time_ms": round(record.cumulative_time_ms, 3),
        "percentage_of_profiled_time": round(record.percentage_of_profiled_time, 2),
        "cumulative_percentage": round(record.cumulative_percentage, 2),
        "total_calls": record.total_calls,
        "primitive_calls": record.primitive_calls,
    }


def profiler_evidence(
    profile: dict[str, Any], match: HotspotRecord | None, hotspots: list[HotspotRecord]
) -> dict[str, Any]:
    related = sorted(
        (h for h in hotspots if not h.is_user_code and h.location_kind != HotspotLocation.HARNESS),
        key=lambda h: h.internal_time_ms,
        reverse=True,
    )[:RELATED_HOTSPOTS]
    return {
        "provenance": Provenance.MEASURED.value,
        "profile_id": profile["id"],
        "profile_status": profile["status"],
        "size": profile["size"],
        "input_sha256": profile["input_sha256"],
        "total_profiled_time_ms": profile["total_profiled_time_ms"],
        "wall_time_under_profiler_ms": profile["wall_time_ms"],
        "target_function": _hotspot_dict(match) if match else None,
        "note": (
            CPROFILE_NOTE
            if match
            else "the flagged function did not appear in the profile; it may not run for this "
            "input, so the finding stays a static hypothesis. " + CPROFILE_NOTE
        ),
        "largest_non_workspace_functions": [_hotspot_dict(h) for h in related],
        "largest_non_workspace_functions_note": (
            "largest exclusive-time functions outside the workspace in the same profile; not "
            "attributed to a caller"
        ),
    }


def _impact(rule_id: str, share: float, measured: bool) -> dict[str, Any]:
    if not measured:
        level, why = "unknown", "the flagged code did not show up in the profile"
    elif rule_id not in COMPLEXITY_RULES:
        level, why = "uncertain", "the change does not alter algorithmic complexity"
    elif share >= 50:
        level, why = "high", "complexity change in the function that dominates the profile"
    elif share >= 10:
        level, why = "moderate", "complexity change in a function with a notable profile share"
    else:
        level, why = "low", "the function holds a small share of the profiled time"
    return {
        "level": level,
        "rationale": why,
        "provenance": Provenance.AI_HYPOTHESIS.value,
        "note": "qualitative heuristic; only the A/B benchmark measures the effect",
    }


def _confidence(finding: Finding, share: float, measured: bool) -> Confidence:
    if not measured or share < 5:
        return Confidence.LOW
    if finding.confidence == Confidence.HIGH or (
        share >= 50 and finding.rule_id in COMPLEXITY_RULES
    ):
        return Confidence.HIGH
    return finding.confidence


def validation_plan(spec: WorkloadSpec) -> list[str]:
    config = spec.validation
    sizes = config.sizes or list(dict.fromkeys([next(iter(spec.sizes)), spec.default_size]))
    seeds = config.seeds or [spec.seed, spec.seed + 1, spec.seed + 2]
    contract = spec.expected_output
    tolerance = (
        f" (rel_tol={contract.rel_tol}, abs_tol={contract.abs_tol})"
        if contract.comparison == "float_tolerance"
        else ""
    )
    plan = [
        "Apply the approved diff to an isolated copy of the workload; verify the original tree "
        "hash is unchanged afterwards.",
        f"Run the workload's tests {config.tests} with pytest in a separate process against the "
        "original and the patched copy; at least "
        f"{config.min_tests} must pass on both.",
        f"Compare original vs candidate outputs for sizes {sizes} x seeds {seeds} using "
        f"{contract.comparison} comparison{tolerance}.",
    ]
    if config.edge_cases:
        plan.append(f"Compare outputs on {len(config.edge_cases)} contract edge case(s).")
    if config.error_cases:
        plan.append(f"Check exception parity on {len(config.error_cases)} error input(s).")
    if not config.allow_input_mutation:
        plan.append("Fail if the candidate mutates an input the original leaves untouched.")
    plan.append("Benchmark only after every required check passed.")
    return plan


def build_recommendation(
    finding: Finding,
    *,
    spec: WorkloadSpec,
    profile: dict[str, Any],
    hotspots: list[HotspotRecord],
    file_sha256: str | None,
) -> RecommendationDraft:
    match = match_hotspot(finding, hotspots)
    share = match.cumulative_percentage if match else 0.0
    template = TEMPLATES.get(finding.rule_id)
    location = finding.location
    where = f"{location.file_path}:{location.line_start}"
    if match:
        observed = (
            f"`{match.function_name}` ({match.file_path}:{match.line_number}) accounts for "
            f"{match.cumulative_percentage:.1f}% of profiled time (cumulative "
            f"{match.cumulative_time_ms:.2f} ms, exclusive {match.internal_time_ms:.2f} ms, "
            f"{match.total_calls} call(s)) in profile {profile['id']} at size "
            f"{profile['size']} [MEASURED]. Rule {finding.rule_id} flags "
            f"{finding.title.lower()} at {where} [STATIC]."
        )
    else:
        observed = (
            f"Rule {finding.rule_id} flags {finding.title.lower()} at {where} [STATIC]; the "
            f"function was not observed in profile {profile['id']}."
        )
    risks = list(template.risks) if template else [finding.tradeoffs]
    return RecommendationDraft(
        rule_id=finding.rule_id,
        finding_id=finding.id,
        title=template.title if template else finding.title,
        target_file=location.file_path,
        target_file_sha256=file_sha256,
        target_function=location.symbol,
        source_lines={"start": location.line_start, "end": location.line_end},
        observed_bottleneck=observed,
        profiler_evidence=profiler_evidence(profile, match, hotspots),
        suspected_root_cause=template.root_cause if template else finding.description,
        proposed_optimization=template.proposed if template else finding.suggestion,
        expected_effect=template.expected_effect
        if template
        else ("Hypothesis: see the rule's suggestion; no deterministic template exists."),
        correctness_risks=risks,
        memory_tradeoffs=template.memory if template else finding.tradeoffs,
        confidence=_confidence(finding, share, match is not None),
        validation_plan=validation_plan(spec),
        estimated_impact=_impact(finding.rule_id, share, match is not None),
        provenance={
            "recommendation": Provenance.STATIC.value,
            "observed_bottleneck": Provenance.MEASURED.value if match else Provenance.STATIC.value,
            "profiler_evidence": Provenance.MEASURED.value,
            "suspected_root_cause": Provenance.STATIC.value,
            "expected_effect": Provenance.AI_HYPOTHESIS.value,
            "estimated_impact": Provenance.AI_HYPOTHESIS.value,
        },
        transform=template.transform if template else None,
        measured_share=share,
    )
