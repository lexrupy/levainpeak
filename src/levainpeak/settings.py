"""User settings kept in a small local JSON file."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from levainpeak.graph_data import TEMPERATURE_SCALES, VOLUME_SCALES

SETTINGS_PATH = Path("data/settings.json")
MIN_RECORD_SECONDS = 10
MAX_RECORD_SECONDS = 120
MIN_PHONE_SECONDS = 60
MAX_PHONE_SECONDS = 900
MIN_OVERLAY_BRIGHTNESS = 10  # percent of the picture's brightness kept outside the marked regions
MAX_OVERLAY_BRIGHTNESS = 100  # 100: nothing is dimmed


@dataclass
class Settings:
    record_interval_seconds: int = 10
    phone_enabled: bool = False
    phone_interval_seconds: int = 120
    phone_snapshot: bool = False
    overlay_brightness: int = 50
    graph_temperature_scale: str = "ampla"
    graph_volume_scale: str = "auto"

    def clamped(self) -> "Settings":
        return Settings(
            record_interval_seconds=min(
                MAX_RECORD_SECONDS, max(MIN_RECORD_SECONDS, int(self.record_interval_seconds))
            ),
            phone_enabled=bool(self.phone_enabled),
            phone_interval_seconds=min(
                MAX_PHONE_SECONDS, max(MIN_PHONE_SECONDS, int(self.phone_interval_seconds))
            ),
            phone_snapshot=bool(self.phone_snapshot),
            overlay_brightness=min(
                MAX_OVERLAY_BRIGHTNESS, max(MIN_OVERLAY_BRIGHTNESS, int(self.overlay_brightness))
            ),
            graph_temperature_scale=(
                self.graph_temperature_scale if self.graph_temperature_scale in TEMPERATURE_SCALES else "ampla"
            ),
            graph_volume_scale=self.graph_volume_scale if self.graph_volume_scale in VOLUME_SCALES else "auto",
        )


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    defaults = Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Files written before the phone options existed only have the first key.
        return Settings(
            record_interval_seconds=int(data.get("record_interval_seconds", defaults.record_interval_seconds)),
            phone_enabled=bool(data.get("phone_enabled", defaults.phone_enabled)),
            phone_interval_seconds=int(data.get("phone_interval_seconds", defaults.phone_interval_seconds)),
            phone_snapshot=bool(data.get("phone_snapshot", defaults.phone_snapshot)),
            overlay_brightness=int(data.get("overlay_brightness", defaults.overlay_brightness)),
            graph_temperature_scale=str(data.get("graph_temperature_scale", defaults.graph_temperature_scale)),
            graph_volume_scale=str(data.get("graph_volume_scale", defaults.graph_volume_scale)),
        ).clamped()
    except FileNotFoundError:
        return defaults
    except (OSError, AttributeError, TypeError, ValueError):
        logging.warning("Configurações inválidas em %s; usando o padrão", path)
        return defaults


def save_settings(settings: Settings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings.clamped()), indent=2), encoding="utf-8")
