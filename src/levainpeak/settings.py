"""User settings kept in a small local JSON file."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

SETTINGS_PATH = Path("data/settings.json")
MIN_RECORD_SECONDS = 10
MAX_RECORD_SECONDS = 120


@dataclass
class Settings:
    record_interval_seconds: int = 10

    def clamped(self) -> "Settings":
        seconds = min(MAX_RECORD_SECONDS, max(MIN_RECORD_SECONDS, int(self.record_interval_seconds)))
        return Settings(record_interval_seconds=seconds)


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Settings(record_interval_seconds=int(data["record_interval_seconds"])).clamped()
    except FileNotFoundError:
        return Settings()
    except (OSError, KeyError, TypeError, ValueError):
        logging.warning("Configurações inválidas em %s; usando o padrão", path)
        return Settings()


def save_settings(settings: Settings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings.clamped()), indent=2), encoding="utf-8")
