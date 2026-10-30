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
    "a": (0.20, 0.02, 0.80, 0.24),
    "b": (0.70, 0.10, 0.98, 0.50),
    "c": (0.70, 0.50, 0.98, 0.90),
    "d": (0.20, 0.76, 0.80, 0.98),
    "e": (0.02, 0.50, 0.30, 0.90),
    "f": (0.02, 0.10, 0.30, 0.50),
    "g": (0.20, 0.39, 0.80, 0.61),
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
    digits = []
    cell_width = 0.11
    for center in (0.32, 0.53, 0.76):
        x0 = round((center - cell_width / 2) * binary.shape[1])
        x1 = round((center + cell_width / 2) * binary.shape[1])
        y0, y1 = round(binary.shape[0] * 0.08), round(binary.shape[0] * 0.95)
        digit = _classify_digit(binary[y0:y1, x0:x1])
        if digit is None:
            # Never close the gap by shifting later digits left: that made
            # 21.9 appear as a different, plausible temperature.
            return None
        digits.append(digit)

    if len(digits) != 3:
        return None
    value = digits[0] * 10 + digits[1] + digits[2] / 10
    return float(value) if -10 <= value <= 60 else None


def _classify_digit(glyph: np.ndarray) -> int | None:
    height, width = glyph.shape
    active: set[str] = set()
    for name, (x0, y0, x1, y1) in _SEGMENTS.items():
        xa, xb = int(width * x0), max(int(width * x0) + 1, int(width * x1))
        ya, yb = int(height * y0), max(int(height * y0) + 1, int(height * y1))
        occupancy = float(glyph[ya:yb, xa:xb].mean() / 255)
        # The lower-left area often contains a dark LCD icon/reflection. Keep
        # a higher cutoff there so a 9 is not mistaken for an 8.
        if occupancy > (0.24 if name == "e" else 0.18):
            active.add(name)
    ranked = sorted(
        (len(active.symmetric_difference(set(pattern))), digit)
        for digit, pattern in _DIGITS.items()
    )
    distance, digit = ranked[0]
    # Accepting a match with two missing/extra segments turns glare and LCD
    # reflections into plausible but incorrect temperatures (for example,
    # 21.8 being reported as 29.2). Prefer no reading over a confident lie.
    return digit if distance <= 1 else None
