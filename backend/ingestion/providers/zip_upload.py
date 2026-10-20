"""ZIP archives previously uploaded to the artifact store."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from backend.core.config import Settings
from backend.core.exceptions import ValidationFailedError
from backend.infrastructure.artifact_store import ArtifactStore
from backend.ingestion.extraction import safe_extract_zip
from backend.models.audit_schemas import AcquiredSource, GitMetadata, RepositoryRef
from backend.models.database import session_scope
from backend.models.enums import RepositorySourceType

UPLOAD_ARTIFACT_KIND = "repository_upload"


class ZipUploadProvider:
    source_type = RepositorySourceType.ZIP_UPLOAD

    def acquire(
        self, ref: RepositoryRef, workspace_dir: Path, settings: Settings
    ) -> AcquiredSource:
        if not ref.upload_artifact_id:
            raise ValidationFailedError("upload_artifact_id is required")
        store = ArtifactStore(settings)
        with session_scope() as session:
            artifact = store.get(session, ref.upload_artifact_id)
            if artifact.kind != UPLOAD_ARTIFACT_KIND:
                raise ValidationFailedError("artifact is not a repository upload")
            archive = store.path_for(artifact)
            filename = artifact.filename
        result = safe_extract_zip(archive, workspace_dir, settings)
        # Uploaded archives are untrusted: git is never run on them (a bundled .git/config could
        # configure hooks or fsmonitor commands), so no git metadata is reported.
        return AcquiredSource(
            workspace_path=str(workspace_dir),
            source_type=self.source_type,
            display_name=ref.display_name or result.root_name or PurePosixPath(filename).stem,
            git=GitMetadata(),
            skipped=result.skipped,
            limits_hit=result.limits_hit,
        )
