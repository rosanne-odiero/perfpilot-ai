"""Advisor output for an audit, and what the optional LLM integration would do."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.ai.llm_client import endpoint_host
from backend.ai.prompt_loader import pipeline_version
from backend.api.deps import AppSettings, DbSession
from backend.models.audit_schemas import ArchitectureInsight, LLMUsage, Recommendation
from backend.models.enums import Provenance
from backend.services import recommendations as service

router = APIRouter(tags=["recommendations"])

DATA_SENT = (
    "Only when an audit is started with use_llm=true and llm_consent=true: source snippets of the "
    "functions around static findings (and on their execution paths), finding descriptions, and "
    "module import summaries — redacted for secrets before sending. Content of sensitive files "
    "(.env, keys, credentials) is never sent, and the repository is never sent wholesale."
)


class AIBudget(BaseModel):
    max_tokens_per_audit: int
    max_tokens_per_call: int
    input_cost_per_1k: float
    output_cost_per_1k: float


class AIStatus(BaseModel):
    configured: bool
    model: str | None
    base_url_host: str | None
    api_key_set: bool
    consent_required: bool = True
    default_advisor: str = "deterministic"
    data_sent: str = DATA_SENT
    budget: AIBudget
    prompt_version: str


@router.get("/audits/{audit_id}/recommendations", response_model=list[Recommendation])
def list_recommendations(
    audit_id: str, session: DbSession, provenance: Provenance | None = None
) -> list[Recommendation]:
    return service.list_recommendations(session, audit_id, provenance)


@router.get("/audits/{audit_id}/insights", response_model=list[ArchitectureInsight])
def list_insights(audit_id: str, session: DbSession) -> list[ArchitectureInsight]:
    return service.list_insights(session, audit_id)


@router.get("/audits/{audit_id}/llm-usage", response_model=LLMUsage)
def get_llm_usage(audit_id: str, session: DbSession) -> LLMUsage:
    return service.get_llm_usage(session, audit_id)


@router.get("/ai/status", response_model=AIStatus)
def ai_status(settings: AppSettings) -> AIStatus:
    return AIStatus(
        configured=settings.llm_configured,
        model=settings.llm_model if settings.llm_configured else None,
        base_url_host=endpoint_host(settings.llm_base_url),
        api_key_set=settings.llm_api_key is not None,
        budget=AIBudget(
            max_tokens_per_audit=settings.llm_max_tokens_per_audit,
            max_tokens_per_call=settings.llm_max_tokens_per_call,
            input_cost_per_1k=settings.llm_input_cost_per_1k,
            output_cost_per_1k=settings.llm_output_cost_per_1k,
        ),
        prompt_version=pipeline_version(),
    )
