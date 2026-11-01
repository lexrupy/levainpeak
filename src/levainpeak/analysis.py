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

from levainpeak.alignment import (
    MIN_SCORE, Alignment, PotReference, carry, load_reference, locate, make_reference,
    reference_path, save_reference,
)
from levainpeak.filters import OutlierGate
from levainpeak.target import find_plus
from levainpeak.profiles import DAY, INFRARED, CaptureProfile, ProfileSelector
from levainpeak.stream import RtspStream
from levainpeak.temperature import read_temperature
from levainpeak.vision import ContainerMeasurement, measure_container

MIN_TEMPERATURE_C = 0.0
MAX_TEMPERATURE_C = 60.0


MAX_PLUS_SLIDE = 200  # px the mark alone may move the drawing sideways
MAX_PLUS_CORRECTION = 25  # px the "+" may correct the edges' height by; more is a false find


class ContainerAnalyzer:
    def __init__(self, stream: RtspStream, interval_seconds: float = 0.5) -> None:
        self.stream = stream
        self.roi: tuple[int, int, int, int] | None = None
        self.volume_offset = 0.0  # joins the session's series across a redrawn contour
        self.outline: tuple[tuple[int, int], ...] | None = None  # the pot's inner contour drawn by the user
        self.empty_pot: ContainerMeasurement | None = None  # outline known, no levain in it (whole-object reads only)
        self.temperature_roi: tuple[int, int, int, int] | None = None
        self.temperature_quad: tuple[tuple[int, int], ...] | None = None  # LCD face corners drawn by the user
        self.anchor_temperature: float | None = None  # last logged value of a resumed session
        self.ink_cut_infrared: float = 0.30  # black/white cuts of the drawn face, set from the window
        self.ink_cut_day: int = 40
        self.marker_y: int | None = None  # the hand-placed mark, carried along with the pot
        # Following the pot when it is moved: one reference per camera profile (the
        # infrared and day pictures do not look alike), made when the drawing is set.
        # The volume reads a little differently in infrared and by day (the foam is opaque
        # in one and clear in the other). At every change of profile, and when a session is
        # resumed, the new reading is joined to the last value instead of stepping.
        self.anchor_volume: float | None = None  # last logged volume of a resumed session
        self._join_offset = 0.0
        self._joined_profile: str | None = None
        self._last_volume: float | None = None
        self.align_enabled = True
        self.mark_seen: tuple[float, float, int] | None = None  # the last "+"/"X" found: x, y, size
        self._mark_slide = 0.0  # px the label has slid sideways on the pot, from the edges' view
        self.use_mark = False  # follow the pot's height by the "+" or "X" on its label
        self.alignment_status: str | None = None  # a short text when the pot cannot be found
        self.aligned: tuple | None = None  # (outline, roi, marker_y) the drawing was carried to; the window takes it
        self._references: dict[str, PotReference] = {}
        self._reference_generation = 0
        self._last_alignment = 0.0
        self._alignment_candidate: tuple | None = None
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._results: queue.Queue[tuple[tuple[int, ContainerMeasurement | None, float | None], tuple]] = queue.Queue(maxsize=1)
        # The regions the result last returned by latest_result() was measured with.
        self.latest_key: tuple | None = None
        self._temperature_c: float | None = None
        self._last_temperature_roi: tuple[int, int, int, int] | None = None
        self._last_temperature_quad: tuple[tuple[int, int], ...] | None = None
        self._last_temperature_read = 0.0
        self._last_temperature_success = 0.0
        self._temperature_updated_at: float | None = None
        self._measurement_history: deque[ContainerMeasurement] = deque(maxlen=7)
        self._geometry_candidates: deque[tuple[int, int, int, int]] = deque(maxlen=5)
        self._geometry_anchor: tuple[int, int, int, int] | None = None
        self._last_stable_measurement: ContainerMeasurement | None = None
        self._missing_measurements = 0
        self._last_pot_roi: tuple[int, int, int, int] | None = None
        self._last_outline: tuple[tuple[int, int], ...] | None = None
        self._filtered_surface_y: float | None = None
        # Out-of-curve readings are discarded and the next frame is read again.
        self._volume_gate = OutlierGate(max_jump=0.03, confirmations=40)
        # Infrared reads are blurrier, so a wrong digit is likelier: only small,
        # well-confirmed moves are trusted there, anchored on the last good read.
        self._temperature_gates = {
            DAY.name: OutlierGate(max_jump=3.0, confirmations=3),
            INFRARED.name: OutlierGate(max_jump=0.25, confirmations=6, big_jump=1.0, big_confirmations=40),
        }
        self._gate_profile: str | None = None
        self._previous_reading: float | None = None
        # Capture profile (day or infrared) and the vessel's top and bottom per
        # profile. Daylight finds them by shading that vanishes in infrared, so
        # the camera and pot being fixed, infrared reuses what daylight measured.
        self._profile_selector = ProfileSelector()
        self.profile: CaptureProfile = DAY
        self.vessel_bounds: dict[str, tuple[int, int]] = {}
        self._thread = threading.Thread(target=self._run, name="container-analyzer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def effective_bounds(self) -> tuple[int, int] | None:
        """Vessel top and bottom to impose, or None to detect them from the picture."""
        if not self.profile.infrared:
            return None
        return self.vessel_bounds.get(self.profile.name) or self.vessel_bounds.get(DAY.name)

    def _learn_vessel_bounds(self, measurement: ContainerMeasurement | None) -> None:
        if measurement is None or self.profile.infrared or measurement.confidence < 0.6:
            return
        learned = (measurement.top, measurement.bottom)
        known = self.vessel_bounds.get(DAY.name)
        if known is None or max(abs(a - b) for a, b in zip(learned, known)) > 2:
            self.vessel_bounds[DAY.name] = learned

    def temperature_age_seconds(self) -> float | None:
        """Seconds since the temperature was last read; None if it never was."""
        if self._temperature_updated_at is None:
            return None
        return time.monotonic() - self._temperature_updated_at

    def reset_join(self) -> None:
        """Forget the series of the previous session: a new one starts from what is read."""
        self.volume_offset = 0.0
        self.anchor_volume = None
        self._join_offset = 0.0
        self._joined_profile = None
        self._last_volume = None

    def _join_volume(self, raw: float) -> float:
        name = self.profile.name
        if self._joined_profile != name:
            reference = self._last_volume if self._last_volume is not None else self.anchor_volume
            if reference is not None and abs(reference - (raw + self.volume_offset)) < 0.15:
                self._join_offset = reference - raw - self.volume_offset
            self._joined_profile = name
        volume = raw + self.volume_offset + self._join_offset
        self._last_volume = volume
        return volume

    def set_reference(self, frame, outline, roi, marker_y) -> None:
        """The pot, as drawn, is where it is in this frame: later frames are matched to it."""
        self._reference_generation += 1
        self._references.clear()
        self._mark_slide = 0.0
        self.aligned = None
        self._alignment_candidate = None
        self.alignment_status = None
        self.marker_y = marker_y
        if frame is None or outline is None:
            return
        reference = make_reference(frame, outline, roi, marker_y, self._plus_in(frame, roi))
        if reference is not None:
            self._store_reference(self.profile.name, reference)

    def _plus_in(self, frame, roi) -> tuple[float, float] | None:
        """Where the "+" on the label is, by day. In infrared it is too faint to be found."""
        if self.profile.infrared or not self.use_mark:
            self.mark_seen = None
            return None
        found = find_plus(frame, roi)
        self.mark_seen = None if found is None else (found.x, found.y, found.size)
        return None if found is None else (found.x, found.y)

    def load_references(self) -> None:
        """The references of an earlier run, so a pot moved while the app was off is found."""
        for profile in (DAY, INFRARED):
            reference = load_reference(reference_path(profile.name))
            if reference is not None:
                self._references[profile.name] = reference

    def _store_reference(self, name: str, reference: PotReference) -> None:
        self._references[name] = reference
        try:
            save_reference(reference, reference_path(name))
        except OSError:
            pass

    def _follow_pot(self, frame) -> None:
        """Carry the drawn contour to where the pot is now, when it has moved or come closer or farther."""
        outline, roi = self.outline, self.roi
        if not self.align_enabled or outline is None or roi is None:
            return
        now = time.monotonic()
        if now - self._last_alignment < 3.0:
            return
        self._last_alignment = now
        name, generation = self.profile.name, self._reference_generation
        reference = self._references.get(name)
        if reference is None:
            # A first look in this picture profile: the pot is taken to be where it is drawn.
            reference = make_reference(frame, outline, roi, self.marker_y, self._plus_in(frame, roi))
            if reference is not None:
                self._store_reference(name, reference)
            return
        found = locate(reference, frame)
        found = self._with_plus(reference, found, frame, roi)
        if found is None or found.score < MIN_SCORE:
            self.alignment_status = "pote não localizado"
            self._alignment_candidate = None
            return
        self.alignment_status = None
        moved = carry(reference, found)
        if generation != self._reference_generation:
            return  # the drawing was replaced while this was being searched
        shift = max(
            max(abs(a - c), abs(b - d)) for (a, b), (c, d) in zip(moved[0], outline, strict=True)
        )
        if shift < 3:
            self._alignment_candidate = None
            return
        # Two searches in a row must agree, so a hand passing over the pot does not move the drawing.
        candidate = self._alignment_candidate
        if candidate is None or max(
            max(abs(a - c), abs(b - d)) for (a, b), (c, d) in zip(moved[0], candidate[0], strict=True)
        ) > 3:
            self._alignment_candidate = moved
            return
        self._alignment_candidate = None
        self.outline, self.roi = moved[0], moved[1]
        if moved[2] is not None:
            self.marker_y = moved[2]
        self.aligned = (moved[0], moved[1], self.marker_y)

    def _with_plus(self, reference: PotReference, found: Alignment | None, frame, roi) -> Alignment | None:
        """Use the "+" or "X" on the label, when it is in view, for where the pot is.

        With the pot's edges found, the mark only corrects the height, and how far the label has
        slid sideways from where the edges put it is kept: that is the pot turning in place.
        When the edges find nothing (a lamp changed the light) the mark alone moves the drawing,
        up or down and sideways, less the slide of the label seen last.
        """
        if reference.plus is None:
            return found
        seen = self._plus_in(frame, roi)
        if seen is None:
            return found
        origin_x, origin_y = reference.origin
        if found is not None and found.score >= MIN_SCORE:
            expected_x = found.dx + (reference.plus[0] - origin_x) * found.scale
            expected_y = found.dy + (reference.plus[1] - origin_y) * found.scale
            self._mark_slide = seen[0] - expected_x
            correction = seen[1] - expected_y
            if abs(correction) > MAX_PLUS_CORRECTION:
                return found
            return Alignment(found.scale, found.dx, found.dy + correction, found.score)
        slide_x = seen[0] - reference.plus[0] - self._mark_slide
        rise = seen[1] - reference.plus[1]
        if abs(rise) > MAX_PLUS_CORRECTION * 3 or abs(slide_x) > MAX_PLUS_SLIDE:
            return found
        return Alignment(1.0, float(origin_x) + slide_x, float(origin_y) + rise, MIN_SCORE)

    def region_key(self) -> tuple:
        """The regions as they are set now (pot area, contour, thermometer area, face corners)."""
        return (self.roi, self.outline, self.temperature_roi, self.temperature_quad)

    def latest_result(
        self,
    ) -> tuple[int, ContainerMeasurement | None, float | None] | None:
        """The newest result, or None. ``latest_key`` then says which regions it was measured with:
        when that is not ``region_key()`` the regions were replaced meanwhile and it is stale."""
        result = None
        while True:
            try:
                result, self.latest_key = self._results.get_nowait()
            except queue.Empty:
                return result

    def _publish(
        self, result: tuple[int, ContainerMeasurement | None, float | None], key: tuple
    ) -> None:
        item = (result, key)
        try:
            self._results.put_nowait(item)
        except queue.Full:
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait(item)
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
            try:
                self._follow_pot(frame)
            except cv2.error:
                pass
            # One reading of the regions for the whole pass, and the same key on the result.
            key = self.region_key()
            roi, outline = key[0], key[1]
            if roi != self._last_pot_roi or outline != self._last_outline:
                self._last_pot_roi = roi
                self._last_outline = outline
                self._measurement_history.clear()
                self._geometry_candidates.clear()
                self._geometry_anchor = None
                self._last_stable_measurement = None
                self._missing_measurements = 0
                self._filtered_surface_y = None
                self._volume_gate.reset()
            previous_profile = self.profile.name
            self.profile = self._profile_selector.update(frame)
            if self.profile.name != previous_profile:
                self._measurement_history.clear()
                self._filtered_surface_y = None
            try:
                measurement = measure_container(
                    frame, roi=roi, vertical_bounds=self.effective_bounds(),
                    outline=outline,
                )
            except cv2.error:
                measurement = None
            # An empty pot is not a measurement: the session sees "no pot" as before,
            # and the window shows the pot as empty.
            self.empty_pot = measurement if measurement is not None and measurement.empty else None
            if self.empty_pot is not None:
                measurement = None
            measurement = self._stabilize_measurement(measurement)
            self._learn_vessel_bounds(measurement)
            now = time.monotonic()
            temperature_roi, temperature_quad = key[2], key[3]
            if temperature_roi != self._last_temperature_roi or temperature_quad != self._last_temperature_quad:
                self._last_temperature_roi = temperature_roi
                self._last_temperature_quad = temperature_quad
                self._temperature_c = None
                self._temperature_updated_at = None
                self._last_temperature_read = 0.0
                self._last_temperature_success = 0.0
                for gate in self._temperature_gates.values():
                    gate.reset()
                    if self.anchor_temperature is not None:
                        gate.trusted = self.anchor_temperature  # a resumed session keeps its last good value
                if self.anchor_temperature is not None:
                    self._temperature_c = self.anchor_temperature
                self._gate_profile = None
                self._previous_reading = None
            if temperature_roi is not None and now - self._last_temperature_read >= 2.0:
                self._last_temperature_read = now
                try:
                    reading = read_temperature(
                        frame, temperature_roi, infrared=self.profile.infrared,
                        face_quad=temperature_quad,
                        ink_cut=self.ink_cut_infrared if self.profile.infrared else self.ink_cut_day,
                    )
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
                    gate = self._temperature_gates[self.profile.name]
                    if self._gate_profile != self.profile.name:
                        self._gate_profile = self.profile.name
                        if self._temperature_c is not None:
                            gate.trusted = self._temperature_c
                    if repeated and gate.accept(reading):
                        self._temperature_c = reading
                        self._temperature_updated_at = now
                # When the display cannot be read, the last good reading stays.
            self._publish((frame_number, measurement, self._temperature_c), key)
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
        volume_fraction = self._join_volume(volume_fraction)

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
            # In the same frame as the volume, so a joined series still reads 1.00x at the mark.
            initial_fraction = volume_profile[marker_index] + self.volume_offset + self._join_offset
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
