"""Initial contour and relative-volume estimator for the fixed camera view."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import cv2
import numpy as np


@dataclass(frozen=True)
class ContainerMeasurement:
    left: int
    right: int
    top: int
    bottom: int
    surface_y: int
    volume_fraction: float
    confidence: float
    marker_y: int | None = None
    initial_volume_fraction: float | None = None
    growth_factor: float | None = None
    marker_manual: bool = False
    volume_below_profile: tuple[float, ...] = field(default=(), repr=False, compare=False)


def measure_container(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] | None = None,
    *,
    center_preference: float = 0.4,
    preferred_width: float = 0.22,
    width_weight: float = 280.0,
    support_weight: float = 220.0,
) -> ContainerMeasurement | None:
    """Find a prominent vessel and estimate its filled-volume fraction.

    This first pass is tuned for the current fixed side view. It assumes a
    mostly round, transparent pot and a visible color/texture transition at the
    levain surface. It returns ``None`` when the image does not provide a clear
    enough measurement.
    """
    if frame is None or frame.ndim != 3 or frame.shape[0] < 100 or frame.shape[1] < 100:
        return None

    if roi is not None:
        frame_height, frame_width = frame.shape[:2]
        x0, y0, x1, y1 = (
            int(np.clip(value, 0, limit))
            for value, limit in zip(roi, (frame_width, frame_height, frame_width, frame_height), strict=True)
        )
        if x1 - x0 < 80 or y1 - y0 < 120:
            return None
        local = measure_container(
            frame[y0:y1, x0:x1],
            center_preference=0.5,
            preferred_width=0.52,
            width_weight=600.0,
            support_weight=80.0,
        )
        if local is None:
            return None
        return replace(
            local,
            left=local.left + x0,
            right=local.right + x0,
            top=local.top + y0,
            bottom=local.bottom + y0,
            surface_y=local.surface_y + y0,
            marker_y=None if local.marker_y is None else local.marker_y + y0,
        )

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gradient_x = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    profile = gradient_x[int(height * 0.05) : int(height * 0.98)].sum(axis=0)
    profile = np.convolve(profile, np.ones(11, dtype=np.float32) / 11, mode="same")

    pair = _select_sides(
        profile, width, gray,
        preferred_center=center_preference,
        preferred_width=preferred_width,
        width_weight=width_weight,
        support_weight=support_weight,
    )
    if pair is None:
        return None
    left, right, edge_confidence = pair
    pot_top, pot_bottom = _container_bounds(frame, left, right)
    if pot_bottom - pot_top < int(height * 0.3):
        return None

    inner_top = pot_top
    inner_bottom = pot_bottom
    marker_y, marker_confidence = _find_initial_marker(
        frame, left, right, inner_top, inner_bottom
    )
    surface = _find_surface(
        frame,
        left,
        right,
        inner_top,
        inner_bottom,
        marker_y=marker_y if marker_confidence >= 0.2 else None,
    )
    if surface is None:
        return None
    surface_y, surface_confidence = surface

    widths = _width_profile(gradient_x, left, right, inner_top, inner_bottom)
    area_profile = np.square(widths)
    total = float(area_profile.sum())
    if total <= 0:
        return None
    filled = float(area_profile[max(0, surface_y - inner_top) :].sum())
    fraction = float(np.clip(filled / total, 0.0, 1.0))
    volume_below_profile = tuple(
        float(value) for value in (np.cumsum(area_profile[::-1])[::-1] / total)
    )
    measurement = ContainerMeasurement(
        left=left,
        right=right,
        top=inner_top,
        bottom=inner_bottom,
        surface_y=surface_y,
        volume_fraction=fraction,
        confidence=float(np.clip(0.45 * edge_confidence + 0.55 * surface_confidence, 0, 1)),
        volume_below_profile=volume_below_profile,
    )
    return set_initial_marker(measurement, marker_y, manual=False) if marker_y is not None else measurement


def set_initial_marker(
    measurement: ContainerMeasurement, marker_y: int, *, manual: bool = True
) -> ContainerMeasurement:
    """Set the starter's initial-height reference and recompute its growth ratio."""
    if not measurement.volume_below_profile:
        return measurement
    y = int(np.clip(marker_y, measurement.top, measurement.bottom - 1))
    index = int(np.clip(y - measurement.top, 0, len(measurement.volume_below_profile) - 1))
    initial_fraction = measurement.volume_below_profile[index]
    growth_factor = (
        measurement.volume_fraction / initial_fraction if initial_fraction > 0.01 else None
    )
    return replace(
        measurement,
        marker_y=y,
        initial_volume_fraction=initial_fraction,
        growth_factor=growth_factor,
        marker_manual=manual,
    )


