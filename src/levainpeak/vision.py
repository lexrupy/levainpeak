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
    empty: bool = False  # the outline is known but no levain was found in it
    # What gave the surface: "automático" (3 px strips), "faixas" (the drawn bands), "central" (middle of the
    # glass, the original way), or "" when it was read some other way (no outline).
    method: str = ""
    volume_below_profile: tuple[float, ...] = field(default=(), repr=False, compare=False)
    # (left_x, right_x) of the inner wall for each row from top to bottom.
    edge_profile: tuple[tuple[int, int], ...] = field(default=(), repr=False, compare=False)


def measure_container(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] | None = None,
    *,
    center_preference: float = 0.4,
    preferred_width: float = 0.22,
    width_weight: float = 280.0,
    support_weight: float = 220.0,
    vertical_bounds: tuple[int, int] | None = None,
    outline: tuple[tuple[int, int], ...] | None = None,
    surface_bands: tuple[tuple[float, float], ...] = (),
    auto_strips: bool = False,
) -> ContainerMeasurement | None:
    """Find a prominent vessel and estimate its filled-volume fraction.

    ``vertical_bounds`` (top, bottom in frame coordinates) replaces the
    detection of the vessel's top and bottom, which relies on daylight shading.

    ``surface_bands`` (at least ``MIN_SURFACE_BANDS`` of them, as fractions of the glass's inner width)
    limit the search for the levain surface to those narrow strips, where the light is steady, instead
    of the middle half of the pot; each strip finds the surface alone and the result is their median.

    ``auto_strips`` reads the surface in many narrow strips across the glass that vote (see
    ``surface_strips``); with ``surface_bands`` too, only the strips inside those bands vote.

    ``outline`` is the vessel's inner contour drawn by the user (polygon, frame
    coordinates). It replaces the search for the walls, top and bottom, so a
    ribbed, tapered or reflective glass can be measured; only the levain surface
    is still searched. With an outline and no levain found the result is an
    ``empty`` measurement instead of ``None``.

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
            vertical_bounds=(
                None
                if vertical_bounds is None
                else (vertical_bounds[0] - y0, vertical_bounds[1] - y0)
            ),
            outline=None if outline is None else tuple((x - x0, y - y0) for x, y in outline),
            surface_bands=surface_bands, auto_strips=auto_strips,
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
            edge_profile=tuple((a + x0, b + x0) for a, b in local.edge_profile),
        )

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gradient_x = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    profile = gradient_x[int(height * 0.05) : int(height * 0.98)].sum(axis=0)
    profile = np.convolve(profile, np.ones(11, dtype=np.float32) / 11, mode="same")

    outline_edges = None
    if outline is not None:
        outline_edges = _outline_edges(outline, width, height)
        if outline_edges is None:
            return None
        pot_top, outer_left, outer_right = outline_edges[0], outline_edges[1], outline_edges[2]
        pot_bottom = pot_top + len(outer_left)
        # The columns every row has inside the glass: the search for the mark and
        # the surface must not run over the tapered wall or the background.
        left, right = int(np.ceil(outer_left.max())), int(np.floor(outer_right.min()))
        if right - left < 30 or pot_bottom - pot_top < 100:
            return None
        edge_confidence = 1.0
    else:
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
        if vertical_bounds is not None:
            pot_top, pot_bottom = vertical_bounds
        else:
            pot_top, pot_bottom = _container_bounds(frame, left, right)
        if pot_bottom - pot_top < int(height * 0.3):
            return None

    inner_top = pot_top
    inner_bottom = pot_bottom
    if outline_edges is not None:
        # The dark-band search was made for one elastic on one pot; on a drawn
        # contour any ring in the glass looks like a band and pushes the surface
        # search above the real level. The mark is placed by hand there.
        marker_y, marker_confidence = None, 0.0
    else:
        marker_y, marker_confidence = _find_initial_marker(
            frame, left, right, inner_top, inner_bottom
        )
    surface = None
    method = ""
    if outline_edges is not None:
        infrared = float(np.abs(frame[..., 0].astype(np.int16) - frame[..., 2]).mean()) < 3
        surface = (
            _find_surface_by_strips(gray, left, right, inner_top, inner_bottom, infrared, surface_bands) if auto_strips else None
        )
        method = "automático"
        if surface is None:
            surface = _find_surface_in_bands(gray, left, right, inner_top, inner_bottom, infrared, surface_bands)
            method = "faixas"
        if surface is None:
            surface = _find_surface_from_below(gray, left, right, inner_top, inner_bottom, infrared)
            method = "central"
        if surface is None:
            method = ""
    if surface is None and marker_confidence >= 0.2:
        surface = _find_surface_by_columns(frame, left, right, inner_top, inner_bottom, marker_y)
    if surface is None:
        surface = _find_surface(
            frame,
            left,
            right,
            inner_top,
            inner_bottom,
            marker_y=marker_y if marker_confidence >= 0.2 else None,
        )
    if surface is None:
        if outline_edges is None:
            return None
        return _empty_outline_measurement(outline_edges)
    surface_y, surface_confidence = surface

    # The vessel's bottom comes from a gradient guess (or from an earlier
    # daylight measurement) and can lie below the glass, on the steel bowl the
    # jar sits on. Cut it where the pale levain ends so the bowl is not counted
    # as starter: that inflated the initial volume and understated the growth.
    base_y = _levain_base(frame, left, right, surface_y, marker_y if marker_confidence >= 0.2 else None, inner_bottom)
    if base_y is not None and base_y < inner_bottom - 3:
        inner_bottom = base_y

    if outline_edges is not None:
        rows = inner_bottom - inner_top
        left_edges = outline_edges[1][:rows].astype(np.float32)
        right_edges = outline_edges[2][:rows].astype(np.float32)
        widths = np.maximum(1.0, right_edges - left_edges).astype(np.float32)
        empty_rows = 0
    else:
        widths, left_edges, right_edges = _width_profile(
            gradient_x, left, right, inner_top, inner_bottom
        )
        empty_rows = int(np.clip(surface_y - inner_top, 0, len(widths)))
    if empty_rows >= 60:
        # Above the levain the pot is empty glass against a busy background:
        # trust a robust straight (or conical) wall rather than noisy edges.
        left_edges = left_edges.copy()
        right_edges = right_edges.copy()
        # The glass wall lies right next to the detected outer edge; anything
        # farther in is something else (levain or a label on the wall).
        tolerance = 0.02 * (right - left)
        left_edges[:empty_rows] = np.clip(
            _robust_wall(left_edges[:empty_rows]), left, left + tolerance
        )
        right_edges[:empty_rows] = np.clip(
            _robust_wall(right_edges[:empty_rows]), right - tolerance, right
        )
        widths = np.maximum(1.0, right_edges - left_edges).astype(np.float32)
    area_profile = np.square(widths)
    total = float(area_profile.sum())
    if total <= 0:
        return None
    filled = float(area_profile[max(0, surface_y - inner_top) :].sum())
    fraction = float(np.clip(filled / total, 0.0, 1.0))
    volume_below_profile = tuple(
        float(value) for value in (np.cumsum(area_profile[::-1])[::-1] / total)
    )
    if outline_edges is not None:
        left, right = int(left_edges.min()), int(np.ceil(right_edges.max()))
    measurement = ContainerMeasurement(
        left=left,
        right=right,
        top=inner_top,
        bottom=inner_bottom,
        surface_y=surface_y,
        volume_fraction=fraction,
        confidence=float(np.clip(0.45 * edge_confidence + 0.55 * surface_confidence, 0, 1)),
        volume_below_profile=volume_below_profile,
        edge_profile=tuple(
            (int(a), int(b)) for a, b in zip(left_edges, right_edges, strict=True)
        ),
        method=method,
    )
    return set_initial_marker(measurement, marker_y, manual=False) if marker_y is not None else measurement


def set_initial_marker(
    measurement: ContainerMeasurement, marker_y: int, *, manual: bool = True, shift: float = 0.0
) -> ContainerMeasurement:
    """Set the starter's initial-height reference and recompute its growth ratio.

    ``shift`` is what the series was moved by (joined across a redrawn contour or a day/infrared
    change); the mark is read in the same frame as the volume, so a mark at the surface reads 1.00x."""
    if not measurement.volume_below_profile:
        return measurement
    y = int(np.clip(marker_y, measurement.top, measurement.bottom - 1))
    index = int(np.clip(y - measurement.top, 0, len(measurement.volume_below_profile) - 1))
    initial_fraction = measurement.volume_below_profile[index] + shift
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


def draw_measurement(
    frame: np.ndarray, measurement: ContainerMeasurement, *, detail: bool = False,
    faint_box: bool = False, outline: tuple[tuple[int, int], ...] | None = None,
) -> np.ndarray:
    """Draw the detected vessel bounds and estimated levain surface.

    With ``detail`` the measured inner wall profile is traced and the area
    counted as levain (from the surface down) is shaded. With ``faint_box`` the
    bounding box is a thin translucent grey: the contour drawn by the user is
    the real outline, and the box only repeats it.
    """
    annotated = frame.copy()
    if detail and measurement.edge_profile:
        _draw_profile(annotated, measurement)
    color = (73, 190, 125)
    corners = ((measurement.left, measurement.top), (measurement.right, measurement.bottom))
    if faint_box:
        overlay = annotated.copy()
        cv2.rectangle(overlay, *corners, (190, 200, 195), 1, cv2.LINE_AA)
        cv2.addWeighted(overlay, 0.4, annotated, 0.6, 0, annotated)
    else:
        cv2.rectangle(annotated, *corners, color, 3)
    def span(y: int) -> tuple[int, int]:
        return _outline_span(outline, y) or _edges_at(measurement, y)

    surface_left, surface_right = span(measurement.surface_y)
    cv2.line(
        annotated,
        (surface_left, measurement.surface_y),
        (surface_right, measurement.surface_y),
        (40, 210, 255),
        3,
    )
    if measurement.marker_y is not None:
        marker_color = (255, 190, 60) if measurement.marker_manual else (255, 160, 50)
        marker_left, marker_right = span(measurement.marker_y)
        for x in range(marker_left, marker_right, 24):
            cv2.line(
                annotated,
                (x, measurement.marker_y),
                (min(x + 12, marker_right), measurement.marker_y),
                marker_color,
                2,
            )
    label = f"Pote | volume ~{measurement.volume_fraction:.0%}"
    if measurement.growth_factor is not None:
        label += f" | {measurement.growth_factor:.1f}x marca inicial"
    label_x, label_top = (min(x for x, _ in outline), min(y for _, y in outline)) if outline else (measurement.left, measurement.top)
    y = max(30, label_top - 12)
    cv2.putText(annotated, label, (label_x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(annotated, label, (label_x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    return annotated


def _outline_span(outline, y: int) -> tuple[int, int] | None:
    """Where the polygon's sides cross row ``y`` (leftmost, rightmost), or None when the row is outside it."""
    if not outline:
        return None
    crossings = []
    for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]):
        if y1 != y2 and min(y1, y2) <= y <= max(y1, y2):
            crossings.append(x1 + (x2 - x1) * (y - y1) / (y2 - y1))
    if len(crossings) < 2:
        return None
    return int(round(min(crossings))), int(round(max(crossings)))


