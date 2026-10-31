"""Optional seven-segment temperature reading from a selected display region."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from levainpeak.imaging import is_infrared


_DIGITS = {
    0: "abcdef",
    1: "bc",
    2: "abged",
    3: "abgcd",
    4: "fgbc",
    5: "afgcd",
    6: "afgecd",
    7: "abc",
    8: "abcdefg",
    9: "abfgcd",
}
_SEGMENTS = {
    "a": (0.32, 0.02, 0.68, 0.24),
    "b": (0.70, 0.10, 0.98, 0.50),
    "c": (0.70, 0.50, 0.98, 0.90),
    "d": (0.32, 0.76, 0.68, 0.98),
    "e": (0.02, 0.50, 0.30, 0.90),
    "f": (0.02, 0.10, 0.30, 0.50),
    "g": (0.32, 0.39, 0.68, 0.61),
}


@dataclass(frozen=True)
class TemperatureInspection:
    """Every stage of one temperature read, for the diagnostics window."""

    roi_image: np.ndarray | None = None  # BGR crop of the selected region
    face: np.ndarray | None = None  # grayscale LCD face
    binary: np.ndarray | None = None  # deslanted ink mask (ink = 255)
    cell_boxes: tuple[tuple[int, int, int, int], ...] = ()  # left, top, right, bottom
    cells: tuple[np.ndarray, ...] = ()
    digits: tuple[int | None, ...] = ()
    occupancy: tuple[tuple[float, ...], ...] = ()  # per cell, segments a..g, 0..1
    reading: float | None = None


def read_temperature(
    frame: np.ndarray, roi: tuple[int, int, int, int] | None, infrared: bool | None = None
) -> float | None:
    """Read a Celsius value from a manually selected seven-segment display."""
    return inspect_temperature(frame, roi, infrared).reading


def inspect_temperature(
    frame: np.ndarray, roi: tuple[int, int, int, int] | None, infrared: bool | None = None
) -> TemperatureInspection:
    """Run the temperature read and keep what each stage produced.

    ``infrared`` selects the capture profile; None decides from the picture.
    """
    if frame is None or roi is None:
        return TemperatureInspection()
    height, width = frame.shape[:2]
    left, top, right, bottom = (
        int(np.clip(value, 0, limit))
        for value, limit in zip(roi, (width, height, width, height), strict=True)
    )
    if right - left < 30 or bottom - top < 20:
        return TemperatureInspection()

    display = frame[top:bottom, left:right]
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    # The manual ROI includes a little bezel for easier selection. Find the
    # bright LCD face first so its dark frame is not joined to the digits by
    # the column-based seven-segment recognizer.
    threshold = max(95, min(150, int(np.percentile(gray, 65))))
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        (gray >= threshold).astype(np.uint8), 8
    )
    lcd_boxes = []
    for index in range(1, count):
        x, y, box_width, box_height, area = stats[index]
        aspect = box_width / max(1, box_height)
        touches_roi_edge = (
            x == 0 or y == 0 or x + box_width >= gray.shape[1]
            or y + box_height >= gray.shape[0]
        )
        if (
            not touches_roi_edge
            and box_width >= 40
            and box_height >= 20
            and 1.25 <= aspect <= 4.5
        ):
            lcd_boxes.append((int(area), int(x), int(y), int(box_width), int(box_height)))
    if lcd_boxes:
        _, x, y, box_width, box_height = max(lcd_boxes)
        margin = max(1, int(min(box_width, box_height) * 0.025))
        gray = gray[
            max(0, y - margin):min(gray.shape[0], y + box_height + margin),
            max(0, x - margin):min(gray.shape[1], x + box_width + margin),
        ]
    # Otsu often selects the bright LCD background on these reflective
    # displays. Threshold relative to the face brightness, then inspect the
    # three fixed digit positions used by this thermometer layout.
    if is_infrared(display) if infrared is None else infrared:
        binary = _infrared_ink(gray)
    else:
        threshold = int(np.clip(np.percentile(gray, 40), 100, 150))
        binary = (gray < threshold).astype(np.uint8) * 255
    binary = cv2.resize(binary, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    binary = _deslant(binary)
    boxes = _digit_cell_boxes(binary)
    if boxes is None:
        return TemperatureInspection(display, gray, binary)
    cells = tuple(binary[top_:bottom_ + 1, left_:right_] for left_, top_, right_, bottom_ in boxes)
    # Never close the gap by shifting later digits left: that made 21.9 appear
    # as a different, plausible temperature. One unreadable digit fails the read.
    digits = tuple(_classify_digit(cell) for cell in cells)
    occupancy = tuple(_normalized_occupancy(cell) for cell in cells)
    reading = None
    if all(digit is not None for digit in digits):
        value = digits[0] * 10 + digits[1] + digits[2] / 10
        reading = float(value) if -10 <= value <= 60 else None
    return TemperatureInspection(display, gray, binary, tuple(boxes), cells, digits, occupancy, reading)


# In infrared the picture is blurrier, the LCD face is brighter and lit from one
# side, and the digits are faint mid-grays. Ink is whatever is clearly darker
# than the smooth local background, so a brightness gradient does not matter.
_INFRARED_INK_DELTA = 9


def _infrared_ink(gray: np.ndarray) -> np.ndarray:
    smooth = cv2.GaussianBlur(gray, (0, 0), 1.0).astype(np.float32)
    background = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), max(6, min(gray.shape) / 3))
    return (smooth < background - _INFRARED_INK_DELTA).astype(np.uint8) * 255


_CELL_CENTERS = (0.32, 0.53, 0.76)
_CELL_WIDTH = 0.16


def _digit_cell_boxes(binary: np.ndarray) -> list[tuple[int, int, int, int]] | None:
    """Return (left, top, right, bottom) for each of the three digit positions.

    Horizontal positions follow the thermometer's fixed layout. The vertical
    extent is measured from the ink itself, so a bottom bar cut by the ROI or
    a slightly different crop does not shift the segment grid.
    """
    height, width = binary.shape

    def spans_for(shift: float) -> list[tuple[int, int]]:
        return [
            (
                max(0, round((center + shift - _CELL_WIDTH / 2) * width)),
                min(width, round((center + shift + _CELL_WIDTH / 2) * width)),
            )
            for center in _CELL_CENTERS
        ]

    # The crop of the LCD face is not identical in every light, which slides the
    # digits sideways a little. Slide the three cells together to where they
    # capture the most ink, so a digit's outer bar is not cut off.
    column_ink = (binary > 0).sum(axis=0)
    shift = max(
        np.linspace(-0.05, 0.05, 21),
        key=lambda candidate: sum(int(column_ink[left:right].sum()) for left, right in spans_for(candidate)),
    )
    span = spans_for(float(shift))
    ink = binary[:, span[0][0]:span[-1][1]] > 0
    rows = ink.sum(axis=1) > ink.shape[1] * 0.02
    # Ignore the thin bezel edges at the very top and bottom of the crop.
    rows[:round(height * 0.04)] = False
    rows[round(height * 0.98):] = False
    filled = np.flatnonzero(rows)
    if filled.size < height * 0.3:
        return None
    top, bottom = int(filled[0]), int(filled[-1])
    return [(left, top, right, bottom) for left, right in span]


def _deslant(binary: np.ndarray) -> np.ndarray:
    """Straighten italic seven-segment digits so segments align with the grid.

    Picks the horizontal shear that makes the digits' vertical strokes line up
    best, measured by how concentrated the column sums become.
    """
    height = binary.shape[0]
    # The shear is searched on a small copy; shear is scale independent.
    small = cv2.resize(binary, None, fx=0.125, fy=0.125, interpolation=cv2.INTER_AREA)
    small_height = small.shape[0]
    best_shear, best_score = 0.0, -1.0
    for shear in np.linspace(-0.4, 0.4, 41):
        matrix = np.float32([[1, shear, -shear * small_height / 2], [0, 1, 0]])
        warped = cv2.warpAffine(small, matrix, (small.shape[1], small_height))
        score = float(np.square(warped.sum(axis=0, dtype=np.float64)).sum())
        if score > best_score:
            best_shear, best_score = float(shear), score
    matrix = np.float32([[1, best_shear, -best_shear * height / 2], [0, 1, 0]])
    return cv2.warpAffine(binary, matrix, (binary.shape[1], height))


# A digit is trusted only when each critical segment is clearly lit or clearly
# dark once normalized to the strongest segment. A segment is critical when
# flipping it alone would give another valid digit (3/9 differ by f, 5/6 by e,
# 0/8 by g, 1/7 by a): glare or a weakly lit segment there would otherwise turn
# into a plausible but wrong temperature, so such glyphs are reported as
# unreadable and get read again. Other segments (such as a bottom bar cropped
# by the ROI) only take part in choosing the closest pattern. Measured on the
# real display (69 digit readings), the weakest lit critical segment is the 8's lower-left
# bar (0.39, weakened by the camera angle) and the strongest dark one is the 9's
# lower-left (0.29), so the threshold sits between them at 0.34.
_PATTERNS = {frozenset(pattern) for pattern in _DIGITS.values()}
_LIT_THRESHOLD = 0.34
_MIN_SEPARATION = 0.04
_MAX_BEST_SCORE = 1.5


def _segment_occupancy(glyph: np.ndarray) -> np.ndarray | None:
    """Ink share of each segment region (a..g), or None for a degenerate glyph."""
    height, width = glyph.shape
    if height < 8 or width < 4:
        return None
    occupancy = np.empty(len(_SEGMENTS))
    for index, name in enumerate(_SEGMENTS):
        x0, y0, x1, y1 = _SEGMENTS[name]
        xa, xb = int(width * x0), max(int(width * x0) + 1, int(width * x1))
        ya, yb = int(height * y0), max(int(height * y0) + 1, int(height * y1))
        occupancy[index] = glyph[ya:yb, xa:xb].mean() / 255
    return occupancy


def _normalized_occupancy(glyph: np.ndarray) -> tuple[float, ...]:
    """Segment occupancy relative to the strongest segment (what is classified)."""
    occupancy = _segment_occupancy(glyph)
    if occupancy is None or occupancy.max() <= 0:
        return (0.0,) * len(_SEGMENTS)
    return tuple(float(value) for value in occupancy / occupancy.max())


def _classify_digit(glyph: np.ndarray) -> int | None:
    names = list(_SEGMENTS)
    occupancy = _segment_occupancy(glyph)
    if occupancy is None:
        return None
    strongest = occupancy.max()
    if strongest < 0.2:
        return None
    # Lit segments vary in brightness and a bar can be partly cropped, so
    # compare against the strongest segment instead of a fixed cutoff.
    occupancy = occupancy / strongest
    # First read the segments directly: lit above the threshold, dark below.
    # When that spells a digit exactly, trust it (an 8 whose lower bars are
    # weakened by the camera angle is still an 8). Otherwise fall back to the
    # closest pattern, which tolerates one cropped bar such as a 2's bottom.
    lit = frozenset(name for name, value in zip(names, occupancy) if value > _LIT_THRESHOLD)
    digit = next((candidate for candidate, shape in _DIGITS.items() if frozenset(shape) == lit), None)
    if digit is None:
        ranked = sorted(
            (
                float(np.square(occupancy - [1.0 if name in shape else 0.0 for name in names]).sum()),
                candidate,
            )
            for candidate, shape in _DIGITS.items()
        )
        digit = ranked[0][1]
    best = float(
        np.square(occupancy - [1.0 if name in _DIGITS[digit] else 0.0 for name in names]).sum()
    )
    if best > _MAX_BEST_SCORE:
        return None
    pattern = frozenset(_DIGITS[digit])
    for name, value in zip(names, occupancy, strict=True):
        if pattern ^ {name} not in _PATTERNS:
            continue
        separation = value - _LIT_THRESHOLD if name in pattern else _LIT_THRESHOLD - value
        if separation < _MIN_SEPARATION:
            return None
    return digit
