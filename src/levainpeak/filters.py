"""Outlier rejection for noisy readings that are re-read before being trusted."""

from __future__ import annotations


class OutlierGate:
    """Discards readings that jump away from the trusted value.

    A reading farther than ``max_jump`` from the trusted value is rejected and
    the caller is expected to read again. The new level is adopted only after
    ``confirmations`` consecutive rejected readings agree with each other
    within ``max_jump``, so a real step change is not blocked forever.
    """

    def __init__(self, max_jump: float, confirmations: int = 4) -> None:
        if max_jump <= 0 or confirmations < 1:
            raise ValueError("max_jump must be positive and confirmations at least 1")
        self.max_jump = max_jump
        self.confirmations = confirmations
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
        if len(self._pending) >= self.confirmations:
            self.trusted = value
            self._pending.clear()
            return True
        return False