def _edges_at(measurement: ContainerMeasurement, y: int) -> tuple[int, int]:
    """The vessel's left and right wall at row ``y``: a level line stops at the walls of a tapered pot."""
    profile = measurement.edge_profile
    if not profile or measurement.bottom <= measurement.top:
        return measurement.left, measurement.right
    fraction = min(1.0, max(0.0, (y - measurement.top) / (measurement.bottom - measurement.top)))
    left, right = profile[round(fraction * (len(profile) - 1))]
    if right - left < 10:
        return measurement.left, measurement.right
    return int(left), int(right)


MAX_WALL_SLOPE = 0.05


def inner_span(
    outline: tuple[tuple[int, int], ...], frame_size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """(left, right, top, bottom) of the glass the surface strips are fractions of: the columns every row
    of the drawn contour has inside it, and the rows the contour spans."""
    edges = _outline_edges(outline, frame_size[0], frame_size[1])
    if edges is None:
        return None
    top, outer_left, outer_right = edges
    left, right = int(np.ceil(outer_left.max())), int(np.floor(outer_right.min()))
    return (left, right, top, top + len(outer_left)) if right - left >= 30 else None


def _outline_edges(
    outline: tuple[tuple[int, int], ...], width: int, height: int
) -> tuple[int, np.ndarray, np.ndarray] | None:
    """(top row, left x per row, right x per row) of a polygon, clipped to the frame."""
    points = np.asarray(outline, dtype=np.float64)
    if points.ndim != 2 or len(points) < 3:
        return None
    points[:, 0] = np.clip(points[:, 0], 0, width - 1)
    points[:, 1] = np.clip(points[:, 1], 0, height - 1)
    # A hand-drawn top or bottom side is never level. The rows above the second
    # highest corner (and below the second lowest) are a sliver where the polygon
    # has only part of its width, so they are left out.
    heights = np.sort(points[:, 1])
    top, bottom = int(np.ceil(heights[1])), int(np.floor(heights[-2]))
    if bottom - top < 20:
        return None
    rows = np.arange(top, bottom)
    left = np.full(len(rows), np.inf)
    right = np.full(len(rows), -np.inf)
    for index in range(len(points)):
        (x1, y1), (x2, y2) = points[index], points[(index + 1) % len(points)]
        if y1 == y2:
            continue
        inside = (rows >= min(y1, y2)) & (rows <= max(y1, y2))
        crossing = x1 + (rows[inside] - y1) * (x2 - x1) / (y2 - y1)
        left[inside] = np.minimum(left[inside], crossing)
        right[inside] = np.maximum(right[inside], crossing)
    if not np.isfinite(left).all() or not np.isfinite(right).all():
        return None
    return top, left, right




def _empty_outline_measurement(outline_edges) -> ContainerMeasurement:
    top, left_edges, right_edges = outline_edges
    bottom = top + len(left_edges)
    return ContainerMeasurement(
        left=int(left_edges.min()),
        right=int(np.ceil(right_edges.max())),
        top=top,
        bottom=bottom,
        surface_y=bottom,
        volume_fraction=0.0,
        confidence=0.0,
        empty=True,
        edge_profile=tuple((int(a), int(b)) for a, b in zip(left_edges, right_edges, strict=True)),
    )


def _levain_base(
    frame: np.ndarray,
    left: int,
    right: int,
    surface_y: int,
    marker_y: int | None,
    bottom: int,
) -> int | None:
    """Row where the pale levain ends at the jar's base, or None when it cannot be told.

    Scans down the middle of the jar from just under the dark initial mark
    (skipping the mark itself) while the rows stay as pale as the levain.
    Needs the levain to be at or above the mark; otherwise the rows under the
    mark are not levain and there is nothing to anchor on.
    """
    if marker_y is None or surface_y > marker_y:
        return None
    start = marker_y + 30
    stop = min(bottom, frame.shape[0])
    if stop - start < 40:
        return None
    width = right - left
    centre = slice(left + int(width * 0.30), right - int(width * 0.30))
    rows = np.median(cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)[:, centre, 0].astype(np.float32), axis=1)
    levain = float(np.percentile(rows[start:start + 50], 80))
    if levain < 120:
        return None  # nothing pale under the mark
    dark = rows < 0.75 * levain
    for row in range(start, stop - 2):
        if dark[row] and dark[row + 1] and dark[row + 2]:
            return row
    return None


