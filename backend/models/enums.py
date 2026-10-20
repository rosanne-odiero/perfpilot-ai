"""Shared enumerations. Values are persisted, so never rename existing members."""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }


class Provenance(StrEnum):
    """Where a result came from. Rendered on every finding, recommendation and metric."""

    MEASURED = "MEASURED"
    STATIC = "STATIC"
    AI_HYPOTHESIS = "AI_HYPOTHESIS"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def weight(self) -> float:
        return {"CRITICAL": 1.0, "HIGH": 0.8, "MEDIUM": 0.55, "LOW": 0.3, "INFO": 0.1}[self.value]


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def weight(self) -> float:
        return {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}[self.value]


class FindingCategory(StrEnum):
    ALGORITHMIC = "ALGORITHMIC"
    REPEATED_PROCESSING = "REPEATED_PROCESSING"
    NESTED_LOOPS = "NESTED_LOOPS"
    COLLECTIONS = "COLLECTIONS"
    DATABASE = "DATABASE"
    ALLOCATIONS = "ALLOCATIONS"
    ASYNC_BLOCKING = "ASYNC_BLOCKING"
    NETWORK = "NETWORK"
    CACHING = "CACHING"
    STRINGS = "STRINGS"
    REGEX = "REGEX"
    ARCHITECTURE = "ARCHITECTURE"


class RepositorySourceType(StrEnum):
    GITHUB_PUBLIC = "GITHUB_PUBLIC"
    GITHUB_PRIVATE = "GITHUB_PRIVATE"
    ZIP_UPLOAD = "ZIP_UPLOAD"
    LOCAL_DIRECTORY = "LOCAL_DIRECTORY"


class SnapshotStatus(StrEnum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    READY = "READY"
    FAILED = "FAILED"


class AuditMode(StrEnum):
    STATIC = "STATIC"
    RUNTIME = "RUNTIME"


class AuditStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AuditStage(StrEnum):
    QUEUED = "QUEUED"
    INDEXING = "INDEXING"
    GRAPHING = "GRAPHING"
    STATIC_RULES = "STATIC_RULES"
    PRIORITIZING = "PRIORITIZING"
    AI_ANALYSIS = "AI_ANALYSIS"
    RUNTIME_DISCOVERY = "RUNTIME_DISCOVERY"
    AWAITING_RUNTIME_APPROVAL = "AWAITING_RUNTIME_APPROVAL"
    RUNTIME_PROFILING = "RUNTIME_PROFILING"
    CORRELATING = "CORRELATING"
    COMPLETED = "COMPLETED"


class ReportFormat(StrEnum):
    HTML = "html"
    MARKDOWN = "md"
    JSON = "json"
    CSV = "csv"
    PDF = "pdf"

    @property
    def media_type(self) -> str:
        return {
            "html": "text/html",
            "md": "text/markdown",
            "json": "application/json",
            "csv": "text/csv",
            "pdf": "application/pdf",
        }[self.value]


class EffortEstimate(StrEnum):
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
