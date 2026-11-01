"""Find the "+" or "X" drawn on the pot's label: a physical mark that stays on the pot in day and infrared.

It is a thick black cross on a white label. Its center follows the pot up and down, so the
initial-level mark and the contour can be carried with it when the pot is put back.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

MIN_ARM = 40  # px, full length of a stroke in the 1080p frame
MAX_ARM = 320
DARKNESS = 0.62  # ink is darker than this fraction of the label around it


@dataclass(frozen=True)
class Plus:
    x: float
    y: float
    size: int


def find_plus(frame: np.ndarray, roi: tuple[int, int, int, int] | None) -> Plus | None:
    """The cross inside ``roi`` (frame coordinates), or None when there is none."""
    if frame is None or frame.ndim != 3 or roi is None:
        return None
    height, width = frame.shape[:2]
    left, top = max(0, int(roi[0])), max(0, int(roi[1]))
    right, bottom = min(width, int(roi[2])), min(height, int(roi[3]))
    if right - left < MIN_ARM or bottom - top < MIN_ARM:
        return None
    gray = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    smooth = cv2.GaussianBlur(gray, (0, 0), 1.5).astype(np.float32)
    around = cv2.GaussianBlur(smooth, (0, 0), 30)
    ink = ((smooth < around * DARKNESS) & (around > 90)).astype(np.uint8)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    best: tuple[float, Plus] | None = None
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if not (MIN_ARM <= w <= MAX_ARM and MIN_ARM <= h <= MAX_ARM) or not 0.6 <= w / h <= 1.6:
            continue
        fill = area / float(w * h)
        if not 0.08 <= fill <= 0.5:
            continue
        # A little thickened: a hand-drawn stroke is never perfectly straight.
        patch = cv2.dilate(ink[y:y + h, x:x + w], np.ones((7, 7), np.uint8))
        rows, columns = patch.sum(axis=1), patch.sum(axis=0)
        corners = patch[: h // 4, : w // 4].mean() + patch[-(h // 4):, -(w // 4):].mean()
        if rows.max() >= 0.85 * w and columns.max() >= 0.75 * h and corners <= 0.2:
            # A "+": one row and one column run almost the whole box, the corners are empty.
            thick = np.flatnonzero(rows >= 0.75 * rows.max())
            wide = np.flatnonzero(columns >= 0.75 * columns.max())
            center_x, center_y = float(wide.mean()), float(thick.mean())
        elif _is_x(patch):
            center_x, center_y = w / 2, h / 2
        else:
            continue
        found = Plus(left + x + center_x + 0.5, top + y + center_y + 0.5, int(max(w, h)))
        score = float(area)
        if best is None or score > best[0]:
            best = (score, found)
    return None if best is None else best[1]


def _is_x(patch: np.ndarray) -> bool:
    """Both diagonals of the box are ink, and the middle of each side is not."""
    h, w = patch.shape
    steps = np.linspace(0.12, 0.88, 24)
    down = np.mean([patch[int(t * (h - 1)), int(t * (w - 1))] for t in steps])
    up = np.mean([patch[int(t * (h - 1)), int((1 - t) * (w - 1))] for t in steps])
    sides = np.mean([patch[h // 2, : w // 8].mean(), patch[h // 2, -(w // 8):].mean(),
                     patch[: h // 8, w // 2].mean(), patch[-(h // 8):, w // 2].mean()])
    return down >= 0.8 and up >= 0.8 and sides <= 0.3
