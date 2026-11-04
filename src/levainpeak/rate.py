"""Growth rate of the levain: how fast the volume grows per hour lately, and whether that is faster or slower than the last hour."""

from __future__ import annotations

import math
from dataclasses import dataclass

WINDOW_SECONDS = 900.0  # the pace shown is that of the last 15 minutes
REFERENCE_SECONDS = 3600.0  # the arrow compares it with the pace of the last hour
FLAT_PERCENT = 3.0  # within this many points of %/h of the hour's pace, the arrow is level (short windows are noisy)
MIN_POINTS = 6
MIN_SPAN = 0.6  # the readings must cover this share of the window


@dataclass(frozen=True)
class GrowthRate:
    per_hour: float  # percent of volume gained per hour (compound), negative when falling
    trend: str | None  # "sobe", "desce", "estavel", or None without enough earlier readings

    def text(self) -> str:
        """For the phone: "+9,8%/h ↗": the pace of the last minutes, and an arrow saying whether it is above,
        below or about the pace of the last hour."""
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
    """``points`` are (seconds, volume) readings. The pace is that of the last 15 minutes up to ``now``;
    the trend compares it with the last hour."""
    current = _slope(points, now - WINDOW_SECONDS, now)
    if current is None:
        return None
    reference = _slope(points, now - REFERENCE_SECONDS, now)
    if reference is None:
        return GrowthRate(current, None)
    change = current - reference
    trend = "estavel" if abs(change) < FLAT_PERCENT else ("sobe" if change > 0 else "desce")
    return GrowthRate(current, trend)


def rate_series(
    points: list[tuple[float, float]], step: float = 120.0, window: float = WINDOW_SECONDS
) -> list[tuple[float, float]]:
    """The 15-minute pace along the session, one value every ``step`` seconds (where the readings allow one).

    ``points`` must be sorted by time. Used by the graphs: the pace falling toward zero is the peak."""
    from bisect import bisect_left, bisect_right

    if len(points) < MIN_POINTS:
        return []
    times = [t for t, _ in points]
    series: list[tuple[float, float]] = []
    moment = times[0] + window * MIN_SPAN
    while moment <= times[-1] + 1e-6:
        low, high = bisect_left(times, moment - window), bisect_right(times, moment)
        value = _slope(points[low:high], moment - window, moment)
        if value is not None:
            series.append((moment, value))
        moment += step
    return series
