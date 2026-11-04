"""Growth rate of the levain: how fast the volume grows per hour, and whether that pace is rising or falling."""

from __future__ import annotations

import math
from dataclasses import dataclass

WINDOW_SECONDS = 3600.0  # the pace is read over the last hour
STEP_SECONDS = 1800.0  # and compared with the hour that ended half an hour earlier
FLAT_PERCENT = 1.0  # a change of the pace smaller than this (points of %/h) is "steady"
MIN_POINTS = 6
MIN_SPAN = 0.6  # the readings must cover this share of the window


@dataclass(frozen=True)
class GrowthRate:
    per_hour: float  # percent of volume gained per hour (compound), negative when falling
    trend: str | None  # "sobe", "desce", "estavel", or None without enough earlier readings

    def text(self) -> str:
        """For the phone: "+9,8%/h ↗" (the arrow shows whether the pace itself is rising or falling)."""
        arrow = {"sobe": " ↗", "desce": " ↘", "estavel": " →"}.get(self.trend, "")
        return f"{self.per_hour:+.1f}".replace(".", ",") + "%/h" + arrow


def _slope(points: list[tuple[float, float]], start: float, end: float) -> float | None:
    """Percent per hour of the exponential fit of volume over [start, end], or None if the readings are too few."""
    chosen = [(t, math.log(v)) for t, v in points if start <= t <= end and v > 0]
    if len(chosen) < MIN_POINTS or chosen[-1][0] - chosen[0][0] < MIN_SPAN * (end - start):
        return None
    mean_t = sum(t for t, _ in chosen) / len(chosen)
    mean_y = sum(y for _, y in chosen) / len(chosen)
    spread = sum((t - mean_t) ** 2 for t, _ in chosen)
    if spread <= 0:
        return None
    per_second = sum((t - mean_t) * (y - mean_y) for t, y in chosen) / spread
    return (math.exp(per_second * 3600.0) - 1.0) * 100.0


def growth_rate(points: list[tuple[float, float]], now: float) -> GrowthRate | None:
    """``points`` are (seconds, volume) readings; the pace is that of the last hour up to ``now``."""
    current = _slope(points, now - WINDOW_SECONDS, now)
    if current is None:
        return None
    before = _slope(points, now - WINDOW_SECONDS - STEP_SECONDS, now - STEP_SECONDS)
    if before is None:
        return GrowthRate(current, None)
    change = current - before
    trend = "estavel" if abs(change) < FLAT_PERCENT else ("sobe" if change > 0 else "desce")
    return GrowthRate(current, trend)
