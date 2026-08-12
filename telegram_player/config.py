"""Configuration and per-user paths for Telegram Player."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Config:
    api_id: int | None = None
    api_hash: str | None = None
    volume: int = 100
    speed: float = 1.0  # playback speed multiplier; persists across the playlist and sessions
    mpv_extra_args: list[str] = field(default_factory=list)
    check_updates: bool = True  # periodically check GitHub Releases for a newer build

    # Paths are resolved once per process from XDG env vars.
    @staticmethod
    def config_dir() -> Path:
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "telegram-player"

    @staticmethod
    def data_dir() -> Path:
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "telegram-player"

    @staticmethod
    def cache_dir() -> Path:
        return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "telegram-player"

    @property
    def session_path(self) -> Path:
        return self.data_dir() / "telegram.session"

    @property
    def playlist_path(self) -> Path:
        return self.config_dir() / "playlist.json"

    @property
    def playlists_path(self) -> Path:
        return self.config_dir() / "playlists.json"

    @property
    def mpv_config_dir(self) -> Path:
        return self.config_dir() / "mpv"

    @classmethod
    def load(cls) -> "Config":
        path = cls.config_dir() / "config.json"
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text("utf-8"))
            cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            if cfg.volume is not None:
                cfg.volume = max(0, min(100, int(cfg.volume)))
            if cfg.speed is not None:
                cfg.speed = max(0.1, min(10.0, float(cfg.speed)))
            return cfg
        except (ValueError, TypeError, OSError):
            return cls()

    def save(self) -> None:
        path = self.config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")