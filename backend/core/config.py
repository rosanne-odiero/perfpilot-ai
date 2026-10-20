"""Application settings loaded from environment variables (prefix ``PERFPILOT_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PERFPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General -----------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    database_url: str | None = None
    api_base_url: str = "http://127.0.0.1:8000"

    # --- Ingestion limits ----------------------------------------------------------
    local_allowlist: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    max_upload_bytes: int = 100 * 1024 * 1024
    max_repo_files: int = 20_000
    max_repo_bytes: int = 500 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    max_python_files: int = 5_000
    max_archive_entries: int = 50_000
    max_compression_ratio: float = 200.0
    clone_timeout_seconds: int = 180
    github_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["github.com"]
    )

    # --- Job worker ---------------------------------------------------------------
    worker_poll_seconds: float = 1.0
    job_lease_seconds: int = 60
    job_default_timeout_seconds: int = 1800
    job_max_attempts: int = 2

    # --- LLM (optional; deterministic advisor is used when unset) ---------------------
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_max_tokens_per_audit: int = 60_000
    llm_max_tokens_per_call: int = 6_000
    llm_input_cost_per_1k: float = 0.0
    llm_output_cost_per_1k: float = 0.0

    # --- Runtime sandbox -------------------------------------------------------------
    sandbox_backend: str = "docker"
    sandbox_image: str = "perfpilot-sandbox:latest"
    sandbox_cpus: float = 2.0
    sandbox_memory_mb: int = 2048
    sandbox_pids_limit: int = 256
    sandbox_timeout_seconds: int = 600

    @field_validator("local_allowlist", mode="before")
    @classmethod
    def _split_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(p.strip()).expanduser() for p in value.split(",") if p.strip()]
        return value

    @field_validator("github_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def artifacts_dir(self) -> Path:
        return self.resolved_data_dir / "artifacts"

    @property
    def workspaces_dir(self) -> Path:
        return self.resolved_data_dir / "workspaces"

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.resolved_data_dir / 'perfpilot.sqlite3'}"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    def ensure_dirs(self) -> None:
        for path in (self.resolved_data_dir, self.artifacts_dir, self.workspaces_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
