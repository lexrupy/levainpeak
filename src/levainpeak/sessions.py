"""Session CSV logging and conservative levain phase tracking."""

from __future__ import annotations

import csv
import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import TextIO

from levainpeak.filters import valid_after_recovery


def latest_session_csv(directory: Path | None = None) -> Path | None:
    """The most recently written session CSV, or None when there is none."""
    files = (directory or Path("data/sessions")).glob("*.csv")
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


# How the volume is measured. Bump it when the geometry changes, so a session
# saved with an older measurement gets its initial level taken again: dividing
# a new volume by an old initial level is wrong by several percent.
GEOMETRY_VERSION = 2


class SessionLog:
    def __init__(self, directory: Path | None = None, sample_seconds: float = 10.0) -> None:
        self.directory = directory or Path("data/sessions")
        self.sample_seconds = sample_seconds
        self.session_id: str | None = None
        self.path: Path | None = None
        self._file: TextIO | None = None
        self._writer = None
        self._last_write_time: float | None = None
        self.snapshot_seconds = 600.0
        self._last_snapshot_time: float | None = None
        self._last_phase: str | None = None
        self.mix_time: datetime | None = None
        self.session_name = ""
        self._columns: list[str] = []
        self.metadata_path: Path | None = None
        self.regions: dict[str, tuple[int, int, int, int] | None] = {
            "pot": None,
            "thermometer": None,
        }
        # Inner contour of the pot the user drew (frame pixels), or None.
        self.pot_outline: tuple[tuple[int, int], ...] | None = None
        # Four corners of the thermometer's LCD face the user drew, or None.
        self.face_quad: tuple[tuple[int, int], ...] | None = None
        # Vessel (top, bottom) in frame pixels for each capture profile.
        self.vessel_bounds: dict[str, tuple[int, int]] = {}
        # Row (frame pixels) of the initial-level mark the user placed by hand.
        self.manual_marker_y: int | None = None
        # Added to every measured volume of this session: joins the series across a
        # change in how the pot is read (redrawn contour) that would show as a step.
        self.volume_offset: float = 0.0
        self.last_volume: float | None = None  # the volume of the last row written this run
        # Version of the volume measurement the saved initial level came from.
        self.geometry: int = GEOMETRY_VERSION

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, mix_time: datetime | None = None, name: str = "") -> str:
        if self.active:
            raise RuntimeError("A session is already active")
        now = datetime.now().astimezone()
        self.session_id = now.strftime("%Y%m%d_%H%M%S_%f")
        self.mix_time = mix_time or now
        self.session_name = name.strip()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{self.session_id}.csv"
        self.metadata_path = self.path.with_suffix(".json")
        self._file = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            (
                "session_id",
                "timestamp",
                "mix_time",
                "temperature_c",
                "volume_fraction",
                "initial_volume_fraction",
                "growth_factor",
                "phase",
                "session_name",
            )
        )
        self._columns = [
            "session_id", "timestamp", "mix_time", "temperature_c",
            "volume_fraction", "initial_volume_fraction", "growth_factor",
            "phase", "session_name",
        ]
        self._file.flush()
        self._last_write_time = None
        self._last_phase = None
        return self.session_id

    def save_regions(
        self,
        pot: tuple[int, int, int, int] | None,
        thermometer: tuple[int, int, int, int] | None,
        outline: tuple[tuple[int, int], ...] | None = ...,  # type: ignore[assignment]
        face_quad: tuple[tuple[int, int], ...] | None = ...,  # type: ignore[assignment]
    ) -> None:
        self.regions = {"pot": pot, "thermometer": thermometer}
        if outline is not ...:
            self.pot_outline = None if outline is None else tuple((int(x), int(y)) for x, y in outline)
        if face_quad is not ...:
            self.face_quad = None if face_quad is None else tuple((int(x), int(y)) for x, y in face_quad)
        if self.metadata_path is None:
            return
        payload = {
            name: list(region) if region is not None else None
            for name, region in self.regions.items()
        }
        payload["session_name"] = self.session_name
        payload["vessel_bounds"] = {name: list(bounds) for name, bounds in self.vessel_bounds.items()}
        payload["marker_y"] = self.manual_marker_y
        payload["geometry"] = self.geometry
        payload["pot_outline"] = None if self.pot_outline is None else [list(point) for point in self.pot_outline]
        payload["face_quad"] = None if self.face_quad is None else [list(point) for point in self.face_quad]
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_outline(self, outline: tuple[tuple[int, int], ...] | None) -> None:
        """Remember the pot contour drawn by the user (None: back to automatic detection)."""
        self.pot_outline = None if outline is None else tuple((int(x), int(y)) for x, y in outline)
        if self.metadata_path is None:
            return
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload["pot_outline"] = None if self.pot_outline is None else [list(point) for point in self.pot_outline]
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_geometry(self, version: int) -> None:
        """Record which volume measurement the initial level now comes from."""
        self.geometry = int(version)
        if self.metadata_path is None:
            return
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload["geometry"] = self.geometry
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_snapshot_seconds(self, seconds: float) -> None:
        """Set how often frames are kept and remember it with the session, so a resume keeps it."""
        self.snapshot_seconds = float(seconds)
        if self.metadata_path is None:
            return
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload["snapshot_seconds"] = self.snapshot_seconds
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_marker(self, marker_y: int | None) -> None:
        """Remember the hand-placed initial-level mark (None: back to automatic)."""
        self.manual_marker_y = None if marker_y is None else int(marker_y)
        if self.metadata_path is None:
            return
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload["marker_y"] = self.manual_marker_y
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_vessel_bounds(self, bounds: dict[str, tuple[int, int]]) -> None:
        """Remember the vessel's top and bottom per profile in the session file."""
        self.vessel_bounds = {name: (int(a), int(b)) for name, (a, b) in bounds.items()}
        if self.metadata_path is None:
            return
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload["vessel_bounds"] = {name: list(b) for name, b in self.vessel_bounds.items()}
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def resume(self, path: Path) -> tuple[str, dict[str, tuple[int, int, int, int] | None]]:
        if self.active:
            raise RuntimeError("A session is already active")
        path = path.expanduser().resolve()
        with path.open("r", newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        if not rows:
            raise ValueError("O CSV não contém medições para retomar.")
        last = rows[-1]
        self.path = path
        self.directory = path.parent
        self.metadata_path = path.with_suffix(".json")
        self.session_id = last.get("session_id") or path.stem
        self.session_name = last.get("session_name", "")
        self.mix_time = datetime.fromisoformat(last["mix_time"]) if last.get("mix_time") else None
        if self.metadata_path.exists():
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if not self.session_name:
                self.session_name = raw.get("session_name", "")
            self.regions = {
                name: tuple(value) if value else None
                for name, value in raw.items()
                if name in ("pot", "thermometer")
            }
            saved = raw.get("vessel_bounds") or {}
            if isinstance(saved, list):  # a single pair means the daylight measurement
                saved = {"dia": saved}
            self.vessel_bounds = {name: (int(v[0]), int(v[1])) for name, v in saved.items() if v}
            outline = raw.get("pot_outline")
            self.pot_outline = tuple((int(x), int(y)) for x, y in outline) if outline else None
            quad = raw.get("face_quad")
            self.face_quad = tuple((int(x), int(y)) for x, y in quad) if quad else None
            marker = raw.get("marker_y")
            self.manual_marker_y = None if marker is None else int(marker)
            self.volume_offset = float(raw.get("volume_offset") or 0.0)
            if raw.get("snapshot_seconds"):  # older sessions keep the caller's value
                self.snapshot_seconds = float(raw["snapshot_seconds"])
            self.geometry = int(raw.get("geometry", 1))  # files from before this existed are version 1
        else:
            self.regions = {"pot": None, "thermometer": None}
            self.pot_outline = None
            self.face_quad = None
            self.vessel_bounds = {}
            self.manual_marker_y = None
            self.volume_offset = 0.0
            self.geometry = GEOMETRY_VERSION
        with path.open("r", newline="", encoding="utf-8") as source:
            self._columns = next(csv.reader(source), [])
        # Add newer optional fields when continuing an older log, keeping all
        # existing samples and the new rows in one consistent CSV schema.
        for optional_column in ("temperature_c", "session_name"):
            if optional_column not in self._columns:
                self._columns.append(optional_column)
        if self._columns != list(rows[0].keys()):
            temporary_path = path.with_suffix(".csv.tmp")
            with temporary_path.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(destination, fieldnames=self._columns)
                writer.writeheader()
                for row in rows:
                    if not row.get("session_name"):
                        row["session_name"] = self.session_name
                    writer.writerow(row)
            os.replace(temporary_path, path)
        self._file = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._last_write_time = None
        self._last_phase = last.get("phase")
        return self.session_id, self.regions

    def record(
        self,
        monotonic_time: float,
        measurement,
        phase: str,
        *,
        temperature_c: float | None = None,
        force: bool = False,
    ) -> bool:
        if not self.active or self._writer is None or self._file is None:
            return False
        phase_changed = phase != self._last_phase
        interval_elapsed = (
            self._last_write_time is None
            or monotonic_time - self._last_write_time >= self.sample_seconds
        )
        if not force and not phase_changed and not interval_elapsed:
            return False

        values = {
            "session_id": self.session_id,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mix_time": "" if self.mix_time is None else self.mix_time.isoformat(timespec="minutes"),
            "temperature_c": "" if temperature_c is None else f"{temperature_c:.1f}",
            "volume_fraction": f"{measurement.volume_fraction:.6f}",
            "initial_volume_fraction": "" if measurement.initial_volume_fraction is None else f"{measurement.initial_volume_fraction:.6f}",
            "growth_factor": "" if measurement.growth_factor is None else f"{measurement.growth_factor:.4f}",
            "phase": phase,
            "session_name": self.session_name,
        }
        self._writer.writerow([values.get(column, "") for column in self._columns])
        self._file.flush()
        self._last_write_time = monotonic_time
        self._last_phase = phase
        self.last_volume = measurement.volume_fraction
        return True

    def snapshot(self, monotonic_time: float, frame) -> Path | None:
        """Keep the raw frame beside the session every `snapshot_seconds` (ten minutes), to audit it later."""
        if not self.active or self.path is None or frame is None:
            return None
        if (
            self._last_snapshot_time is not None
            and monotonic_time - self._last_snapshot_time < self.snapshot_seconds
        ):
            return None
        import cv2

        folder = self.path.with_name(f"{self.path.stem}_fotos")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{datetime.now().astimezone():%Y%m%d_%H%M%S}.jpg"
        if not cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 85]):
            return None
        self._last_snapshot_time = monotonic_time
        return target

    def set_mix_time(self, mix_time: datetime) -> None:
        self.mix_time = mix_time

    def stop(self) -> Path | None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None
        return self.path


