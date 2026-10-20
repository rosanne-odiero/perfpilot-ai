"""Ingestion limits. Hitting a limit stops the walk cleanly and is reported, never a crash."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.config import Settings


@dataclass(frozen=True)
class IngestLimits:
    max_repo_files: int
    max_repo_bytes: int
    max_file_bytes: int
    max_python_files: int

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestLimits:
        return cls(
            max_repo_files=settings.max_repo_files,
            max_repo_bytes=settings.max_repo_bytes,
            max_file_bytes=settings.max_file_bytes,
            max_python_files=settings.max_python_files,
        )


@dataclass
class LimitTracker:
    """Counts files/bytes while walking and records which limits were reached."""

    limits: IngestLimits
    files_seen: int = 0
    bytes_seen: int = 0
    kept_bytes: int = 0
    python_kept: int = 0
    stopped: bool = False
    hit: list[str] = field(default_factory=list)

    def _record(self, name: str, value: int, effect: str) -> None:
        message = f"{name} ({value}) reached: {effect}"
        if not any(h.startswith(f"{name} ") for h in self.hit):
            self.hit.append(message)

    def admit(self, size: int, *, kept: bool) -> bool:
        """Account for one file. Returns False (and stops the walk) when a limit is reached."""
        if self.stopped:
            return False
        if self.files_seen >= self.limits.max_repo_files:
            self._record(
                "max_repo_files", self.limits.max_repo_files, "stopped early; inventory is partial"
            )
            self.stopped = True
            return False
        if kept and self.kept_bytes + size > self.limits.max_repo_bytes:
            self._record(
                "max_repo_bytes", self.limits.max_repo_bytes, "stopped early; inventory is partial"
            )
            self.stopped = True
            return False
        self.files_seen += 1
        self.bytes_seen += size
        if kept:
            self.kept_bytes += size
        return True

    def admit_python(self) -> bool:
        """Account for one analyzable Python file; extra files are skipped, the walk continues."""
        if self.python_kept >= self.limits.max_python_files:
            self._record(
                "max_python_files",
                self.limits.max_python_files,
                "further Python files were skipped",
            )
            return False
        self.python_kept += 1
        return True
