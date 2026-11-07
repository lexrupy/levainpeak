"""The levain surface read in many narrow strips across the whole glass, which then vote.

Every strip (3 px wide by default) finds its own surface, between rows. A step between neighbouring
strips splits the profile into stretches; a label corner or a smear is a narrow, higher stretch, and
the reference is the lowest stretch that enough of the glass supports. A smooth arch (centre higher
than the sides, no step) is a dome, and the level is then the mean of the wall and the dome.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from levainpeak.vision import _surface_in_columns

STRIP_WIDTH = 3  # px
STEP_FRACTION = 0.008  # of the glass's height: a jump between neighbours bigger than this is a step
MIN_SUPPORT = 0.25  # of the strips: a stretch needs this share of them to be the reference
WALL_MARGIN = 0.05  # of the width, left out on each side: the wall refracts and the foam climbs it
MIN_CONFIDENCE = 0.25  # below this share of agreeing strips the reading is not used
DOME_RISE = 2.0  # px the middle must stand above the sides for the arch to count as a dome


@dataclass(frozen=True)
class StripReading:
    xs: tuple[float, ...]  # centre of every strip
    ys: tuple[float | None, ...]  # its surface row, or None when it found none
    kept: tuple[bool, ...]  # in the stretch that gave the level
    level: float | None  # the surface row, or None when the strips do not agree
    confidence: float  # share of the strips that gave it
    dome: bool
    step: float  # px: the jump that split the stretches


def read_strips(
    gray: np.ndarray, left: int, right: int, top: int, bottom: int, infrared: bool = False,
    *, strip_width: int = STRIP_WIDTH, step_fraction: float = STEP_FRACTION,
    min_support: float = MIN_SUPPORT, wall_margin: float = WALL_MARGIN,
    bands: tuple[tuple[float, float], ...] = (),
) -> StripReading:
    width = right - left
    start, end = left + int(round(wall_margin * width)), right - int(round(wall_margin * width))
    xs, ys = [], []
    for x0 in range(start, end - strip_width + 1, strip_width):
        fraction = (x0 + strip_width / 2 - left) / width
        if bands and not any(a <= fraction <= b for a, b in bands):
            continue  # outside the strips the user drew: left out of the vote
        found = _surface_in_columns(gray, x0, x0 + strip_width, top, bottom, infrared, width, fine=True)
        xs.append(x0 + strip_width / 2)
        ys.append(None if found is None else found[0])
    step = max(2.0, step_fraction * (bottom - top))
    empty = StripReading(tuple(xs), tuple(ys), (False,) * len(ys), None, 0.0, False, step)
    valid = [i for i, y in enumerate(ys) if y is not None]
    if len(valid) < 8:
        return empty
    if bands:
        # Drawn bands are far apart, so neighbours mean nothing: the strips are grouped by height, and the
        # lowest group enough of them agree on is the surface.
        need = max(3, int(np.ceil(len(valid) * min_support)))
        chosen = None
        for candidate in sorted(valid, key=lambda i: ys[i], reverse=True):
            group = [i for i in valid if ys[candidate] - step <= ys[i] <= ys[candidate] + step / 2]
            if len(group) >= need:
                chosen = group
                break
        if chosen is None:
            return empty
        level = float(np.mean([ys[i] for i in chosen]))
        kept = tuple(i in set(chosen) for i in range(len(ys)))
        return StripReading(tuple(xs), tuple(ys), kept, level, len(chosen) / len(valid), False, step)
    # Stretches of neighbours with no step between them (a run of strips that found nothing ends one).
    stretches, current = [], [valid[0]]
    for before, index in zip(valid, valid[1:]):
        if abs(ys[index] - ys[before]) > step or index - before > 3:
            stretches.append(current)
            current = []
        current.append(index)
    stretches.append(current)
    need = max(5, int(np.ceil(len(valid) * min_support)))
    supported = [stretch for stretch in stretches if len(stretch) >= need]
    if not supported:
        return empty
    chosen = max(supported, key=lambda stretch: float(np.median([ys[i] for i in stretch])))  # the lowest
    values = np.asarray([ys[i] for i in chosen])
    dome = False
    level = float(np.mean(values))
    if len(chosen) >= 12:
        tail = max(2, len(chosen) // 6)
        sides = float((values[:tail].mean() + values[-tail:].mean()) / 2)
        middle = values[len(chosen) // 2 - tail // 2: len(chosen) // 2 + tail - tail // 2]
        top_of_dome = float(middle.mean())
        if sides - top_of_dome >= DOME_RISE and values.min() >= top_of_dome - step:  # an arch: the middle is the highest
            dome = True
            level = (sides + top_of_dome) / 2
    kept = tuple(i in set(chosen) for i in range(len(ys)))
    return StripReading(tuple(xs), tuple(ys), kept, level, len(chosen) / len(valid), dome, step)
