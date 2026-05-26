import logging
from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


RUNTIME_CONFIG_FILENAME = "runtime_config.yaml"
# Fields that can be overridden at runtime via /settings or by editing
# data/runtime_config.yaml directly. Anything not in this list is ignored
# even if present in the file.
RUNTIME_OVERRIDABLE = frozenset({
    "gemini_api_key",
    "gemini_model",
    "log_level",
    "max_vod_hours",
    "default_style",
    "download_rate_limit",
    "ffmpeg_bin",
    # Comma-separated guild id list. Overridable so the owner can add/remove
    # servers from a Discord DM via /settings without SSHing to edit .env.
    "discord_guild_id",
})


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
        """Back-compat: return the FIRST configured guild id, or None.

        New code should use `guild_ids` instead — `DISCORD_GUILD_ID` now
        accepts a comma-separated list to support multi-server deployments.
        """
        ids = self.guild_ids
        return ids[0] if ids else None

    @property
    def guild_ids(self) -> list[int]:
        """Parse DISCORD_GUILD_ID into a list of guild ids.

        Accepts:
          - empty string → no instant-sync guilds (commands sync globally only,
            which has up to a 1h propagation lag — fine for production but
            painful during development)
          - single id: "12345"
          - comma-separated: "12345, 67890, 11111"

        Each id gets channel commands synced instantly. DM-only commands
        (`/jobs`, `/check`, `/settings`) are always global regardless — they
        need to be global to appear in DM autocomplete.

        Multi-guild support keeps the fast-iteration workflow that single-guild
        gives us, while letting one bot instance serve several Discord servers.
        Adding a new server = append the id to the env var and restart.
        """
        raw = (self.discord_guild_id or "").strip()
        if not raw:
            return []
        out: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                logger.warning("Ignoring non-integer guild id in DISCORD_GUILD_ID: %r", part)
        return out


settings = Settings()


def runtime_config_path() -> Path:
    """Where `/settings set ...` writes runtime overrides."""
    return settings.data_dir / RUNTIME_CONFIG_FILENAME


def _apply_runtime_overrides() -> None:
    """Layer `runtime_config.yaml` on top of `.env`-derived settings.

    Anything in RUNTIME_OVERRIDABLE that's present in the file wins over
    the env-var value. Bad/missing values are ignored silently (env value
    stays). Called once at process startup; container restart picks up
    changes written by `/settings`.
    """
    path = runtime_config_path()
    if not path.exists():
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("Failed to read %s; runtime overrides ignored", path)
        return
    if not isinstance(data, dict):
        return

    applied: list[str] = []
    for key, value in data.items():
        if key not in RUNTIME_OVERRIDABLE or value is None:
            continue
        try:
            setattr(settings, key, value)
            applied.append(key)
        except Exception:
            logger.exception("Failed to apply runtime override %s", key)
    if applied:
        logger.info("Applied runtime overrides from %s: %s", path, applied)


_apply_runtime_overrides()


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