def _robust_wall(edge: np.ndarray) -> np.ndarray:
    """Line fit through wall positions, ignoring rows that grabbed other edges."""
    rows = np.arange(len(edge), dtype=np.float32)
    keep = np.ones(len(edge), dtype=bool)
    coefficients = np.polyfit(rows, edge, 1)
    for _ in range(3):
        coefficients = np.polyfit(rows[keep], edge[keep], 1)
        residual = edge - np.polyval(coefficients, rows)
        spread = np.median(np.abs(residual[keep] - np.median(residual[keep]))) + 1.0
        keep = np.abs(residual) <= max(4.0, 2.5 * spread)
        if keep.sum() < len(edge) * 0.3:
            break
    if abs(coefficients[0]) > MAX_WALL_SLOPE:
        # A glass wall is nearly vertical; a steep fit means noisy edges won.
        return np.full(len(edge), np.median(edge[keep]), dtype=np.float32)
    return np.polyval(coefficients, rows).astype(np.float32)


def _draw_profile(annotated: np.ndarray, measurement: ContainerMeasurement) -> None:
    edges = np.asarray(measurement.edge_profile, dtype=np.float32)
    rows = measurement.top + np.arange(len(edges), dtype=np.float32)
    left_wall = np.column_stack((edges[:, 0], rows)).astype(np.int32)
    right_wall = np.column_stack((edges[:, 1], rows)).astype(np.int32)
    below = rows >= measurement.surface_y
    if below.any():
        region = np.vstack((left_wall[below], right_wall[below][::-1]))
        shade = annotated.copy()
        cv2.fillPoly(shade, [region], (60, 160, 255))
        cv2.addWeighted(shade, 0.35, annotated, 0.65, 0, dst=annotated)
    for wall in (left_wall, right_wall):
        cv2.polylines(annotated, [wall.reshape(-1, 1, 2)], False, (255, 255, 0), 2, cv2.LINE_AA)


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


