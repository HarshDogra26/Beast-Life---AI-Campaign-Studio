"""Typed configuration. Every bound, timeout and credential enters here.
"""

from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .domain.enums import ProviderMode

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- mode --------------------------------------------------------------
    provider_mode: ProviderMode = ProviderMode.FIXTURE

    # --- Azure OpenAI: chat ------------------------------------------------
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_chat_deployment: str = "gpt-4.1"
    azure_openai_chat_api_version: str = "2024-10-21"

    # --- Azure OpenAI: images ----------------------------------------------
    azure_openai_image_deployment: str = "gpt-image-2.5-sunburst"
    azure_openai_image_model: str = "gpt-image-2.5-sunburst"
    azure_openai_image_api_version: str = "2025-04-01-preview"
    image_quality: str = "high"
    image_input_fidelity: str = "high"
    image_rpm: int = Field(default=5, ge=1, le=120)

    # --- Tavily ------------------------------------------------------------
    tavily_api_key: SecretStr = SecretStr("")
    tavily_base_url: str = "https://api.tavily.com"

    # --- MCP ---------------------------------------------------------------
    mcp_research_url: str = "http://127.0.0.1:8765/mcp"
    mcp_research_host: str = "127.0.0.1"
    mcp_research_port: int = 8765
    mcp_inprocess: bool = True

    # --- research bounds ---------------------------------------------------
    max_search_calls: int = Field(default=4, ge=1, le=20)
    max_fetch_calls: int = Field(default=6, ge=1, le=40)
    research_wall_clock_s: float = Field(default=120.0, gt=0, le=900)
    min_sources: int = Field(default=3, ge=1, le=20)

    # --- timeouts & retries ------------------------------------------------
    # Research turns accumulate search results and page text, so a late turn
    # carries a much larger prompt than the first. 60s was tight enough to
    # time out in practice.
    llm_timeout_s: float = Field(default=120.0, gt=0)
    image_timeout_s: float = Field(default=180.0, gt=0)
    video_timeout_s: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=3, ge=1, le=8)
    retry_base_delay_s: float = Field(default=1.0, gt=0)

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./var/studio.sqlite"
    artifact_dir: Path = Path("./var/artifacts")
    checkpoint_db: Path = Path("./var/checkpoints.sqlite")

    # --- uploads -----------------------------------------------------------
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    allowed_upload_types: str = "image/png,image/jpeg,image/webp"

    # --- video -------------------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    video_target_seconds: float = Field(default=8.0, ge=6.0, le=10.0)

    # --- review aids -------------------------------------------------------
    failure_inject: str = ""
    log_level: str = "INFO"
    log_json: bool = False
    cors_origins: str = "http://localhost:5173"

    # --- derived -----------------------------------------------------------
    @field_validator("artifact_dir", "checkpoint_db", mode="after")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_ROOT / v).resolve()

    @property
    def is_live(self) -> bool:
        return self.provider_mode is ProviderMode.LIVE

    @property
    def upload_types(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.allowed_upload_types.split(",") if t.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_database_url(self) -> str:
        """Make the sqlite path absolute so the DB does not follow the CWD."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            raw = self.database_url[len(prefix) :]
            path = Path(raw)
            if not path.is_absolute():
                path = (BACKEND_ROOT / path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path}"
        return self.database_url

    def failure_injections(self) -> dict[str, str]:
        """Parse ``FAILURE_INJECT`` into ``{stage: failure_kind}``."""
        out: dict[str, str] = {}
        for item in self.failure_inject.split(","):
            item = item.strip()
            if not item:
                continue
            stage, _, kind = item.partition(":")
            if stage and kind:
                out[stage.strip()] = kind.strip()
        return out

    def missing_live_credentials(self) -> list[str]:
        """Which secrets are absent. Checked at startup so LIVE fails loudly."""
        missing: list[str] = []
        if not self.azure_openai_endpoint:
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not self.azure_openai_api_key.get_secret_value():
            missing.append("AZURE_OPENAI_API_KEY")
        if not self.tavily_api_key.get_secret_value():
            missing.append("TAVILY_API_KEY")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
