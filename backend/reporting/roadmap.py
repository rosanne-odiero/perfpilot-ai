"""Deterministic grouping of recommendations into an optimization roadmap (report section 14)."""

from __future__ import annotations

from backend.models.audit_schemas import Finding, Recommendation
from backend.models.enums import EffortEstimate, Severity
from backend.reporting.model import RoadmapItem, RoadmapPhase, RoadmapPhaseName, RoadmapSection

_QUICK_WIN_SEVERITIES = {Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM}

ROADMAP_RULES: list[str] = [
    "Strategic: every recommendation with LARGE effort.",
    "Quick wins: SMALL effort and at least one linked finding of MEDIUM severity or higher.",
    "Next: everything else (MEDIUM effort, or SMALL effort with only LOW/INFO or no linked "
    "findings).",
    "Within a phase, items are ordered by the advisor's priority rank (unranked last), then by "
    "highest linked severity, then by recommendation id.",
]

_PHASE_DESCRIPTIONS: dict[RoadmapPhaseName, str] = {
    "Quick wins": "Small, localized changes addressing meaningful findings. Validate and "
    "benchmark each before merging.",
    "Next": "Moderate changes or lower-severity items to schedule after the quick wins.",
    "Strategic": "Large or cross-cutting changes that need design work and a measured baseline "
    "first.",
}


def highest_severity(rec: Recommendation, findings: dict[str, Finding]) -> Severity | None:
    linked = [findings[fid].severity for fid in rec.finding_ids if fid in findings]
    return max(linked, key=lambda s: s.weight) if linked else None


def classify(rec: Recommendation, severity: Severity | None) -> tuple[RoadmapPhaseName, str]:
    if rec.effort == EffortEstimate.LARGE:
        return "Strategic", "LARGE effort"
    if rec.effort == EffortEstimate.SMALL and severity in _QUICK_WIN_SEVERITIES:
        return "Quick wins", f"SMALL effort, linked {severity} finding"
    if rec.effort == EffortEstimate.SMALL:
        return "Next", "SMALL effort, no linked finding of MEDIUM severity or higher"
    return "Next", f"{rec.effort} effort"


def build_roadmap(
    recommendations: list[Recommendation], findings: dict[str, Finding]
) -> RoadmapSection:
    buckets: dict[RoadmapPhaseName, list[RoadmapItem]] = {
        "Quick wins": [],
        "Next": [],
        "Strategic": [],
    }
    for rec in recommendations:
        severity = highest_severity(rec, findings)
        phase, reason = classify(rec, severity)
        buckets[phase].append(
            RoadmapItem(
                recommendation_id=rec.id,
                title=rec.title,
                effort=rec.effort,
                provenance=rec.provenance,
                priority_rank=rec.priority_rank,
                highest_severity=severity,
                reason=reason,
            )
        )
    for items in buckets.values():
        items.sort(
            key=lambda item: (
                item.priority_rank <= 0,
                item.priority_rank,
                -(item.highest_severity.weight if item.highest_severity else 0.0),
                item.recommendation_id,
            )
        )
    return RoadmapSection(
        phases=[
            RoadmapPhase(name=name, description=_PHASE_DESCRIPTIONS[name], items=items)
            for name, items in buckets.items()
        ],
        rules=list(ROADMAP_RULES),
    )