@dataclass(frozen=True)
class ColumnScan:
    """What the per-column surface search saw, for diagnostics."""

    columns: tuple[int, ...]  # x of every scanned column
    tops: tuple[tuple[int, int], ...]  # (x, y) where the pale mass ends
    levain: float  # lightness of the levain
    glass: float  # lightness of the empty glass
    threshold: float  # halfway between the two


def column_scan(
    frame: np.ndarray, left: int, right: int, top: int, bottom: int, marker_y: int
) -> ColumnScan | None:
    """Scan every column upward from the dark mark while it stays levain-pale.

    Returns None when levain and empty glass are not clearly different in
    lightness, or when too few columns found an end to the pale mass.
    """
    height = bottom - top
    width = right - left
    if height < 100 or width < 30 or marker_y - 3 <= top + int(height * 0.12):
        return None
    lightness = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    columns = np.arange(left + int(width * 0.10), right - int(width * 0.10), 3)
    limit = top + int(height * 0.10)
    scan_end = marker_y - 3
    window = lightness[limit:scan_end, columns[0]:columns[-1]]
    levain = float(np.percentile(np.median(window, axis=1), 95))
    glass = float(
        np.median(
            lightness[top + int(height * 0.15):top + int(height * 0.30), columns[0]:columns[-1]]
        )
    )
    if levain - glass < 40:
        return None
    threshold = (levain + glass) / 2
    tops: list[tuple[int, int]] = []
    for x in columns:
        column = lightness[limit:scan_end, x - 1:x + 2].mean(axis=1)
        row = len(column) - 1
        # Skip the shaded rows right above the mark until the pale levain starts.
        while row >= 0 and column[row] < threshold and len(column) - 1 - row < 45:
            row -= 1
        if row < 0 or column[row] < threshold:
            continue
        dark_rows = 0
        while row >= 0:
            if column[row] < threshold:
                dark_rows += 1
                if dark_rows >= 5:
                    break
            else:
                dark_rows = 0
            row -= 1
        if row >= 0:
            tops.append((int(x), int(limit + row + dark_rows)))
    if len(tops) < len(columns) * 0.4:
        return None
    return ColumnScan(
        tuple(int(x) for x in columns), tuple(tops), levain, glass, float(threshold)
    )


