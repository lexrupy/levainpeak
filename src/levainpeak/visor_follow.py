"""Follow the thermometer when the camera is moved: find the drawn visor area again.

The thermometer area and the face corners are drawn on the picture. When the camera is
nudged, they stay put in the picture while the thermometer goes elsewhere, and the digits
read are those of something else. A reference is taken of the edges around the area (the
body of the thermometer, not the digits, which keep changing), and later frames are searched
for it. The estimate always comes from the reference, so small errors do not add up.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from levainpeak.alignment import WORK_SCALE, _crop, _edges

MARGIN = 0.8  # of the area's size, taken around it so that the body of the thermometer is in
SEARCH = 150  # px: how far the thermometer may have gone
MIN_SCORE = 0.80  # real finds in the field scored 0.94 and up; wrong ones 0.65
MIN_TEXTURE = 0.02  # a reference with almost no edges cannot be found again


@dataclass(frozen=True)
class VisorReference:
    edges: np.ndarray  # float32, work size
    origin: tuple[int, int]  # top-left of the picture, full-size frame coordinates
    roi: tuple[int, int, int, int]
    quad: tuple[tuple[int, int], ...] | None


def make_visor_reference(frame: np.ndarray, roi, quad) -> VisorReference | None:
    if frame is None or frame.ndim != 3 or roi is None:
        return None
    height, width = frame.shape[:2]
    left, top, right, bottom = roi
    margin_x, margin_y = int((right - left) * MARGIN), int((bottom - top) * MARGIN)
    x0, y0 = max(0, left - margin_x), max(0, top - margin_y)
    x1, y1 = min(width, right + margin_x), min(height, bottom + margin_y)
    edges = _edges(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    patch = _crop(edges, x0, y0, x1, y1)
    if patch.size == 0 or float(patch.std()) < MIN_TEXTURE:
        return None
    return VisorReference(patch, (x0, y0), tuple(roi), None if quad is None else tuple(map(tuple, quad)))


def locate_visor(reference: VisorReference, frame: np.ndarray) -> tuple[float, float, float] | None:
    """(dx, dy, score): how far the reference went in this frame, in full-size pixels."""
    if frame is None or frame.ndim != 3:
        return None
    edges = _edges(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    template = reference.edges
    th, tw = template.shape
    left, top = reference.origin
    reach = int(SEARCH * WORK_SCALE)
    x0, y0 = max(0, int(left * WORK_SCALE) - reach), max(0, int(top * WORK_SCALE) - reach)
    x1 = min(edges.shape[1], int(left * WORK_SCALE) + tw + reach)
    y1 = min(edges.shape[0], int(top * WORK_SCALE) + th + reach)
    area = edges[y0:y1, x0:x1]
    if area.shape[0] <= th or area.shape[1] <= tw:
        return None
    found = np.nan_to_num(cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
    _, score, _, where = cv2.minMaxLoc(found)
    return ((x0 + where[0]) / WORK_SCALE - left, (y0 + where[1]) / WORK_SCALE - top, float(score))


def carry_visor(reference: VisorReference, dx: float, dy: float):
    """The area and the face corners, moved by the reference's shift."""
    left, top, right, bottom = reference.roi
    roi = (round(left + dx), round(top + dy), round(right + dx), round(bottom + dy))
    quad = None if reference.quad is None else tuple((round(x + dx), round(y + dy)) for x, y in reference.quad)
    return roi, quad


def _turn_point(x: float, y: float, degrees: float, width: float, height: float) -> tuple[float, float]:
    """A point after ``rotate_frame(frame, degrees)`` on a width x height picture (clockwise)."""
    turns = round(degrees / 90)
    for _ in range(turns % 4):
        x, y, width, height = height - y, x, height, width
    rest = np.radians(degrees - turns * 90)
    cx, cy = width / 2, height / 2
    dx, dy = x - cx, y - cy
    return cx + dx * np.cos(rest) - dy * np.sin(rest), cy + dx * np.sin(rest) + dy * np.cos(rest)


def rotate_visor(roi, quad, old_degrees: float, new_degrees: float, size: tuple[int, int]):
    """The area and face corners after the picture goes from one angle to another.

    ``size`` is the picture's size at ``old_degrees``. The corners turn with the picture; the
    area is the box around the turned corners (or around the turned area when there are none).
    """
    width, height = size
    old_turns = round(old_degrees / 90)
    new_turns = round(new_degrees / 90)
    old_fine, new_fine = old_degrees - old_turns * 90, new_degrees - new_turns * 90
    quarter_w, quarter_h = width, height  # the picture at the old angle, minus its small angle

    def move(x: float, y: float) -> tuple[float, float]:
        x, y = _turn_point(x, y, -old_fine, width, height)  # take the small angle off
        x, y = _turn_point(x, y, (new_turns - old_turns) * 90, quarter_w, quarter_h)
        turned_w, turned_h = (quarter_w, quarter_h) if (new_turns - old_turns) % 2 == 0 else (quarter_h, quarter_w)
        return _turn_point(x, y, new_fine, turned_w, turned_h)

    if quad is not None:
        quad = tuple((round(x), round(y)) for x, y in (move(px, py) for px, py in quad))
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        return (min(xs), min(ys), max(xs), max(ys)), quad
    left, top, right, bottom = roi
    moved = [move(x, y) for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))]
    xs, ys = [p[0] for p in moved], [p[1] for p in moved]
    return (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))), None
