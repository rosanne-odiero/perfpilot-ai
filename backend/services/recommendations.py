"""Persistence and queries for advisor output: recommendations, insights and LLM usage."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.ai.analysis import AIAnalysisResult
from backend.models.audit_schemas import ArchitectureInsight, LLMUsage, Recommendation
from backend.models.enums import Provenance
from backend.models.orm.audit import AuditRun, RecommendationRecord
from backend.services.audits import get_audit

_RECOMMENDATION_NAMESPACE = uuid.UUID("2b0f7c9e-5d1a-4e8b-9c3f-7a6d5e4b3c21")


def recommendation_record_id(audit_id: str, recommendation_id: str) -> str:
    return str(uuid.uuid5(_RECOMMENDATION_NAMESPACE, f"{audit_id}:{recommendation_id}"))


def store_ai_result(session: Session, audit: AuditRun, result: AIAnalysisResult) -> None:
    """Replace the audit's recommendations, insights and LLM usage with ``result``."""
    for record in session.scalars(
        select(RecommendationRecord).where(RecommendationRecord.audit_id == audit.id)
    ):
        session.delete(record)
    session.flush()
    session.add_all(
        RecommendationRecord(
            id=recommendation_record_id(audit.id, rec.id),
            audit_id=audit.id,
            priority_rank=rec.priority_rank,
            provenance=rec.provenance.value,
            generated_by=rec.generated_by[:128],
            data=rec.model_dump(mode="json"),
        )
        for rec in result.recommendations
    )
    audit.insights = [insight.model_dump(mode="json") for insight in result.insights]
    audit.llm_usage = result.usage.model_dump(mode="json")
    by_provenance: dict[str, int] = {}
    for rec in result.recommendations:
        by_provenance[rec.provenance.value] = by_provenance.get(rec.provenance.value, 0) + 1
    audit.summary = {
        **(audit.summary or {}),
        "advisor": {
            "mode": result.mode,
            "recommendations": len(result.recommendations),
            "recommendations_by_provenance": dict(sorted(by_provenance.items())),
            "insights": len(result.insights),
            "suggested_diffs": sum(1 for r in result.recommendations if r.suggested_diff),
        },
    }


def list_recommendations(
    session: Session, audit_id: str, provenance: Provenance | None = None
) -> list[Recommendation]:
    get_audit(session, audit_id)
    stmt = select(RecommendationRecord).where(RecommendationRecord.audit_id == audit_id)
    if provenance is not None:
        stmt = stmt.where(RecommendationRecord.provenance == provenance.value)
    stmt = stmt.order_by(RecommendationRecord.priority_rank, RecommendationRecord.id)
    return [Recommendation.model_validate(r.data) for r in session.scalars(stmt)]


def list_insights(session: Session, audit_id: str) -> list[ArchitectureInsight]:
    audit = get_audit(session, audit_id)
    return [ArchitectureInsight.model_validate(raw) for raw in audit.insights or []]


def get_llm_usage(session: Session, audit_id: str) -> LLMUsage:
    audit = get_audit(session, audit_id)
    return LLMUsage.model_validate(audit.llm_usage or {})