_INFRARED_CUT = 0.4
_DAY_CUT = 0.65
_FRONT_REACH = 0.12  # of the jar's width: how far below the cut the front edge of the surface may lie
_FRONT_STEP = 0.18  # of the levain-to-glass difference: the rise that marks that front edge


def _find_surface_by_strips(
    gray: np.ndarray, left: int, right: int, top: int, bottom: int, infrared: bool,
    bands: tuple[tuple[float, float], ...] = (),
) -> tuple[int, float] | None:
    """The surface the many narrow strips vote for (None when they do not agree enough)."""
    from levainpeak.surface_strips import MIN_CONFIDENCE, read_strips

    reading = read_strips(gray, left, right, top, bottom, infrared, bands=bands if len(bands) >= MIN_SURFACE_BANDS else ())
    if reading.level is None or reading.confidence < MIN_CONFIDENCE:
        return None
    return int(round(reading.level)), float(min(1.0, reading.confidence + 0.3))


MIN_SURFACE_BANDS = 2
MAX_SURFACE_BANDS = 4
_BAND_AGREEMENT = 6  # rows: bands this close to the median count as agreeing


def _find_surface_in_bands(
    gray: np.ndarray, left: int, right: int, top: int, bottom: int, infrared: bool,
    bands: tuple[tuple[float, float], ...],
) -> tuple[int, float] | None:
    """The surface read only inside the user's narrow strips: the median of what each strip finds.

    The confidence is the share of strips that agree with that median. None when fewer than
    ``MIN_SURFACE_BANDS`` strips find a surface, so the caller reads the whole width instead.
    """
    if len(bands) < MIN_SURFACE_BANDS:
        return None
    width = right - left
    found = []
    for start, end in bands[:MAX_SURFACE_BANDS]:
        x0, x1 = left + int(round(start * width)), left + int(round(end * width))
        if x1 - x0 < 3:
            continue
        surface = _surface_in_columns(gray, x0, x1, top, bottom, infrared, width)
        if surface is not None:
            found.append(surface[0])
    if len(found) < MIN_SURFACE_BANDS:
        return None
    level = float(np.median(found))
    return int(round(level)), float(np.mean([abs(y - level) <= _BAND_AGREEMENT for y in found]))


