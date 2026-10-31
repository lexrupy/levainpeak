"""Capture profiles: the camera looks different by day and in infrared night mode."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from levainpeak.imaging import is_infrared


@dataclass(frozen=True)
class CaptureProfile:
    name: str
    infrared: bool


DAY = CaptureProfile("dia", False)
INFRARED = CaptureProfile("infravermelho", True)


class ProfileSelector:
    """Chooses the profile from the picture, switching only after a few agreeing frames.

    Infrared frames are exactly gray, daylight frames are colorful, so the
    choice is clear-cut; the delay only keeps a stray frame or the sunrise and
    sunset transition from flipping the profile back and forth.
    """

    def __init__(self, confirm_frames: int = 5) -> None:
        self.confirm_frames = confirm_frames
        self.current: CaptureProfile | None = None
        self._candidate: CaptureProfile | None = None
        self._count = 0

    def update(self, frame: np.ndarray | None) -> CaptureProfile:
        observed = INFRARED if is_infrared(frame) else DAY
        if self.current is None:
            self.current = observed
        elif observed is self.current:
            self._candidate, self._count = None, 0
        else:
            self._count = self._count + 1 if observed is self._candidate else 1
            self._candidate = observed
            if self._count >= self.confirm_frames:
                self.current, self._candidate, self._count = observed, None, 0
        return self.current
