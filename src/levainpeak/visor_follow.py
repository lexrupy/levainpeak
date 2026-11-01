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
