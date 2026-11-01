"""Outlier rejection for noisy readings that are re-read before being trusted."""

from __future__ import annotations


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
