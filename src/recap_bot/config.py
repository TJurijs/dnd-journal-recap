from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_bot_token: str
    gemini_api_key: str
    gemini_model: str = "gemini-3-flash-preview"
    log_level: str = "INFO"
    data_dir: Path = Path("/data")
    max_vod_hours: int = 6
    default_style: str = "chapters"
    download_rate_limit: str = ""
    ffmpeg_bin: str = "ffmpeg"
    discord_guild_id: str = ""

    @field_validator("data_dir", mode="before")
    @classmethod
    def _parse_data_dir(cls, v):
        return Path(v)

    @property
    def guild_id_as_int(self) -> int | None:
        return int(self.discord_guild_id) if self.discord_guild_id else None


settings = Settings()


class ModelConfig:
    """Loads per-action model assignments from models.yaml."""

    def __init__(self, path: Path = Path("models.yaml")):
        self._models: dict[str, str] = {}
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self._models = data.get("models", {})
            except Exception:
                pass
        # Fallbacks for any missing keys
        default = settings.gemini_model
        self._models.setdefault("roster_build", default)
        self._models.setdefault("scratchpad_build", default)
        self._models.setdefault("transcribe", default)
        self._models.setdefault("summarize", default)
        self._models.setdefault("update_roster", default)
        self._models.setdefault("update_scratchpad", default)

    def get(self, action: str) -> str:
        return self._models.get(action, settings.gemini_model)


model_config = ModelConfig()
