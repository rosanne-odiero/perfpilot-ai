"""Local artifact store: bytes on disk under ``<data_dir>/artifacts``, metadata in the DB."""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.exceptions import NotFoundError, SecurityViolationError
from backend.models.database import new_id
from backend.models.orm.platform import Artifact

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip("._")
    return cleaned[:200] or "artifact"


class ArtifactStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.artifacts_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, kind: str, filename: str) -> tuple[Path, str]:
        day = datetime.now(UTC).strftime("%Y%m%d")
        rel = f"{safe_filename(kind)}/{day}/{new_id()}-{safe_filename(filename)}"
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise SecurityViolationError("artifact path escapes store")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, rel

    def save_bytes(
        self,
        session: Session,
        *,
        kind: str,
        filename: str,
        data: bytes,
        media_type: str = "application/octet-stream",
        owner_type: str | None = None,
        owner_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        path, rel = self._target(kind, filename)
        path.write_bytes(data)
        artifact = Artifact(
            kind=kind,
            relative_path=rel,
            filename=safe_filename(filename),
            media_type=media_type,
            size_bytes=len(data),
            sha256=sha256_bytes(data),
            owner_type=owner_type,
            owner_id=owner_id,
            meta=meta or {},
        )
        session.add(artifact)
        session.flush()
        return artifact

    def save_file(
        self,
        session: Session,
        *,
        kind: str,
        source: Path,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        owner_type: str | None = None,
        owner_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        path, rel = self._target(kind, filename or source.name)
        shutil.copyfile(source, path)
        artifact = Artifact(
            kind=kind,
            relative_path=rel,
            filename=safe_filename(filename or source.name),
            media_type=media_type,
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
            owner_type=owner_type,
            owner_id=owner_id,
            meta=meta or {},
        )
        session.add(artifact)
        session.flush()
        return artifact

    def path_for(self, artifact: Artifact) -> Path:
        path = (self.root / artifact.relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise SecurityViolationError("artifact path escapes store")
        if not path.exists():
            raise NotFoundError(f"artifact file missing: {artifact.id}")
        return path

    def read_bytes(self, artifact: Artifact) -> bytes:
        return self.path_for(artifact).read_bytes()

    def get(self, session: Session, artifact_id: str) -> Artifact:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        return artifact