class PhaseTracker:
    """Detects sustained rise, a probable plateau peak, then confirmed decline."""

    WAITING = "Aquecendo"
    RISING = "Crescendo"
    PLATEAU = "Estabilizando"
    PEAK = "Em pico"
    FALL_CHECK = "Queda em confirmação"
    FALLING = "Decaindo"

    def __init__(
        self,
        *,
        minimum_rise: float = 0.03,
        minimum_peak_rise: float = 0.06,
        plateau_slope_per_minute: float = 0.00012,
        plateau_seconds: float = 600,
        decline_fraction: float = 0.02,
        decline_slope_per_minute: float = 0.0003,
        decline_seconds: float = 600,
        artifact_seconds: float = 720,
        slope_window_seconds: float = 300,
        plateau_window_seconds: float = 900,
        filter_dips: bool = True,
    ) -> None:
        self.minimum_rise = minimum_rise
        # A plateau, a peak or a fall only count once the volume has risen this far over the baseline:
        # a levain still warming up is flat too, and is not at its peak.
        self.minimum_peak_rise = minimum_peak_rise
        self.plateau_slope_per_minute = plateau_slope_per_minute
        self.plateau_seconds = plateau_seconds
        self.decline_fraction = decline_fraction
        self.decline_slope_per_minute = decline_slope_per_minute
        self.decline_seconds = decline_seconds
        # A fall that does not come back within this time was real, whatever came before it: a lamp or a
        # lifted pot is back within ten minutes, a collapse of the dough is not.
        self.artifact_seconds = artifact_seconds
        self.slope_window_seconds = slope_window_seconds
        self.plateau_window_seconds = plateau_window_seconds
        # Readings overtaken by a later recovery are left out of the trend. Turn it
        # off when the readings were already cleaned (the graph does).
        self.filter_dips = filter_dips
        self.reset()

    def reset(self, baseline: float | None = None) -> None:
        self.baseline = baseline
        self.peak_fraction: float | None = None
        self._raw: deque[float] = deque(maxlen=9)
        self._samples: deque[tuple[float, float]] = deque()
        self._last_sample_time: float | None = None
        self._flags: list[bool] = []
        self._flags_key: tuple | None = None
        self._plateau_since: float | None = None
        self._decline_since: float | None = None
        self._topped_out = False  # the volume has levelled off since it last made a new high
        self._low_since: float | None = None  # when the volume fell below its high by the decline fraction
        self.state = self.WAITING

    def set_baseline(self, baseline: float | None) -> None:
        if baseline is None:
            return
        self.baseline = baseline
        self._plateau_since = None
        self._decline_since = None
        if self.peak_fraction is None:
            self.state = self.WAITING

    def update(
        self,
        monotonic_time: float,
        volume_fraction: float,
        initial_volume_fraction: float | None = None,
    ) -> str:
        self._raw.append(float(volume_fraction))
        smooth = float(median(self._raw))
        if initial_volume_fraction is not None and self.baseline is None:
            self.baseline = initial_volume_fraction
        if self.baseline is None:
            self.baseline = smooth
        if self.peak_fraction is None or smooth > self.peak_fraction:
            if self.peak_fraction is not None and smooth > self.peak_fraction + self.decline_fraction / 2:
                self._topped_out = False  # a clearly new high: the top must level off again
            self.peak_fraction = smooth

        if (
            self._last_sample_time is None
            or monotonic_time - self._last_sample_time >= 10
        ):
            self._samples.append((monotonic_time, smooth))
            self._last_sample_time = monotonic_time
            cutoff = monotonic_time - max(self.slope_window_seconds * 3, 1800)
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

        if self.peak_fraction - self.baseline < self.minimum_rise:
            self._plateau_since = None
            self._decline_since = None
            self.state = self.WAITING
            return self.state

        slope = self._slope(monotonic_time)
        if slope is None:
            self.state = self.RISING
            return self.state

        if self.peak_fraction - self.baseline < self.minimum_peak_rise:
            self._plateau_since = None
            self._decline_since = None
            self.state = self.RISING if slope > self.plateau_slope_per_minute else self.WAITING
            return self.state

        drop = self.peak_fraction - smooth
        if drop >= self.decline_fraction:
            if self._low_since is None:
                self._low_since = monotonic_time
        else:
            self._low_since = None
        stayed_low = self._low_since is not None and monotonic_time - self._low_since >= self.artifact_seconds
        # A decline normally follows a stretch of stable readings at the top. A drop with no such
        # stretch before it (the pot lifted out, a lamp coming on) is not a decline yet: it is one
        # only once it has stayed low longer than such an artifact lasts.
        if drop >= self.decline_fraction and not self._topped_out and not stayed_low:
            self._decline_since = None
            self._plateau_since = None
            if self.state not in (self.RISING, self.WAITING):
                self.state = self.RISING
            return self.state
        if drop >= self.decline_fraction and stayed_low and slope > -self.decline_slope_per_minute:
            # fallen and settled low: it has declined, even if it no longer falls
            self._plateau_since = None
            self.state = self.FALLING
            return self.state
        if drop >= self.decline_fraction and slope <= -self.decline_slope_per_minute:
            if self._decline_since is None:
                self._decline_since = monotonic_time
            self._plateau_since = None
            self.state = (
                self.FALLING
                if monotonic_time - self._decline_since >= self.decline_seconds
                else self.FALL_CHECK
            )
            return self.state

        self._decline_since = None
        # Slow fermentation grows at about the noise level of a short window,
        # which made the phase flip between rising and stable. Judge the
        # plateau over a longer window, and make it harder to leave a plateau
        # than to enter one.
        trend = self._slope(monotonic_time, self.plateau_window_seconds)
        if trend is None:
            trend = slope
        limit = self.plateau_slope_per_minute
        if self.state in (self.PLATEAU, self.PEAK):
            limit *= 1.5
        if abs(trend) <= limit:
            if self._plateau_since is None:
                self._plateau_since = monotonic_time
            self.state = (
                self.PEAK
                if monotonic_time - self._plateau_since >= self.plateau_seconds
                else self.PLATEAU
            )
            self._topped_out = True
        elif trend > 0:
            self._plateau_since = None
            self.state = self.RISING
        else:
            self._plateau_since = None
            self.state = self.PLATEAU
        return self.state

    def _valid_flags(self) -> list[bool]:
        """Which stored samples are not proven wrong by a later recovery.

        Computed once per set of samples: both slope windows of one update share it.
        """
        if not self.filter_dips:
            return [True] * len(self._samples)
        key = (len(self._samples), self._samples[-1] if self._samples else None)
        if self._flags_key != key:
            self._flags = valid_after_recovery([value for _, value in self._samples])
            self._flags_key = key
        return self._flags

    def _slope(self, now: float, window: float | None = None) -> float | None:
        window = self.slope_window_seconds if window is None else window
        flags = self._valid_flags()
        recent = [
            (timestamp, value)
            for (timestamp, value), valid in zip(self._samples, flags, strict=True)
            if valid and now - timestamp <= window
        ]
        if len(recent) < 3:
            return None
        origin = recent[0][0]
        times = [timestamp - origin for timestamp, _ in recent]
        values = [value for _, value in recent]
        mean_time = sum(times) / len(times)
        mean_value = sum(values) / len(values)
        denominator = sum((value - mean_time) ** 2 for value in times)
        if denominator <= 0:
            return None
        slope_per_second = sum(
            (timestamp - mean_time) * (value - mean_value)
            for timestamp, value in zip(times, values, strict=True)
        ) / denominator
        return slope_per_second * 60

    def growth_rate_per_minute(self, now: float) -> float | None:
        """Return a recent growth rate after enough time has passed to be useful."""
        recent = [
            (timestamp, value)
            for timestamp, value in self._samples
            if now - timestamp <= self.slope_window_seconds
        ]
        if len(recent) < 6 or recent[-1][0] - recent[0][0] < 120:
            return None
        return self._slope(now)
