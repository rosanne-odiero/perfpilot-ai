"""Private GitHub repositories (designed, not yet enabled). See ``docs/private-github.md``."""

from __future__ import annotations

from pathlib import Path

from backend.core.config import Settings
from backend.core.exceptions import NotConfiguredError
from backend.models.audit_schemas import AcquiredSource, RepositoryRef
from backend.models.enums import RepositorySourceType

NOT_CONFIGURED_MESSAGE = (
    "Private GitHub repositories require a GitHub App installation or an OAuth device-flow "
    "authorization, which is not configured in this deployment. Use a public GitHub URL, "
    "upload a ZIP, or import an allowlisted local directory instead."
)


class PrivateGitHubProvider:
    """Will fetch private repositories with short-lived GitHub App installation tokens.

    Tokens will be handed to git only through a ``GIT_ASKPASS`` helper or an
    ``http.extraHeader`` supplied via environment-scoped config, never embedded in URLs, argv,
    logs or the database. Until a GitHub App is configured, every call raises
    :class:`NotConfiguredError`.
    """

    source_type = RepositorySourceType.GITHUB_PRIVATE

    def acquire(
        self, ref: RepositoryRef, workspace_dir: Path, settings: Settings
    ) -> AcquiredSource:
        raise NotConfiguredError(NOT_CONFIGURED_MESSAGE, details={"docs": "docs/private-github.md"})
