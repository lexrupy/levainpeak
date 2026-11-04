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
INK_CUT_INFRARED = (0.10, 0.80)  # fraction of the face's contrast; lower reads fainter strokes
INK_CUT_DAY = (20, 60)  # brightness percentile of the face below which a pixel is ink
DEFAULT_INK_CUT_INFRARED = 0.30  # what "auto" puts back
DEFAULT_INK_CUT_DAY = 40
PEAK_MINUTES = (3, 60)  # how long the readings stay level before the levain is called at its peak
LAMP_MINUTES = (0, 60)  # how long the volume is held after the lamp changes the picture; 0 turns it off
SNAPSHOT_MINUTES = (1, 120)  # how often a raw frame is kept beside the session log
BASE_DOUBLING_HOURS = (2.0, 48.0)  # hours to double at 24 °C with a 1:5:5 starter, for the forecast by the table
ARTIFACT_MINUTES = (3, 60)  # a departure that comes back within this is a lamp or a lifted pot, not the levain


ROTATIONS = (0, 90, 180, 270)  # quarter turns offered in the video menu
ROTATION_DEGREES = (-195.0, 195.0)  # degrees clockwise the camera picture is turned
MARK_SHAPES = ("círculo", "quadrado")


@dataclass
class Settings:
    record_interval_seconds: int = 10
    phone_enabled: bool = False
    phone_interval_seconds: int = 120
    phone_snapshot: bool = False
    overlay_brightness: int = 50
    graph_temperature_scale: str = "ampla"
    graph_volume_scale: str = "auto"
    graph_show_start: bool = True  # the graph starts at the mix, with the stretch before the monitoring
    graph_show_rate: bool = True  # a strip under the graph with the growth rate
    graph_show_forecast: bool = True  # the graph draws the doubling forecast over the measured curve
    ink_cut_infrared: float = DEFAULT_INK_CUT_INFRARED
    ink_cut_day: int = DEFAULT_INK_CUT_DAY
    mark_shape: str = "círculo"  # drawn around the followed mark: "círculo" or "quadrado"
    peak_minutes: int = 10
    artifact_minutes: int = 12
    lamp_minutes: int = 10
    snapshot_minutes: int = 10
    base_doubling_hours: float = 8.0
    rotation: float = 0.0
    mark_alignment: bool = False  # follow the pot's height by the "+" or "X" on its label

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
            graph_show_start=bool(self.graph_show_start),
            graph_show_rate=bool(self.graph_show_rate),
            graph_show_forecast=bool(self.graph_show_forecast),
            ink_cut_infrared=round(min(INK_CUT_INFRARED[1], max(INK_CUT_INFRARED[0], float(self.ink_cut_infrared))), 2),
            ink_cut_day=min(INK_CUT_DAY[1], max(INK_CUT_DAY[0], int(self.ink_cut_day))),
            peak_minutes=min(PEAK_MINUTES[1], max(PEAK_MINUTES[0], int(self.peak_minutes))),
            artifact_minutes=min(ARTIFACT_MINUTES[1], max(ARTIFACT_MINUTES[0], int(self.artifact_minutes))),
            lamp_minutes=min(LAMP_MINUTES[1], max(LAMP_MINUTES[0], int(self.lamp_minutes))),
            snapshot_minutes=min(SNAPSHOT_MINUTES[1], max(SNAPSHOT_MINUTES[0], int(self.snapshot_minutes))),
            base_doubling_hours=round(
                min(BASE_DOUBLING_HOURS[1], max(BASE_DOUBLING_HOURS[0], float(self.base_doubling_hours))), 1
            ),
            rotation=round(min(ROTATION_DEGREES[1], max(ROTATION_DEGREES[0], float(self.rotation))), 1),
            mark_alignment=bool(self.mark_alignment),
            mark_shape=self.mark_shape if self.mark_shape in MARK_SHAPES else "círculo",
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
            graph_show_start=bool(data.get("graph_show_start", defaults.graph_show_start)),
            graph_show_rate=bool(data.get("graph_show_rate", defaults.graph_show_rate)),
            graph_show_forecast=bool(data.get("graph_show_forecast", defaults.graph_show_forecast)),
            ink_cut_infrared=float(data.get("ink_cut_infrared", defaults.ink_cut_infrared)),
            ink_cut_day=int(data.get("ink_cut_day", defaults.ink_cut_day)),
            peak_minutes=int(data.get("peak_minutes", defaults.peak_minutes)),
            artifact_minutes=int(data.get("artifact_minutes", defaults.artifact_minutes)),
            lamp_minutes=int(data.get("lamp_minutes", defaults.lamp_minutes)),
            snapshot_minutes=int(data.get("snapshot_minutes", defaults.snapshot_minutes)),
            base_doubling_hours=float(data.get("base_doubling_hours", defaults.base_doubling_hours)),
            rotation=float(data.get("rotation", defaults.rotation)),
            mark_alignment=bool(data.get("mark_alignment", defaults.mark_alignment)),
            mark_shape=str(data.get("mark_shape", defaults.mark_shape)),
        ).clamped()
    except FileNotFoundError:
        return defaults
    except (OSError, AttributeError, TypeError, ValueError):
        logging.warning("Configurações inválidas em %s; usando o padrão", path)
        return defaults


def save_settings(settings: Settings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings.clamped()), indent=2), encoding="utf-8")
