"""Repository providers, one per :class:`RepositorySourceType`."""

from __future__ import annotations

from backend.ingestion.providers.base import RepositoryProvider
from backend.ingestion.providers.github_private import PrivateGitHubProvider
from backend.ingestion.providers.github_public import PublicGitHubProvider
from backend.ingestion.providers.local_dir import LocalDirectoryProvider
from backend.ingestion.providers.zip_upload import ZipUploadProvider
from backend.models.enums import RepositorySourceType

_PROVIDERS: dict[RepositorySourceType, RepositoryProvider] = {
    RepositorySourceType.GITHUB_PUBLIC: PublicGitHubProvider(),
    RepositorySourceType.GITHUB_PRIVATE: PrivateGitHubProvider(),
    RepositorySourceType.ZIP_UPLOAD: ZipUploadProvider(),
    RepositorySourceType.LOCAL_DIRECTORY: LocalDirectoryProvider(),
}


def get_provider(source_type: RepositorySourceType) -> RepositoryProvider:
    return _PROVIDERS[source_type]


__all__ = [
    "LocalDirectoryProvider",
    "PrivateGitHubProvider",
    "PublicGitHubProvider",
    "RepositoryProvider",
    "ZipUploadProvider",
    "get_provider",
]
