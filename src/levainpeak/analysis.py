"""Low-rate vision worker that analyzes camera snapshots off the UI thread."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import replace
from statistics import median

import cv2
import numpy as np

from levainpeak.filters import OutlierGate
from levainpeak.stream import RtspStream
from levainpeak.temperature import read_temperature
from levainpeak.vision import ContainerMeasurement, measure_container

MIN_TEMPERATURE_C = 0.0
MAX_TEMPERATURE_C = 60.0


class ContainerAnalyzer:
    def __init__(self, stream: RtspStream, interval_seconds: float = 0.5) -> None:
        self.stream = stream
        self.roi: tuple[int, int, int, int] | None = None
        self.temperature_roi: tuple[int, int, int, int] | None = None
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._results: queue.Queue[
            tuple[int, ContainerMeasurement | None, float | None]
        ] = queue.Queue(maxsize=1)
        self._temperature_c: float | None = None
        self._last_temperature_roi: tuple[int, int, int, int] | None = None
        self._last_temperature_read = 0.0
        self._last_temperature_success = 0.0
        self._temperature_updated_at: float | None = None
        self._measurement_history: deque[ContainerMeasurement] = deque(maxlen=7)
        self._geometry_candidates: deque[tuple[int, int, int, int]] = deque(maxlen=5)
        self._geometry_anchor: tuple[int, int, int, int] | None = None
        self._last_stable_measurement: ContainerMeasurement | None = None
        self._missing_measurements = 0
        self._last_pot_roi: tuple[int, int, int, int] | None = None
        self._filtered_surface_y: float | None = None
        # Out-of-curve readings are discarded and the next frame is read again.
        self._volume_gate = OutlierGate(max_jump=0.03, confirmations=40)
        self._temperature_gate = OutlierGate(max_jump=3.0, confirmations=3)
        self._previous_reading: float | None = None
        self._thread = threading.Thread(target=self._run, name="container-analyzer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def temperature_age_seconds(self) -> float | None:
        """Seconds since the temperature was last read; None if it never was."""
        if self._temperature_updated_at is None:
            return None
        return time.monotonic() - self._temperature_updated_at

    def latest_result(
        self,
    ) -> tuple[int, ContainerMeasurement | None, float | None] | None:
        result = None
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return result

    def _publish(
        self, result: tuple[int, ContainerMeasurement | None, float | None]
    ) -> None:
        try:
            self._results.put_nowait(result)
        except queue.Full:
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait(result)
            except queue.Full:
                pass

    def _run(self) -> None:
        last_frame_number = 0
        while not self._stop.is_set():
            frame_number, frame = self.stream.snapshot()
            if frame is None or frame_number == last_frame_number:
                self._stop.wait(0.1)
                continue
            last_frame_number = frame_number
            if self.roi != self._last_pot_roi:
                self._last_pot_roi = self.roi
                self._measurement_history.clear()
                self._geometry_candidates.clear()
                self._geometry_anchor = None
                self._last_stable_measurement = None
                self._missing_measurements = 0
                self._filtered_surface_y = None
                self._volume_gate.reset()
            try:
                measurement = measure_container(frame, roi=self.roi)
            except cv2.error:
                measurement = None
            measurement = self._stabilize_measurement(measurement)
            now = time.monotonic()
            temperature_roi = self.temperature_roi
            if temperature_roi != self._last_temperature_roi:
                self._last_temperature_roi = temperature_roi
                self._temperature_c = None
                self._temperature_updated_at = None
                self._last_temperature_read = 0.0
                self._last_temperature_success = 0.0
                self._temperature_gate.reset()
                self._previous_reading = None
            if temperature_roi is not None and now - self._last_temperature_read >= 2.0:
                self._last_temperature_read = now
                try:
                    reading = read_temperature(frame, temperature_roi)
                except cv2.error:
                    reading = None
                if reading is not None and not MIN_TEMPERATURE_C <= reading <= MAX_TEMPERATURE_C:
                    reading = None
                # A reading counts only when the previous read agrees with it,
                # so a one-off OCR slip never reaches the log or the forecast.
                repeated = reading is not None and reading == self._previous_reading
                self._previous_reading = reading
                if reading is not None:
                    self._last_temperature_success = now
                    if repeated and self._temperature_gate.accept(reading):
                        self._temperature_c = reading
                        self._temperature_updated_at = now
                # When the display cannot be read, the last good reading stays.
            self._publish((frame_number, measurement, self._temperature_c))
            self._stop.wait(self.interval_seconds)

    def _stabilize_measurement(
        self, measurement: ContainerMeasurement | None
    ) -> ContainerMeasurement | None:
        if measurement is None:
            self._missing_measurements += 1
            if self._missing_measurements <= 2:
                return self._last_stable_measurement
            self._measurement_history.clear()
            if self._missing_measurements >= 12:
                self._geometry_anchor = None
                self._geometry_candidates.clear()
                self._filtered_surface_y = None
                self._last_stable_measurement = None
                self._volume_gate.reset()
            return None

        self._missing_measurements = 0
        if not self._volume_gate.accept(measurement.volume_fraction):
            return self._last_stable_measurement
        geometry = (measurement.left, measurement.top, measurement.right, measurement.bottom)
        if self._geometry_anchor is None:
            self._geometry_anchor = geometry
            self._geometry_candidates.clear()
        elif not self._geometry_matches(geometry, self._geometry_anchor):
            self._geometry_candidates.append(geometry)
            if not self._consistent_geometry_change():
                # Ignore one-frame false detections. A moved/replaced pot is
                # adopted only after the new bounds remain consistent.
                return self._last_stable_measurement
            self._geometry_anchor = geometry
            self._geometry_candidates.clear()
            self._measurement_history.clear()
            self._filtered_surface_y = None
            self._volume_gate.reset()
            self._volume_gate.accept(measurement.volume_fraction)
        else:
            self._geometry_candidates.clear()
            self._geometry_anchor = tuple(
                round((old + new) / 2)
                for old, new in zip(self._geometry_anchor, geometry, strict=True)
            )

        self._measurement_history.append(measurement)
        history = tuple(self._measurement_history)
        left = round(median(sample.left for sample in history))
        right = round(median(sample.right for sample in history))
        top = round(median(sample.top for sample in history))
        bottom = round(median(sample.bottom for sample in history))
        measured_surface_y = float(median(sample.surface_y for sample in history))
        height = max(1, bottom - top)
        if self._filtered_surface_y is None:
            self._filtered_surface_y = measured_surface_y
        else:
            # A deadband and exponential low-pass reject frame-to-frame
            # contour jitter while still following sustained fermentation rise.
            error = measured_surface_y - self._filtered_surface_y
            if abs(error) >= 1.5:
                self._filtered_surface_y += 0.28 * error
        surface_y = round(self._filtered_surface_y)

        profiles = [
            np.interp(
                np.linspace(0, len(sample.volume_below_profile) - 1, height),
                np.arange(len(sample.volume_below_profile)),
                np.asarray(sample.volume_below_profile, dtype=np.float32),
            )
            for sample in history
            if sample.volume_below_profile
        ]
        if profiles:
            profile = np.median(np.stack(profiles), axis=0)
            surface_index = int(np.clip(surface_y - top, 0, height - 1))
            volume_fraction = float(profile[surface_index])
            volume_profile = tuple(float(value) for value in profile)
        else:
            volume_fraction = float(median(sample.volume_fraction for sample in history))
            volume_profile = measurement.volume_below_profile

        edge_profiles = [
            np.stack(
                [
                    np.interp(
                        np.linspace(0, len(sample.edge_profile) - 1, height),
                        np.arange(len(sample.edge_profile)),
                        np.asarray(sample.edge_profile, dtype=np.float32)[:, side],
                    )
                    for side in (0, 1)
                ],
                axis=1,
            )
            for sample in history
            if sample.edge_profile
        ]
        edge_profile = (
            tuple((round(a), round(b)) for a, b in np.median(np.stack(edge_profiles), axis=0))
            if edge_profiles
            else measurement.edge_profile
        )

        markers = [sample.marker_y for sample in history if sample.marker_y is not None]
        marker_y = round(median(markers)) if markers else None
        initial_fraction = None
        if marker_y is not None and volume_profile:
            marker_index = int(np.clip(marker_y - top, 0, len(volume_profile) - 1))
            initial_fraction = volume_profile[marker_index]
        growth_factor = (
            volume_fraction / initial_fraction
            if initial_fraction is not None and initial_fraction > 0.01
            else None
        )
        stable = replace(
            measurement,
            left=left,
            right=right,
            top=top,
            bottom=bottom,
            surface_y=surface_y,
            volume_fraction=volume_fraction,
            confidence=float(median(sample.confidence for sample in history)),
            marker_y=marker_y,
            initial_volume_fraction=initial_fraction,
            growth_factor=growth_factor,
            volume_below_profile=volume_profile,
            edge_profile=edge_profile,
        )
        self._last_stable_measurement = stable
        return stable

    @staticmethod
    def _geometry_matches(
        first: tuple[int, int, int, int], second: tuple[int, int, int, int]
    ) -> bool:
        left, top, right, bottom = first
        other_left, other_top, other_right, other_bottom = second
        width = max(1, right - left)
        height = max(1, bottom - top)
        return (
            abs(left - other_left) <= max(12, width * 0.12)
            and abs(right - other_right) <= max(12, width * 0.12)
            and abs(top - other_top) <= max(12, height * 0.08)
            and abs(bottom - other_bottom) <= max(12, height * 0.08)
        )

    def _consistent_geometry_change(self) -> bool:
        if len(self._geometry_candidates) < self._geometry_candidates.maxlen:
            return False
        first = self._geometry_candidates[0]
        return all(
            self._geometry_matches(first, candidate)
            for candidate in tuple(self._geometry_candidates)[1:]
        )
