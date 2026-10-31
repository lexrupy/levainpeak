"""The regions marked on the picture, kept between runs.

Without an active session they lived only in the window, so closing or restarting
the app lost them. They are written here as soon as they are marked.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

REGIONS_PATH = Path("data/regions.json")


def save_regions_file(
    *, pot: tuple | None, outline: tuple | None, thermometer: tuple | None,
    face_quad: tuple | None, marker_y: int | None, path: Path = REGIONS_PATH,
) -> None:
    payload = {
        "pot": None if pot is None else list(pot),
        "outline": None if outline is None else [list(point) for point in outline],
        "thermometer": None if thermometer is None else list(thermometer),
        "face_quad": None if face_quad is None else [list(point) for point in face_quad],
        "marker_y": marker_y,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)  # never a half-written file, even if the app dies mid-save


def load_regions_file(path: Path = REGIONS_PATH) -> dict:
    """The saved regions as tuples (empty values are None); {} when there is no usable file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pairs = lambda value: tuple((int(x), int(y)) for x, y in value) if value else None  # noqa: E731
        box = lambda value: tuple(int(v) for v in value) if value else None  # noqa: E731
        marker = raw.get("marker_y")
        return {
            "pot": box(raw.get("pot")),
            "outline": pairs(raw.get("outline")),
            "thermometer": box(raw.get("thermometer")),
            "face_quad": pairs(raw.get("face_quad")),
            "marker_y": None if marker is None else int(marker),
        }
    except FileNotFoundError:
        return {}
    except (OSError, AttributeError, TypeError, ValueError):
        logging.warning("Regiões salvas inválidas em %s; ignorando", path)
        return {}