def _find_surface_from_below(
    gray: np.ndarray, left: int, right: int, top: int, bottom: int, infrared: bool = False
) -> tuple[int, float] | None:
    """The top of the opaque, bright levain, found by climbing from the bottom of the jar.

    Clear glass above the levain shows the dim inside of the jar, and the far rim of the
    jar shows up in it as a bright reflection. An edge search takes that reflection for
    the surface; here the surface is the first sustained dim stretch met climbing up from
    the levain, so what lies above that stretch is ignored. A bright label on the glass
    does not stop the climb (only a dim stretch does).
    """
    width = right - left
    return _surface_in_columns(gray, left + width // 4, right - width // 4, top, bottom, infrared, width)


def _surface_in_columns(
    gray: np.ndarray, x0: int, x1: int, top: int, bottom: int, infrared: bool, width: int,
    fine: bool = False,
) -> tuple[float, float] | None:
    """The climb from the bottom of the jar, over the columns x0..x1 (``width``: the jar's).

    ``fine`` is for very narrow strips: the columns are averaged (not the median) and the surface is
    placed between rows, where the brightness crosses the cut, instead of on a whole row."""
    height = bottom - top
    if height < 100:
        return None
    columns = gray[top:bottom, x0:x1]
    reduced = columns.mean(axis=1) if fine else np.median(columns, axis=1)
    profile = cv2.GaussianBlur(reduced.astype(np.float32).reshape(-1, 1), (1, 9), 0).ravel()
    levain = float(np.median(profile[int(height * 0.75):int(height * 0.92)]))
    glass = float(np.percentile(profile[: int(height * 0.6)], 10))
    if levain <= 0 or levain - glass < 0.2 * levain:
        return None  # nothing dimmer than the levain above it: no surface to find
    # Infrared shows the levain's edge sharply, so its middle is the surface; by day the
    # foam's pale rim makes the transition wider and the edge sits nearer the levain.
    limit = glass + (_INFRARED_CUT if infrared else _DAY_CUT) * (levain - glass)
    # A stretch of clear glass is long; a dark band or ring on the levain is thin and
    # darker than any glass, so neither stops the climb.
    run = max(16, height // 25)
    dim = profile < limit
    for y in range(int(height * 0.92), run, -1):
        if dim[y - run:y].all() and float(np.median(profile[y - run:y])) >= 0.6 * glass:
            if not infrared:
                y = _front_edge(profile, y, int(_FRONT_REACH * width), _FRONT_STEP * (levain - glass))
            if fine:
                y = _crossing(profile, y, limit)
            return top + y, 0.9
    return None


def _crossing(profile: np.ndarray, y: int, limit: float) -> float:
    """Where, between rows, the profile climbs through ``limit`` going down from row ``y``."""
    for row in range(max(0, y - 2), min(len(profile) - 1, y + 14)):
        low, high = float(profile[row]), float(profile[row + 1])
        if low < limit <= high:
            return row + (limit - low) / (high - low)
    return float(y)


def _front_edge(profile: np.ndarray, y: int, reach: int, step: float) -> int:
    """Seen from a little above, the surface is a band: its far edge is higher in the picture than
    its near edge. The cut may land on the far edge when the foam is bright (a pale band against
    the clear glass); the series is read at the near edge, where the pale body begins. If a clear
    rise follows within ``reach`` rows, that is the edge."""
    window = 16  # the edge is soft: judge the rise over a stretch, not a single row
    end = min(len(profile) - window - 1, y + reach)
    if end <= y:
        return y
    rise = profile[y + window:end + window] - profile[y:end]
    best = int(np.argmax(rise))
    return y + best + window // 2 if float(rise[best]) >= step and best > 8 else y


def _find_surface_by_columns(
    frame: np.ndarray, left: int, right: int, top: int, bottom: int, marker_y: int
) -> tuple[int, float] | None:
    """Levain surface as the median top of the pale mass above the initial mark.

    The median over columns ignores foam, levain climbing one wall and
    reflection streaks, which a gradient-based edge would lock onto. Returns
    None when the scan finds nothing usable, so the caller can fall back to
    the edge-based search.
    """
    scan = column_scan(frame, left, right, top, bottom, marker_y)
    if scan is None:
        return None
    found = np.asarray([y for _, y in scan.tops])
    level = _surface_cluster(found)
    confidence = float(np.mean(np.abs(found - level) <= 6))
    return int(round(level)), confidence


def _surface_cluster(tops: np.ndarray, radius: int = 6, min_share: float = 0.25) -> float:
    """Median of the lowest group of column tops that enough columns agree on.

    Pale things above the surface (a label, bright background seen through the
    empty glass) make a column's scan run past the real surface, never stop
    short of it. So the surface is the lowest level in the image with solid
    support, not the median of everything, which those columns drag upward.
    """
    ordered = np.sort(tops)
    need = max(5, int(np.ceil(len(ordered) * min_share)))
    for index in range(len(ordered) - 1, -1, -1):
        group = ordered[(ordered >= ordered[index] - 2 * radius) & (ordered <= ordered[index])]
        if len(group) >= need:
            return float(np.median(group))
    return float(np.median(ordered))


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
    # A real fill boundary should show in most horizontal bands. The median of
    # the three still rejects label edges and reflections confined to one side,
    # but tolerates one band being confused (for example levain or a label
    # climbing the wall hides the transition in the side band).
    scores = np.median(np.asarray(band_scores), axis=0)
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
    # Inside the levain a shading edge can score nearly as high as the real
    # surface. The surface is the highest strong transition into pale levain.
    strong = np.flatnonzero(
        (candidate_scores >= peak * 0.6)
        & (direction[candidate_start:candidate_end] >= 8.0)
    )
    if strong.size:
        index = int(strong[0]) + candidate_start
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
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    height, width = gradient_x.shape
    rows = np.arange(max(0, top), min(height, bottom))
    left_start, left_end = max(0, left - 42), min(width, left + 43)
    right_start, right_end = max(0, right - 42), min(width, right + 25)
    smoothed = cv2.blur(gradient_x, (1, 9))
    left_edges = left_start + np.argmax(smoothed[rows, left_start:left_end], axis=1)
    right_edges = right_start + np.argmax(smoothed[rows, right_start:right_end], axis=1)
    widths = np.maximum(1, right_edges - left_edges).astype(np.float32)
    kernel = np.ones(21, dtype=np.float32) / 21

    def smooth(values: np.ndarray) -> np.ndarray:
        # A wide running median ignores wood-grain streaks that grab the edge
        # for a few rows, yet keeps the real taper of the base; then a light
        # blur removes the remaining steps.
        window = min(61, len(values) | 1)
        padded = np.pad(values.astype(np.float32), window // 2, mode="edge")
        median = np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)
        padded = np.pad(median, 10, mode="edge")
        return np.convolve(padded, kernel, mode="valid")

    left_smooth, right_smooth = smooth(left_edges), smooth(right_edges)
    return (
        np.maximum(1.0, right_smooth - left_smooth).astype(np.float32),
        left_smooth,
        right_smooth,
    )
