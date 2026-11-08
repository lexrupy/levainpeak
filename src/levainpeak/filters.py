"""Outlier rejection for noisy readings that are re-read before being trusted."""

from __future__ import annotations

import numpy as np


class OutlierGate:
    """Discards readings that jump away from the trusted value.

    A reading farther than ``max_jump`` from the trusted value is rejected and
    the caller is expected to read again. The new level is adopted only after
    ``confirmations`` consecutive rejected readings agree with each other
    within ``max_jump``, so a real step change is not blocked forever.
    """

    def __init__(
        self, max_jump: float, confirmations: int = 4,
        big_jump: float | None = None, big_confirmations: int | None = None,
    ) -> None:
        if max_jump <= 0 or confirmations < 1:
            raise ValueError("max_jump must be positive and confirmations at least 1")
        self.max_jump = max_jump
        self.confirmations = confirmations
        # A jump past ``big_jump`` is far likelier a misread digit than a real change:
        # it must hold for ``big_confirmations`` readings before it is adopted.
        self.big_jump = big_jump
        self.big_confirmations = big_confirmations or confirmations
        self.trusted: float | None = None
        self._pending: list[float] = []

    def reset(self) -> None:
        self.trusted = None
        self._pending.clear()

    def accept(self, value: float) -> bool:
        """Return True when ``value`` is trusted, False when it was discarded."""
        if self.trusted is None or abs(value - self.trusted) <= self.max_jump:
            self.trusted = value
            self._pending.clear()
            return True
        if self._pending and abs(value - self._pending[-1]) > self.max_jump:
            self._pending.clear()
        self._pending.append(value)
        needed = self.confirmations
        if self.big_jump is not None and abs(value - self.trusted) > self.big_jump:
            needed = self.big_confirmations
        if len(self._pending) >= needed:
            self.trusted = value
            self._pending.clear()
            return True
        return False


class LampWatch:
    """Notices the lamp: a sudden change in how bright the whole picture is.

    The reference is the median brightness a little before; a reading that departs from it by more
    than ``jump`` (a fraction) starts a hold of ``hold_seconds``, and every further change extends it
    (the lamp going off is another change). ``update`` tells whether the picture is held now.

    A hold never lasts past ``max_factor`` times ``hold_seconds`` from its start: past that the
    brightness it was held against is the new normal and the reference starts over, so a light that
    keeps changing cannot freeze the volume for hours.
    """

    def __init__(
        self, jump: float = 0.12, window: float = 90.0, settle: float = 10.0, max_factor: float = 3.0
    ) -> None:
        self.jump, self.window, self.settle, self.max_factor = jump, window, settle, max_factor
        self._samples: list[tuple[float, float]] = []
        self.held_until = float("-inf")
        self._held_since: float | None = None
        self.reference: float | None = None  # the brightness the last hold was held against

    def stayed_changed(self, brightness: float) -> bool:
        """After a hold: the picture is still as different from before it as when it began (a lamp moved,
        not one that went off and on again)."""
        return bool(self.reference) and abs(brightness - self.reference) / self.reference > self.jump

    def reset(self) -> None:
        self._samples.clear()
        self.held_until = float("-inf")
        self._held_since = None

    def update(self, now: float, brightness: float, hold_seconds: float) -> bool:
        from statistics import median

        if self._held_since is not None and now - self._held_since > self.max_factor * hold_seconds:
            self._samples.clear()  # held long enough: what the picture shows now is the reference
            self.held_until = float("-inf")
            self._held_since = None
        self._samples = [item for item in self._samples if now - item[0] <= self.window]
        before = [value for stamp, value in self._samples if now - stamp >= self.settle]
        if hold_seconds > 0 and len(before) >= 3:
            reference = median(before)
            if reference > 1 and abs(brightness - reference) / reference > self.jump:
                if self._held_since is None or now >= self.held_until:
                    self._held_since = now
                    self.reference = reference
                self.held_until = max(self.held_until, now + hold_seconds)
        self._samples.append((now, brightness))
        if now >= self.held_until:
            self._held_since = None
        return hold_seconds > 0 and now < self.held_until


def despike(
    values: list[float],
    *,
    window: int = 80,
    threshold: float = 0.02,
    minimum_side: int = 12,
) -> list[float]:
    """Replace short excursions that leave the trend and come back.

    A point is a spike when it lies beyond ``threshold`` from the median of
    the samples before it *and* from the median of the samples after it, on
    the same side. A lasting step matches one side and is kept, and so is a
    ramp. A dip shorter than about half of ``window`` is replaced by the mean
    of the two medians. The first and last ``minimum_side`` points cannot be
    judged from both sides and are left as they are.
    """
    from statistics import median

    cleaned = list(values)
    for index, value in enumerate(values):
        before = values[max(0, index - window):index]
        after = values[index + 1:index + 1 + window]
        if len(before) < minimum_side or len(after) < minimum_side:
            continue
        median_before, median_after = median(before), median(after)
        below = value < median_before - threshold and value < median_after - threshold
        above = value > median_before + threshold and value > median_after + threshold
        if below or above:
            cleaned[index] = (median_before + median_after) / 2
    return cleaned


