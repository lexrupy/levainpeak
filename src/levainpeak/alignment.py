"""Follow the pot when it is moved: find the drawn contour again from a picture of the pot.

The contour, the search box and the initial-level mark are drawn once. When the pot is
lifted out and put back, or slides a little, they no longer lie on it. A reference is
taken when they are drawn: the edges of the pot and what stands around it (lid, rim,
base), without the inside, where the levain keeps changing. Later frames are searched
for that reference at a few sizes, and the drawing is carried to where it was found,
larger or smaller when the pot came closer to the camera or went away from it.

Every estimate is made from the reference, never from the last estimate, so small
errors do not add up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

REFERENCE_DIR = Path("data")
SCALES = (0.90, 0.93, 0.96, 0.98, 1.0, 1.02, 1.05, 1.08, 1.11)
WORK_SCALE = 0.5  # the search runs on a half-size picture
MARGIN = 0.12  # of the contour's size, taken around it (lid, rim and base are in here)
SEARCH = 0.30  # how far the pot may have gone, as a fraction of the contour's size
INSIDE = 0.78  # the part of the contour left out of the reference: the levain's own area
MIN_SCORE = 0.70  # a frame from the other camera profile, or a hand over the pot, scores about 0.5


@dataclass(frozen=True)
class PotReference:
    """Edges around the pot as they were when the contour was drawn."""

    edges: np.ndarray  # float32, work size, inside blanked
    mask: np.ndarray  # uint8, 0 where the inside of the pot was blanked
    origin: tuple[int, int]  # top-left of the picture, full-size frame coordinates
    outline: tuple[tuple[int, int], ...]
    roi: tuple[int, int, int, int] | None
    marker_y: int | None
    plus: tuple[float, float] | None = None  # the "+" on the label when this was taken (day only)


@dataclass(frozen=True)
class Alignment:
    scale: float
    dx: float  # where the reference's origin went, full-size frame coordinates
    dy: float
    score: float


def _edges(gray: np.ndarray) -> np.ndarray:
    """Edge strength, scaled so that a dim infrared frame and a bright day frame compare."""
    small = cv2.resize(gray, None, fx=WORK_SCALE, fy=WORK_SCALE, interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 1.6).astype(np.float32)
    strength = np.hypot(cv2.Sobel(small, cv2.CV_32F, 1, 0), cv2.Sobel(small, cv2.CV_32F, 0, 1))
    top = float(np.percentile(strength, 99)) or 1.0
    return np.clip(strength / top, 0.0, 1.0)


def make_reference(
    frame: np.ndarray,
    outline: tuple[tuple[int, int], ...],
    roi: tuple[int, int, int, int] | None,
    marker_y: int | None,
    plus: tuple[float, float] | None = None,
) -> PotReference | None:
    if frame is None or frame.ndim != 3 or len(outline) < 3:
        return None
    height, width = frame.shape[:2]
    points = np.asarray(outline, np.float32)
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    margin = MARGIN * max(x1 - x0, y1 - y0)
    left, top = int(max(0, x0 - margin)), int(max(0, y0 - margin))
    right, bottom = int(min(width, x1 + margin)), int(min(height, y1 + margin))
    if right - left < 60 or bottom - top < 60:
        return None
    edges = _edges(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    crop = _crop(edges, left, top, right, bottom)
    center = points.mean(axis=0)
    inside = (center + (points - center) * INSIDE) * WORK_SCALE - np.array([left, top]) * WORK_SCALE
    mask = np.full(crop.shape, 255, np.uint8)
    cv2.fillPoly(mask, [np.round(inside).astype(np.int32)], 0)
    return PotReference(
        edges=crop * (mask > 0), mask=mask, origin=(left, top),
        outline=tuple((int(x), int(y)) for x, y in outline), roi=roi, marker_y=marker_y, plus=plus,
    )


def _crop(edges: np.ndarray, left: int, top: int, right: int, bottom: int) -> np.ndarray:
    return np.ascontiguousarray(
        edges[int(top * WORK_SCALE):int(bottom * WORK_SCALE), int(left * WORK_SCALE):int(right * WORK_SCALE)]
    )


def locate(reference: PotReference, frame: np.ndarray) -> Alignment | None:
    """Where the reference is in this frame, or None when nothing resembles it."""
    if frame is None or frame.ndim != 3:
        return None
    edges = _edges(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    template, mask = reference.edges, reference.mask
    th, tw = template.shape
    left, top = reference.origin
    reach_x, reach_y = SEARCH * tw / WORK_SCALE, SEARCH * th / WORK_SCALE
    best: Alignment | None = None
    for scale in SCALES:
        scaled = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        scaled_mask = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        # Where the scaled picture would sit if the pot's center stayed put, plus the reach.
        shift_x = (tw / WORK_SCALE) * (1 - scale) / 2
        shift_y = (th / WORK_SCALE) * (1 - scale) / 2
        search_left = int(max(0, (left + shift_x - reach_x) * WORK_SCALE))
        search_top = int(max(0, (top + shift_y - reach_y) * WORK_SCALE))
        search_right = int(min(edges.shape[1], (left + shift_x + reach_x) * WORK_SCALE + scaled.shape[1]))
        search_bottom = int(min(edges.shape[0], (top + shift_y + reach_y) * WORK_SCALE + scaled.shape[0]))
        area = edges[search_top:search_bottom, search_left:search_right]
        if area.shape[0] <= scaled.shape[0] or area.shape[1] <= scaled.shape[1]:
            continue
        found = cv2.matchTemplate(area, scaled, cv2.TM_CCORR_NORMED, mask=scaled_mask)
        found = np.nan_to_num(found, nan=0.0, posinf=0.0, neginf=0.0)
        _, score, _, where = cv2.minMaxLoc(found)
        if best is None or score > best.score:
            best = Alignment(
                scale=scale,
                dx=(search_left + where[0]) / WORK_SCALE,
                dy=(search_top + where[1]) / WORK_SCALE,
                score=float(score),
            )
    return best


def carry(reference: PotReference, alignment: Alignment) -> tuple[tuple[tuple[int, int], ...], tuple[int, int, int, int] | None, int | None]:
    """The drawn contour, search box and mark moved to where the pot is now."""
    origin_x, origin_y = reference.origin

    def move(x: float, y: float) -> tuple[int, int]:
        return (
            int(round(alignment.dx + (x - origin_x) * alignment.scale)),
            int(round(alignment.dy + (y - origin_y) * alignment.scale)),
        )

    outline = tuple(move(x, y) for x, y in reference.outline)
    roi = None
    if reference.roi is not None:
        left, top, right, bottom = reference.roi
        (a, b), (c, d) = move(left, top), move(right, bottom)
        roi = (a, b, c, d)
    marker = None if reference.marker_y is None else move(origin_x, reference.marker_y)[1]
    return outline, roi, marker


def reference_path(profile: str, directory: Path = REFERENCE_DIR) -> Path:
    return directory / f"pot_reference_{profile}.npz"


def save_reference(reference: PotReference, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.npz")
    np.savez_compressed(
        temporary, edges=reference.edges, mask=reference.mask,
        origin=np.asarray(reference.origin), outline=np.asarray(reference.outline),
        roi=np.asarray(reference.roi if reference.roi is not None else (-1, -1, -1, -1)),
        marker_y=np.asarray(-1 if reference.marker_y is None else reference.marker_y),
        plus=np.asarray(reference.plus if reference.plus is not None else (-1.0, -1.0)),
    )
    temporary.replace(path)


def load_reference(path: Path) -> PotReference | None:
    try:
        with np.load(path) as saved:
            roi = tuple(int(v) for v in saved["roi"])
            marker = int(saved["marker_y"])
            plus = tuple(float(v) for v in saved["plus"]) if "plus" in saved.files else (-1.0, -1.0)
            return PotReference(
                edges=saved["edges"], mask=saved["mask"],
                origin=(int(saved["origin"][0]), int(saved["origin"][1])),
                outline=tuple((int(x), int(y)) for x, y in saved["outline"]),
                roi=None if roi[0] < 0 else roi, marker_y=None if marker < 0 else marker,
                plus=None if plus[0] < 0 else plus,
            )
    except (OSError, ValueError, KeyError):
        return None
