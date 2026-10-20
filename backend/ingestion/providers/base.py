"""The provider contract: turn a :class:`RepositoryRef` into a local working copy."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backend.core.config import Settings
from backend.models.audit_schemas import AcquiredSource, RepositoryRef
from backend.models.enums import RepositorySourceType


class RepositoryProvider(Protocol):
    source_type: RepositorySourceType

    def acquire(
        self, ref: RepositoryRef, workspace_dir: Path, settings: Settings
    ) -> AcquiredSource:
        """Populate the empty ``workspace_dir`` and describe what was acquired.

        Implementations must keep every written path inside ``workspace_dir``, never execute
        repository content, enforce the ingestion limits in ``settings``, and report entries
        they filtered out in ``AcquiredSource.skipped``.
        """
        ...