def valid_after_recovery(values: list[float], tolerance: float = 0.02) -> list[bool]:
    """Flag readings that a later recovery proves were not real.

    Levain volume does not dip and come back. When a reading is below a level
    reached earlier in the session and a later reading rises above it again,
    that low reading (and every other one under that level in between) is
    invalid. A fall that never recovers stays valid, and so do the newest
    readings until a later one overrules them. ``tolerance`` keeps ordinary
    measurement noise from being treated as a dip.
    """
    from statistics import median

    count = len(values)
    # Levels come from a short running median, so an isolated high spike never
    # becomes the "level reached earlier" that later readings fall short of.
    levels = [median(values[max(0, index - 3):index + 4]) for index in range(count)]
    before_peak = [float("-inf")] * count
    running = float("-inf")
    for index in range(count):
        before_peak[index] = running
        running = max(running, levels[index])
    after_peak = [float("-inf")] * count
    running = float("-inf")
    for index in range(count - 1, -1, -1):
        after_peak[index] = running
        running = max(running, levels[index])
    return [
        not (
            value < before_peak[index] - tolerance
            and value < after_peak[index] - tolerance
        )
        for index, value in enumerate(values)
    ]


def bridge_holds(
    times: list[float], values: list[float], *, min_seconds: float = 180.0, min_step: float = 0.01,
) -> list[float]:
    """Replace a stretch of identical readings that ends in a step by a straight line to that step.

    The lamp holds the level while it changes the picture: the readings stop dead for minutes and then
    jump to wherever the levain has got to. A rising levain does not stand still and then leap, so the
    stretch is drawn as the line from the last reading before it to the first one after it. Short repeats
    (the same reading twice) and stretches that end flat are left alone.
    """
    cleaned = list(values)
    start = 0
    count = len(values)
    while start < count:
        end = start
        while end + 1 < count and values[end + 1] == values[start]:
            end += 1
        after = end + 1
        if (
            start > 0 and after < count and times[end] - times[start] >= min_seconds
            and abs(values[after] - values[start]) >= min_step
        ):
            first, last = start - 1, after
            span = times[last] - times[first]
            # The line ends at the median of the next readings, not at one that may be a overshoot.
            target = sorted(values[after:after + 6])[len(values[after:after + 6]) // 2]
            for index in range(start, after):
                share = (times[index] - times[first]) / span if span > 0 else 1.0
                cleaned[index] = values[first] + share * (target - values[first])
        start = end + 1
    return cleaned


def bridge_bumps(
    times: list[float], values: list[float], *, max_seconds: float = 720.0,
    min_jump: float = 0.012, min_return: float = 0.008,
) -> list[float]:
    """Replace a step away from the trend and a step back, close together, by the line between the two ends.

    After the lamp the picture's brightness shifts and the level reads a little up (or down) for some
    minutes, then goes back: a step of at least ``min_jump`` between two readings, answered within
    ``max_seconds`` by an opposite step of at least ``min_return`` and half the first. Growth does not do that. The
    readings between are drawn as the line from the one before the first step to the one after the
    second. The ends of the series cannot be judged and are left as they are.
    """
    cleaned = list(values)
    count = len(values)
    index = 1
    while index < count:
        jump = values[index] - values[index - 1]
        if abs(jump) < min_jump:
            index += 1
            continue
        back = None
        for later in range(index + 1, count):
            if times[later] - times[index] > max_seconds:
                break
            step = values[later] - values[later - 1]
            if step * jump < 0 and abs(step) >= max(min_return, 0.5 * abs(jump)):
                back = later
                break
            if step * jump > 0 and abs(step) >= min_jump:
                break  # it went on in the same direction: a real step, not a bump
        if back is None:
            index += 1
            continue
        first, last = index - 1, back
        span = times[last] - times[first]
        for inside in range(index, back):
            share = (times[inside] - times[first]) / span if span > 0 else 1.0
            cleaned[inside] = values[first] + share * (values[last] - values[first])
        index = back + 1
    return cleaned


def enforce_trend(values: list[float], times: list[float] | None = None, *, smooth: int = 15) -> list[float]:
    """Rise up to the peak, fall after it: nothing in between goes back, and the fix is a straight line.

    A starter does not sink while it is still growing (falling below an earlier level is a misread or a
    mispositioned frame) and a real fall is never followed by a new rise. So up to the highest point a
    reading is good only if it reaches a new high, and after that point only if it reaches a new low. The
    others are drawn on the line between the good readings around them (the behaviour is linear), by
    ``times`` when given, else by position. The peak is found on a median-smoothed copy so one stray
    reading does not decide it.
    """
    from statistics import median

    count = len(values)
    if count < 3:
        return list(values)
    times = list(range(count)) if times is None else times
    half = max(1, smooth // 2)
    smoothed = [median(values[max(0, i - half):i + half + 1]) for i in range(count)]
    peak = max(range(count), key=lambda i: (smoothed[i], -i))
    good = [0]
    for index in range(1, peak + 1):
        if values[index] >= values[good[-1]]:
            good.append(index)
    for index in range(peak + 1, count):
        if values[index] <= values[good[-1]]:
            good.append(index)
    good = sorted(set(good))
    return [float(v) for v in np.interp(times, [times[i] for i in good], [values[i] for i in good])]
