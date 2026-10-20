"""API response models shared across routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.models.enums import JobStatus


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_type: str
    status: JobStatus
    stage: str | None
    progress: float
    message: str | None
    error: str | None
    error_details: dict[str, Any] | None
    result: dict[str, Any] | None
    attempts: int
    subject_type: str | None
    subject_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobAccepted(BaseModel):
    """Returned (HTTP 202) by endpoints that start long-running work."""

    job_id: str
    status: JobStatus
    subject_type: str | None = None
    subject_id: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    llm_configured: bool
    llm_model: str | None
    sandbox_backend: str
    job_types: list[str]