def set_initial_volume_reference(
    measurement: ContainerMeasurement, initial_fraction: float
) -> ContainerMeasurement:
    """Set a volume baseline without drawing a physical marker on the pot."""
    initial_fraction = float(np.clip(initial_fraction, 0.0, 1.0))
    growth_factor = (
        measurement.volume_fraction / initial_fraction
        if initial_fraction > 0.01
        else None
    )
    return replace(
        measurement,
        initial_volume_fraction=initial_fraction,
        growth_factor=growth_factor,
    )


def draw_measurement(frame: np.ndarray, measurement: ContainerMeasurement) -> np.ndarray:
    """Draw the detected vessel bounds and estimated levain surface."""
    annotated = frame.copy()
    color = (73, 190, 125)
    cv2.rectangle(
        annotated,
        (measurement.left, measurement.top),
        (measurement.right, measurement.bottom),
        color,
        3,
    )
    cv2.line(
        annotated,
        (measurement.left, measurement.surface_y),
        (measurement.right, measurement.surface_y),
        (40, 210, 255),
        3,
    )
    if measurement.marker_y is not None:
        marker_color = (255, 190, 60) if measurement.marker_manual else (255, 160, 50)
        for x in range(measurement.left, measurement.right, 24):
            cv2.line(
                annotated,
                (x, measurement.marker_y),
                (min(x + 12, measurement.right), measurement.marker_y),
                marker_color,
                2,
            )
    label = f"Pote | volume ~{measurement.volume_fraction:.0%}"
    if measurement.growth_factor is not None:
        label += f" | {measurement.growth_factor:.1f}x marca inicial"
    y = max(30, measurement.top - 12)
    cv2.putText(annotated, label, (measurement.left, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(annotated, label, (measurement.left, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    return annotated


def _select_sides(
    profile: np.ndarray,
    image_width: int,
    gray: np.ndarray,
    *,
    preferred_center: float = 0.4,
    preferred_width: float = 0.22,
    width_weight: float = 280.0,
    support_weight: float = 220.0,
) -> tuple[int, int, float] | None:
    peaks = _peaks(profile)
    integral = np.pad(gray.cumsum(axis=1, dtype=np.float64), ((0, 0), (1, 0)))
    edge_map = cv2.blur(
        np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)), (1, 11)
    )
    height = gray.shape[0]
    support_top, support_bottom = int(height * 0.15), int(height * 0.98)
    profile_scale = max(1, int(gray.shape[0] * 0.93))
    candidates = []
    for left, left_strength in peaks:
        if left < 0.04 * image_width or left > 0.88 * image_width:
            continue
        for right, right_strength in peaks:
            if right <= left or right > 0.96 * image_width:
                continue
            width_ratio = (right - left) / image_width
            if not 0.16 <= width_ratio <= max(0.5, preferred_width + 0.25):
                continue
            side_support = []
            for side in (left, right):
                x0, x1 = max(0, side - 8), min(image_width, side + 9)
                row_edges = edge_map[support_top:support_bottom, x0:x1].max(axis=1)
                side_support.append(float(np.mean(row_edges > 40)))
            persistent_support = min(side_support)
            if persistent_support < 0.55:
                continue
            band_score = _horizontal_band_score(gray, integral, left, right)
            edge_strength = (left_strength + right_strength) / profile_scale
            center_ratio = (left + right) / (2 * image_width)
            center_prior = 450 * abs(center_ratio - preferred_center)
            score = (
                edge_strength
                + 6.0 * band_score
                - width_weight * abs(width_ratio - preferred_width)
                + support_weight * persistent_support
                - center_prior
            )
            candidates.append((score, left, right, left_strength + right_strength))
    if not candidates:
        return None
    _, left, right, pair_strength = max(candidates)
    confidence = min(1.0, pair_strength / max(1.0, 2 * float(profile.max())))
    return left, right, confidence


def _horizontal_band_score(
    gray: np.ndarray, integral: np.ndarray, left: int, right: int
) -> float:
    width = right - left
    x0 = left + int(width * 0.08)
    x1 = right - int(width * 0.08)
    if x1 - x0 < 12:
        return 0.0
    row_mean = (integral[:, x1] - integral[:, x0]) / (x1 - x0)
    smoothed = cv2.blur(row_mean.astype(np.float32).reshape(-1, 1), (1, 9)).ravel()
    prefix = np.concatenate(([0.0], np.cumsum(smoothed, dtype=np.float64)))
    centers = np.arange(35, len(smoothed) - 35, 4)
    center_mean = (prefix[centers + 6] - prefix[centers - 5]) / 11
    above = (prefix[centers - 15] - prefix[centers - 30]) / 15
    below = (prefix[centers + 30] - prefix[centers + 15]) / 15
    return float(np.max((above + below) / 2 - center_mean))


def _peaks(profile: np.ndarray) -> list[tuple[int, float]]:
    maxima = [
        (index, float(profile[index]))
        for index in range(1, len(profile) - 1)
        if profile[index] >= profile[index - 1] and profile[index] > profile[index + 1]
    ]
    selected: list[tuple[int, float]] = []
    for candidate in sorted(maxima, key=lambda peak: peak[1], reverse=True):
        if all(abs(candidate[0] - previous[0]) > 14 for previous in selected):
            selected.append(candidate)
        if len(selected) >= 32:
            break
    return selected


def _container_bounds(frame: np.ndarray, left: int, right: int) -> tuple[int, int]:
    height, width = frame.shape[:2]
    vessel_width = right - left
    sample_left = max(0, left + int(vessel_width * 0.08))
    sample_right = min(width, right - int(vessel_width * 0.08))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    row_level = np.asarray(
        [np.median(gray[y, sample_left:sample_right]) for y in range(height)],
        dtype=np.float32,
    )
    row_level = np.convolve(row_level, np.ones(11, dtype=np.float32) / 11, mode="same")

    top_start, top_end = int(height * 0.02), int(height * 0.5)
    top_region = row_level[top_start:top_end]
    low = float(top_region.min())
    high = float(np.percentile(top_region, 85))
    dark_limit = low + 0.4 * (high - low)
    dark_rows = np.flatnonzero(top_region <= dark_limit) + top_start
    groups = np.split(dark_rows, np.flatnonzero(np.diff(dark_rows) > 8) + 1)
    usable_groups = [group for group in groups if len(group) >= 8]
    if usable_groups:
        top = int(max(usable_groups, key=len)[-1]) + 4
    else:
        top = int(height * 0.24)

    bottom_start, bottom_end = int(height * 0.82), int(height * 0.99)
    bottom_gradient = np.abs(np.gradient(row_level[bottom_start:bottom_end]))
    bottom = bottom_start + int(np.argmax(bottom_gradient))
    if bottom <= top:
        bottom = int(height * 0.96)
    return top, bottom


def _find_surface(
    frame: np.ndarray,
    left: int,
    right: int,
    top: int,
    bottom: int,
    *,
    marker_y: int | None = None,
) -> tuple[int, float] | None:
    height, width = frame.shape[:2]
    vessel_width = right - left
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    vessel_height = bottom - top
    if vessel_height < 32:
        return None

    radius = max(5, int(vessel_height * 0.012))
    start, end = int(vessel_height * 0.12), int(vessel_height * 0.88)
    band_scores = []
    for start_ratio, end_ratio in ((0.03, 0.20), (0.25, 0.75), (0.80, 0.97)):
        sample_left = max(0, left + int(vessel_width * start_ratio))
        sample_right = min(width, left + int(vessel_width * end_ratio))
        rows = np.asarray(
            [
                np.median(lab[y, sample_left:sample_right], axis=0)
                for y in range(top, bottom)
            ],
            dtype=np.float32,
        )
        scores = np.zeros(vessel_height, dtype=np.float32)
        for index in range(start + radius, min(end, vessel_height - radius)):
            before = np.median(rows[index - radius : index], axis=0)
            after = np.median(rows[index : index + radius], axis=0)
            scores[index] = float(np.linalg.norm(after - before))
        smooth_radius = max(2, radius // 2)
        scores = np.convolve(
            scores,
            np.ones(2 * smooth_radius + 1, dtype=np.float32)
            / (2 * smooth_radius + 1),
            mode="same",
        )
        band_scores.append(scores)
    # A real fill boundary should be visible in every horizontal band. The
    # minimum rejects strong label edges and reflections confined to one side.
    scores = np.min(np.asarray(band_scores), axis=0)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    vessel_left = max(0, left + int(vessel_width * 0.08))
    vessel_right = min(width, right - int(vessel_width * 0.08))
    whole_vessel_level = gray[:, vessel_left:vessel_right].mean(axis=1)
    dark_band = (whole_vessel_level < 65).astype(np.float32)
    dark_nearby = np.convolve(
        dark_band,
        np.ones(2 * radius + 1, dtype=np.float32),
        mode="same",
    )
    scores[dark_nearby[top:bottom] > 0] = 0
    candidate_start = start + radius
    candidate_end = min(end, vessel_height - radius)
    if marker_y is not None:
        # The starter's current surface is expected above its dark initial
        # level mark (smaller image y). Restricting the search to this section
        # prevents a lower edge, label, or reflection from becoming the level.
        marker_index = marker_y - top
        candidate_start = max(
            candidate_start, marker_index - max(8, int(vessel_height * 0.12))
        )
        candidate_end = min(candidate_end, marker_index)
    local = scores[candidate_start:candidate_end]
    if len(local) == 0:
        return None
    # Prefer transitions into the pale, opaque levain. Color contrast alone
    # also gives high scores on labels and glass reflections.
    central_left = max(0, left + int(vessel_width * 0.30))
    central_right = min(width, right - int(vessel_width * 0.30))
    lightness = lab[:, central_left:central_right, 0].mean(axis=1)
    direction = np.zeros_like(scores)
    for candidate in range(candidate_start, candidate_end):
        before = float(np.median(lightness[top + candidate - radius : top + candidate]))
        after = float(np.median(lightness[top + candidate : top + candidate + radius]))
        direction[candidate] = max(0.0, after - before)
    candidate_scores = local * (0.35 + np.minimum(direction[candidate_start:candidate_end] / 25.0, 1.0))
    index = int(np.argmax(candidate_scores)) + candidate_start
    peak = float(candidate_scores[index - candidate_start])
    baseline = float(np.median(local))
    if peak < 3.0:
        return None
    confidence = float(np.clip((peak - baseline) / max(peak, 1e-6), 0, 1))
    return top + index, confidence


def _find_initial_marker(
    frame: np.ndarray, left: int, right: int, top: int, bottom: int
) -> tuple[int | None, float]:
    height, width = frame.shape[:2]
    vessel_width = right - left
    x0 = max(0, left + int(vessel_width * 0.1))
    x1 = min(width, right - int(vessel_width * 0.1))
    if x1 - x0 < 16:
        return None, 0.0
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    region = gray[top:bottom, x0:x1]
    if len(region) < 16:
        return None, 0.0

    dark_fraction = (region < 85).mean(axis=1).astype(np.float32)
    dark_fraction = cv2.blur(dark_fraction.reshape(-1, 1), (1, 3)).ravel()
    pot_height = bottom - top
    min_thickness = max(2, int(pot_height * 0.003))
    max_thickness = max(8, int(pot_height * 0.12))
    dark_rows = np.flatnonzero(dark_fraction >= 0.28)
    groups = np.split(dark_rows, np.flatnonzero(np.diff(dark_rows) > 1) + 1)
    best: tuple[float, int] | None = None
    shoulder_inner = max(2, int(pot_height * 0.015))
    shoulder_outer = max(shoulder_inner + 3, int(pot_height * 0.05))
    for group in groups:
        thickness = len(group)
        if not min_thickness <= thickness <= max_thickness:
            continue
        start, end = int(group[0]), int(group[-1]) + 1
        if start < pot_height * 0.1 or end > pot_height * 0.92:
            continue
        before_start = max(0, start - shoulder_outer)
        before_end = max(before_start, start - shoulder_inner)
        after_start = min(pot_height, end + shoulder_inner)
        after_end = min(pot_height, end + shoulder_outer)
        shoulders = np.concatenate(
            (dark_fraction[before_start:before_end], dark_fraction[after_start:after_end])
        )
        if not len(shoulders):
            continue
        band_darkness = float(np.mean(dark_fraction[start:end]))
        contrast = band_darkness - float(np.median(shoulders))
        if contrast < 0.22:
            continue
        score = contrast * band_darkness
        center = top + (start + end - 1) // 2
        if best is None or score > best[0]:
            best = (score, center)

    if best is None:
        return None, 0.0
    confidence = float(np.clip(best[0], 0, 1))
    return best[1], confidence


def _width_profile(
    gradient_x: np.ndarray, left: int, right: int, top: int, bottom: int
) -> np.ndarray:
    height, width = gradient_x.shape
    rows = np.arange(max(0, top), min(height, bottom))
    left_start, left_end = max(0, left - 42), min(width, left + 43)
    right_start, right_end = max(0, right - 42), min(width, right + 25)
    smoothed = cv2.blur(gradient_x, (1, 9))
    left_edges = left_start + np.argmax(smoothed[rows, left_start:left_end], axis=1)
    right_edges = right_start + np.argmax(smoothed[rows, right_start:right_end], axis=1)
    widths = np.maximum(1, right_edges - left_edges).astype(np.float32)
    return np.convolve(widths, np.ones(21, dtype=np.float32) / 21, mode="same")
