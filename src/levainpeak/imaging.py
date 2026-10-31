"""Small image helpers shared by the detectors."""

from __future__ import annotations

import numpy as np

_INFRARED_SATURATION = 3.0


def is_infrared(image: np.ndarray | None) -> bool:
    """True for the grayscale picture the camera sends in infrared night mode."""
    if image is None or image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
        return False
    sample = image[:: max(1, image.shape[0] // 64), :: max(1, image.shape[1] // 64)].astype(np.int16)
    spread = sample.max(axis=2) - sample.min(axis=2)
    return float(spread.mean()) < _INFRARED_SATURATION
