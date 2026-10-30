"""Optional seven-segment temperature reading from a selected display region."""

from __future__ import annotations

import cv2
import numpy as np


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


def read_temperature(
    frame: np.ndarray, roi: tuple[int, int, int, int] | None
) -> float | None:
    """Read a Celsius value from a manually selected seven-segment display."""
    if frame is None or roi is None:
        return None
    height, width = frame.shape[:2]
    left, top, right, bottom = (
        int(np.clip(value, 0, limit))
        for value, limit in zip(roi, (width, height, width, height), strict=True)
    )
    if right - left < 30 or bottom - top < 20:
        return None

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
    threshold = int(np.clip(np.percentile(gray, 40), 100, 150))
    binary = (gray < threshold).astype(np.uint8) * 255
    binary = cv2.resize(binary, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    binary = _deslant(binary)
    cells = _digit_cells(binary)
    if cells is None:
        return None
    digits = []
    for cell in cells:
        digit = _classify_digit(cell)
        if digit is None:
            # Never close the gap by shifting later digits left: that made
            # 21.9 appear as a different, plausible temperature.
            return None
        digits.append(digit)

    value = digits[0] * 10 + digits[1] + digits[2] / 10
    return float(value) if -10 <= value <= 60 else None


_CELL_CENTERS = (0.32, 0.53, 0.76)
_CELL_WIDTH = 0.16


def _digit_cells(binary: np.ndarray) -> list[np.ndarray] | None:
    """Return a normalized image for each of the three digit positions.

    Horizontal positions follow the thermometer's fixed layout. The vertical
    extent is measured from the ink itself, so a bottom bar cut by the ROI or
    a slightly different crop does not shift the segment grid.
    """
    height, width = binary.shape
    span = [
        (round((center - _CELL_WIDTH / 2) * width), round((center + _CELL_WIDTH / 2) * width))
        for center in _CELL_CENTERS
    ]
    ink = binary[:, span[0][0]:span[-1][1]] > 0
    rows = ink.sum(axis=1) > ink.shape[1] * 0.02
    # Ignore the thin bezel edges at the very top and bottom of the crop.
    rows[:round(height * 0.04)] = False
    rows[round(height * 0.98):] = False
    filled = np.flatnonzero(rows)
    if filled.size < height * 0.3:
        return None
    top, bottom = int(filled[0]), int(filled[-1])
    return [binary[top:bottom + 1, left:right] for left, right in span]


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


# A digit is trusted only when its best pattern beats the runner-up clearly.
# Patterns one segment apart (3/9, 5/6, 0/8, 1/7) would otherwise turn glare or
# a weakly lit segment into a plausible but wrong temperature; those ambiguous
# glyphs are reported as unreadable so they get read again.
_MIN_MARGIN = 0.25
_MAX_BEST_SCORE = 1.5


def _classify_digit(glyph: np.ndarray) -> int | None:
    height, width = glyph.shape
    if height < 8 or width < 4:
        return None
    names = list(_SEGMENTS)
    occupancy = np.empty(len(names))
    for index, name in enumerate(names):
        x0, y0, x1, y1 = _SEGMENTS[name]
        xa, xb = int(width * x0), max(int(width * x0) + 1, int(width * x1))
        ya, yb = int(height * y0), max(int(height * y0) + 1, int(height * y1))
        occupancy[index] = glyph[ya:yb, xa:xb].mean() / 255
    strongest = occupancy.max()
    if strongest < 0.2:
        return None
    # Lit segments vary in brightness and a bar can be partly cropped, so
    # compare against the strongest segment instead of a fixed cutoff.
    occupancy = occupancy / strongest
    ranked = sorted(
        (
            float(np.square(occupancy - [1.0 if name in pattern else 0.0 for name in names]).sum()),
            digit,
        )
        for digit, pattern in _DIGITS.items()
    )
    (best, digit), (runner_up, _) = ranked[0], ranked[1]
    if best > _MAX_BEST_SCORE or runner_up - best < _MIN_MARGIN:
        return None
    return digit
