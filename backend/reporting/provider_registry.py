"""Report providers: optional enrichers for the measured report sections.

A provider is a function decorated with ``@report_provider("<name>")`` in a module placed in
``backend/reporting/providers/``. Modules there are imported automatically. Each provider receives
a read-only ``ProviderContext`` and returns a ``ProviderContribution`` (or ``None`` when it has
nothing for this audit). Providers are applied in name order and their lists are concatenated.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import Field
from sqlalchemy.orm import Session

from backend.models.audit_schemas import Finding
from backend.reporting.model import (
    BenchmarkResult,
    DiffEntry,
    ReportModel,
    RuntimeProfile,
    ValidationOutcome,
)


@dataclass(frozen=True)
class ProviderContext:
    audit_id: str
    snapshot_id: str
    repository_id: str
    commit_sha: str | None
    findings: list[Finding] = field(default_factory=list)


class ProviderContribution(ReportModel):
    runtime_profiles: list[RuntimeProfile] = Field(default_factory=list)
    validation_outcomes: list[ValidationOutcome] = Field(default_factory=list)
    benchmark_results: list[BenchmarkResult] = Field(default_factory=list)
    code_changes: list[DiffEntry] = Field(
        default_factory=list, description="Diffs of real patch candidates (section 10)"
    )
    limitations: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.runtime_profiles
            or self.code_changes
            or self.validation_outcomes
            or self.benchmark_results
            or self.limitations
        )


ReportProvider = Callable[[Session, ProviderContext], ProviderContribution | None]

_PROVIDERS: dict[str, ReportProvider] = {}
_discovered = False


def report_provider(name: str) -> Callable[[ReportProvider], ReportProvider]:
    def decorator(fn: ReportProvider) -> ReportProvider:
        register_provider(name, fn)
        return fn

    return decorator


def register_provider(name: str, fn: ReportProvider) -> None:
    if name in _PROVIDERS and _PROVIDERS[name] is not fn:
        raise RuntimeError(f"duplicate report provider {name!r}")
    _PROVIDERS[name] = fn


def unregister_provider(name: str) -> None:
    _PROVIDERS.pop(name, None)


def discover_providers() -> None:
    global _discovered
    if _discovered:
        return
    import backend.reporting.providers as providers_pkg

    for info in pkgutil.iter_modules(providers_pkg.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{providers_pkg.__name__}.{info.name}")
    _discovered = True


def registered_providers() -> list[tuple[str, ReportProvider]]:
    discover_providers()
    return sorted(_PROVIDERS.items())
