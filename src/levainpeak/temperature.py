"""Optional seven-segment temperature reading from a selected display region."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from levainpeak.imaging import is_infrared


_DIGITS = {
    0: "abcdef",
    1: "bc",
    2: "abged",
    3: "abgcd",
    4: "fgbc",
    5: "afgcd",
    6: "afgecd",
    7: "abc",
    8: "abcdefg",
    9: "abfgcd",
}
# This thermometer draws its 7 with the upper-left bar lit as well (a, b, c, f);
# the plain three-bar 7 stays accepted.
_SHAPES = [*_DIGITS.items(), (7, "abcf")]
_SEGMENTS = {
    "a": (0.32, 0.02, 0.68, 0.24),
    "b": (0.70, 0.10, 0.98, 0.50),
    "c": (0.70, 0.50, 0.98, 0.90),
    "d": (0.32, 0.76, 0.68, 0.98),
    "e": (0.02, 0.50, 0.30, 0.90),
    "f": (0.02, 0.10, 0.30, 0.50),
    "g": (0.32, 0.39, 0.68, 0.61),
}


@dataclass(frozen=True)
class TemperatureInspection:
    """Every stage of one temperature read, for the diagnostics window."""

    roi_image: np.ndarray | None = None  # BGR crop of the selected region
    face: np.ndarray | None = None  # grayscale LCD face
    binary: np.ndarray | None = None  # deslanted ink mask (ink = 255)
    cell_boxes: tuple[tuple[int, int, int, int], ...] = ()  # left, top, right, bottom
    cells: tuple[np.ndarray, ...] = ()
    digits: tuple[int | None, ...] = ()
    occupancy: tuple[tuple[float, ...], ...] = ()  # per cell, segments a..g, 0..1
    reading: float | None = None


def read_temperature(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] | None,
    infrared: bool | None = None,
    face_quad: tuple[tuple[int, int], ...] | None = None,
) -> float | None:
    """Read a Celsius value from a manually selected seven-segment display."""
    return inspect_temperature(frame, roi, infrared, face_quad).reading


def rectify_face(frame: np.ndarray, quad) -> np.ndarray | None:
    """The LCD face seen straight on: the user's four corners (top-left, top-right,
    bottom-right, bottom-left, frame pixels) warped to a rectangle.

    It removes tilt and perspective, and it is the face itself, so no search for
    the face is needed (that search fails when the glass is as dark as its frame).
    """
    corners = np.asarray(quad, dtype=np.float32)
    if corners.shape != (4, 2) or frame is None:
        return None
    width = max(float(np.hypot(*(corners[1] - corners[0]))), float(np.hypot(*(corners[2] - corners[3]))))
    height = max(float(np.hypot(*(corners[3] - corners[0]))), float(np.hypot(*(corners[2] - corners[1]))))
    if width < 30 or height < 15:
        return None
    size = (int(round(width)), int(round(height)))
    target = np.float32([(0, 0), (size[0], 0), (size[0], size[1]), (0, size[1])])
    matrix = cv2.getPerspectiveTransform(corners, target)
    return cv2.warpPerspective(frame, matrix, size, flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def inspect_temperature(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] | None,
    infrared: bool | None = None,
    face_quad: tuple[tuple[int, int], ...] | None = None,
) -> TemperatureInspection:
    """Run the temperature read and keep what each stage produced.

    ``infrared`` selects the capture profile; None decides from the picture.
    ``face_quad`` is the LCD face drawn by the user; it replaces the search for
    the face, the straightening and the cropping.
    """
    if face_quad is not None and frame is not None:
        face = rectify_face(frame, face_quad)
        if face is None:
            return TemperatureInspection()
        use_infrared = is_infrared(face) if infrared is None else infrared
        return _inspect_face(face, cv2.cvtColor(face, cv2.COLOR_BGR2GRAY), use_infrared, drawn=True)
    if frame is None or roi is None:
        return TemperatureInspection()
    height, width = frame.shape[:2]
    left, top, right, bottom = (
        int(np.clip(value, 0, limit))
        for value, limit in zip(roi, (width, height, width, height), strict=True)
    )
    if right - left < 30 or bottom - top < 20:
        return TemperatureInspection()

    display = _straighten(frame[top:bottom, left:right])
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    use_infrared = is_infrared(display) if infrared is None else infrared
    # The manual ROI includes a little bezel for easier selection. Find the
    # bright LCD face first so its dark frame is not joined to the digits by
    # the column-based seven-segment recognizer. How bright "bright" is depends
    # on how much dark surroundings the ROI takes in, so several thresholds are
    # tried, the usual one first, and the first face that reads is kept.
    first: TemperatureInspection | None = None
    votes: dict[float, list[TemperatureInspection]] = {}
    for index, face in enumerate(_face_candidates(gray)):
        result = _inspect_face(display, face, use_infrared)
        first = first or result
        if result.reading is None:
            continue
        if index == 0:
            return result  # the usual threshold found the face and read it
        # A face found with another threshold must be confirmed by a second one:
        # one odd rectangle can still spell three valid digits by chance.
        votes.setdefault(result.reading, []).append(result)
        if len(votes[result.reading]) >= 2:
            return votes[result.reading][0]
    return first or TemperatureInspection(display, gray)


def _bezel_tilt(gray: np.ndarray) -> float | None:
    """Tilt in degrees (-45..45) of the display's dark frame, or None when there is no clear frame."""
    reference = float(np.percentile(gray, 95))
    dark = (cv2.GaussianBlur(gray, (0, 0), 2) < 0.4 * reference).astype(np.uint8)
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    (_, _), (box_width, box_height), angle = cv2.minAreaRect(biggest)
    area = box_width * box_height
    if area < 0.2 * gray.size or cv2.contourArea(biggest) < 0.7 * area:
        return None  # not a frame-like rectangle
    return ((angle + 45.0) % 90.0) - 45.0


def _straighten(display: np.ndarray) -> np.ndarray:
    """Undo a tilt of the display, which the digit grid (fixed positions) cannot follow.

    A thermometer stuck on a wall is rarely level. The tilt comes from the dark
    frame; only 1.5 to 25 degrees are corrected, and the turn is kept only if it
    does make the frame straighter.
    """
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    tilt = _bezel_tilt(gray)
    if tilt is None or not 1.5 <= abs(tilt) <= 25:
        return display
    height, width = display.shape[:2]
    best, best_residual = display, abs(tilt)
    for angle in (tilt, -tilt):
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        turned = cv2.warpAffine(display, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        residual = _bezel_tilt(cv2.cvtColor(turned, cv2.COLOR_BGR2GRAY))
        if residual is not None and abs(residual) < best_residual:
            best, best_residual = turned, abs(residual)
    return best


def _lcd_face(gray: np.ndarray, threshold: int) -> np.ndarray | None:
    """The bright LCD rectangle at ``threshold``, or None when there is none."""
    count, _, stats, _ = cv2.connectedComponentsWithStats((gray >= threshold).astype(np.uint8), 8)
    lcd_boxes = []
    for index in range(1, count):
        x, y, box_width, box_height, area = stats[index]
        aspect = box_width / max(1, box_height)
        touches_roi_edge = (
            x == 0 or y == 0 or x + box_width >= gray.shape[1]
            or y + box_height >= gray.shape[0]
        )
        if (
            not touches_roi_edge
            and box_width >= 40
            and box_height >= 20
            and 1.25 <= aspect <= 4.5
        ):
            lcd_boxes.append((int(area), int(x), int(y), int(box_width), int(box_height)))
    if not lcd_boxes:
        return None
    _, x, y, box_width, box_height = max(lcd_boxes)
    margin = max(1, int(min(box_width, box_height) * 0.025))
    return gray[
        max(0, y - margin):min(gray.shape[0], y + box_height + margin),
        max(0, x - margin):min(gray.shape[1], x + box_width + margin),
    ]


def _trim_dark_border(face: np.ndarray) -> np.ndarray:
    """Cut away the bezel: outer rows and columns much darker than the LCD face."""
    height, width = face.shape
    if height < 12 or width < 12:
        return face
    reference = float(np.percentile(face[height // 4:3 * height // 4, width // 4:3 * width // 4], 75))
    bright_rows = np.flatnonzero(face.mean(axis=1) >= 0.65 * reference)
    bright_columns = np.flatnonzero(face.mean(axis=0) >= 0.65 * reference)
    if bright_rows.size < 12 or bright_columns.size < 12:
        return face
    return face[bright_rows[0]:bright_rows[-1] + 1, bright_columns[0]:bright_columns[-1] + 1]


def _face_candidates(gray: np.ndarray) -> list[np.ndarray]:
    """Possible LCD faces, from the usual threshold first, without duplicates."""
    bright = float(np.percentile(gray, 98))
    thresholds = [
        _face_threshold(gray),
        int(0.65 * bright),
        int(0.75 * bright),
        int(cv2.threshold(cv2.GaussianBlur(gray, (0, 0), 1.5), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]),
        int((float(np.percentile(gray, 50)) + bright) / 2),
    ]
    faces: list[np.ndarray] = []
    seen: set[tuple[int, int, int]] = set()

    def add(face: np.ndarray) -> None:
        key = (face.shape[0], face.shape[1], int(face.sum()) % 100003)
        if key not in seen:
            seen.add(key)
            faces.append(face)

    for threshold in dict.fromkeys(thresholds):
        face = _lcd_face(gray, threshold)
        if face is None:
            if not faces and threshold == thresholds[0]:
                add(gray)  # nothing rectangular found: use the whole region
            continue
        # Trimmed first: it removes the bezel a loose threshold drags in. The
        # untrimmed face stays as an option because in infrared the face fades
        # toward one side and trimming can cut into the digits there.
        add(_trim_dark_border(face))
        add(face)
    return faces or [gray]


def _inspect_face(
    display: np.ndarray, gray: np.ndarray, infrared: bool, drawn: bool = False
) -> TemperatureInspection:
    """Binarize the LCD face, locate the three digits and read them."""
    # Otsu often selects the bright LCD background on these reflective
    # displays. Threshold relative to the face brightness, then inspect the
    # three fixed digit positions used by this thermometer layout.
    if infrared and drawn:
        binary = _flat_field_ink(gray)
    elif infrared:
        binary = _infrared_ink(gray)
    else:
        threshold = int(np.clip(np.percentile(gray, 40), 100, 150))
        binary = (gray < threshold).astype(np.uint8) * 255
    binary = _drop_edge_ink(binary)
    binary = cv2.resize(binary, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    binary = _deslant(binary)
    # A drawn face may leave any margin around the digits. The usual layout (fixed
    # positions, slid a little sideways) is tried first because it is the more
    # exact when it fits; the cells placed by the ink are the second try.
    strategies = (_digit_cell_boxes, _fit_digit_cells) if drawn else (_digit_cell_boxes,)
    first: TemperatureInspection | None = None
    for find_cells in strategies:
        boxes = find_cells(binary)
        if boxes is None:
            continue
        cells = tuple(binary[top_:bottom_ + 1, left_:right_] for left_, top_, right_, bottom_ in boxes)
        # Never close the gap by shifting later digits left: that made 21.9 appear
        # as a different, plausible temperature. One unreadable digit fails the read.
        digits = tuple(_classify_digit(cell) for cell in cells)
        occupancy = tuple(_normalized_occupancy(cell) for cell in cells)
        reading = None
        if all(digit is not None for digit in digits):
            value = digits[0] * 10 + digits[1] + digits[2] / 10
            reading = float(value) if -10 <= value <= 60 else None
        result = TemperatureInspection(display, gray, binary, tuple(boxes), cells, digits, occupancy, reading)
        if reading is not None:
            return result
        first = first or result
    return first or TemperatureInspection(display, gray, binary)


def _face_threshold(gray: np.ndarray) -> int:
    return max(95, min(150, int(np.percentile(gray, 65))))


# In infrared the picture is blurry, the LCD face is brighter and lit from one
# side, and the digits are faint mid-grays whose lower bars are the faintest.
# Sharpen first (the camera's own blur is what hides those bars), then call ink
# whatever is clearly darker than the smooth local background, so a brightness
# gradient does not matter. Chosen on 24 real frames of a display showing 21.8:
# every setting nearby read it right or not at all, never as another digit.
_INFRARED_SHARPEN_AMOUNT = 4.5
_INFRARED_SHARPEN_SIGMA = 3.0
_INFRARED_INK_DELTA = 9


def _infrared_ink(gray: np.ndarray) -> np.ndarray:
    plain = gray.astype(np.float32)
    sharp = np.clip(
        plain + _INFRARED_SHARPEN_AMOUNT * (plain - cv2.GaussianBlur(plain, (0, 0), _INFRARED_SHARPEN_SIGMA)),
        0, 255,
    )
    smooth = cv2.GaussianBlur(sharp.astype(np.uint8), (0, 0), 1.0).astype(np.float32)
    background = cv2.GaussianBlur(sharp, (0, 0), max(6, min(gray.shape) / 3))
    return (smooth < background - _INFRARED_INK_DELTA).astype(np.uint8) * 255


def _flat_field_ink(gray: np.ndarray) -> np.ndarray:
    """Ink of a dim, blurred display: what is darker than its surroundings by a fraction
    of the picture's own contrast.

    The fixed offset of ``_infrared_ink`` is made for a sharp, well-lit face. Behind a
    cold lamp, out of focus, the whole face may differ by only 20 levels, so the cut is
    taken relative to the contrast actually there.
    """
    plain = gray.astype(np.float32)
    flat = plain - cv2.GaussianBlur(plain, (0, 0), 14)
    relative = cv2.GaussianBlur(flat / (float(flat.std()) + 1e-6), (0, 0), 1.2)
    return (relative < -0.3).astype(np.uint8) * 255


def _fit_digit_cells(binary: np.ndarray) -> list[tuple[int, int, int, int]] | None:
    """Three equally spaced digit cells placed where the ink says they are.

    The corners the user draws decide how much margin the face has, so the digits
    do not sit at fixed fractions of its width. The cells (width, gap, first
    position) are chosen to hold as much ink as possible while the gaps between
    them stay empty.
    """
    height, width = binary.shape
    ink = binary > 0
    columns = ink.sum(axis=0)
    prefix = np.concatenate(([0.0], np.cumsum(columns, dtype=np.float64)))
    # A cell wider than its digit would still hold all of its ink, but the segment
    # boxes would no longer match the strokes: padding of near-empty columns costs.
    strong = float(np.percentile(columns, 90)) if columns.size else 0.0
    empty = columns < max(0.02 * height, 0.05 * strong)
    empty_prefix = np.concatenate(([0.0], np.cumsum(empty, dtype=np.float64)))
    padding_cost = float(np.median(columns[columns > 0])) if (columns > 0).any() else 1.0
    best = None
    step = max(1, width // 100)
    for cell in range(int(0.12 * width), int(0.31 * width), step):
        for gap in range(max(2, int(0.02 * width)), int(0.10 * width), max(1, width // 150)):
            pitch = cell + gap
            # the decimal point sits between the second and third digits: that step is wider
            offsets = (0, pitch, pitch + int(round(_DECIMAL_STEP * pitch)))
            for start in range(0, int(0.35 * width), step):
                spans = [(start + offset, start + offset + cell) for offset in offsets]
                if spans[-1][1] > width:
                    continue
                inside = sum(prefix[right] - prefix[left] for left, right in spans)
                between = sum(prefix[spans[i + 1][0]] - prefix[spans[i][1]] for i in range(2))
                padding = sum(empty_prefix[right] - empty_prefix[left] for left, right in spans)
                score = inside - 2.5 * between - padding_cost * padding
                if best is None or score > best[0]:
                    best = (score, spans)
    if best is None:
        return None
    spans = best[1]
    region = ink[:, spans[0][0]:spans[-1][1]]
    rows = region.sum(axis=1) > region.shape[1] * 0.02
    rows[:round(height * 0.02)] = False
    rows[round(height * 0.99):] = False
    filled = np.flatnonzero(rows)
    if filled.size < height * 0.3:
        return None
    return [(left, int(filled[0]), right, int(filled[-1])) for left, right in spans]


_CELL_CENTERS = (0.32, 0.53, 0.76)
_DECIMAL_STEP = (0.76 - 0.53) / (0.53 - 0.32)  # second step over the first, from the fixed layout
_CELL_WIDTH = 0.16


def _drop_edge_ink(binary: np.ndarray) -> np.ndarray:
    """Erase the bezel: narrow ink shapes that touch the left or right side of the face.

    The digits sit well inside the face (margin of about a quarter on the left,
    a sixth on the right), so ink reaching a side edge is the dark frame, the
    degree/unit marks or a shadow. Left in, it pulls the cell search sideways.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary > 0).astype(np.uint8), 8)
    width = binary.shape[1]
    cleaned = binary.copy()
    for index in range(1, count):
        x, _, box_width, _, _ = stats[index]
        if (x == 0 or x + box_width >= width) and box_width < 0.25 * width:
            cleaned[labels == index] = 0
    return cleaned


def _digit_cell_boxes(binary: np.ndarray) -> list[tuple[int, int, int, int]] | None:
    """Return (left, top, right, bottom) for each of the three digit positions.

    Horizontal positions follow the thermometer's fixed layout. The vertical
    extent is measured from the ink itself, so a bottom bar cut by the ROI or
    a slightly different crop does not shift the segment grid.
    """
    height, width = binary.shape

    def spans_for(shift: float) -> list[tuple[int, int]]:
        return [
            (
                max(0, round((center + shift - _CELL_WIDTH / 2) * width)),
                min(width, round((center + shift + _CELL_WIDTH / 2) * width)),
            )
            for center in _CELL_CENTERS
        ]

    # The crop of the LCD face is not identical in every light, which slides the
    # digits sideways a little. Slide the three cells together to where they
    # capture the most ink, so a digit's outer bar is not cut off.
    column_ink = (binary > 0).sum(axis=0)
    shift = max(
        np.linspace(-0.05, 0.05, 21),
        key=lambda candidate: sum(int(column_ink[left:right].sum()) for left, right in spans_for(candidate)),
    )
    span = spans_for(float(shift))
    ink = binary[:, span[0][0]:span[-1][1]] > 0
    rows = ink.sum(axis=1) > ink.shape[1] * 0.02
    # Ignore the thin bezel edges at the very top and bottom of the crop.
    rows[:round(height * 0.04)] = False
    rows[round(height * 0.98):] = False
    filled = np.flatnonzero(rows)
    if filled.size < height * 0.3:
        return None
    top, bottom = int(filled[0]), int(filled[-1])
    return [(left, top, right, bottom) for left, right in span]


def _deslant(binary: np.ndarray) -> np.ndarray:
    """Straighten italic seven-segment digits so segments align with the grid.

    Picks the horizontal shear that makes the digits' vertical strokes line up
    best, measured by how concentrated the column sums become.
    """
    height = binary.shape[0]
    # The shear is searched on a small copy; shear is scale independent.
    small = cv2.resize(binary, None, fx=0.125, fy=0.125, interpolation=cv2.INTER_AREA)
    small_height = small.shape[0]
    best_shear, best_score = 0.0, -1.0
    for shear in np.linspace(-0.4, 0.4, 41):
        matrix = np.float32([[1, shear, -shear * small_height / 2], [0, 1, 0]])
        warped = cv2.warpAffine(small, matrix, (small.shape[1], small_height))
        score = float(np.square(warped.sum(axis=0, dtype=np.float64)).sum())
        if score > best_score:
            best_shear, best_score = float(shear), score
    matrix = np.float32([[1, best_shear, -best_shear * height / 2], [0, 1, 0]])
    return cv2.warpAffine(binary, matrix, (binary.shape[1], height))


# A digit is trusted only when each critical segment is clearly lit or clearly
# dark once normalized to the strongest segment. A segment is critical when
# flipping it alone would give another valid digit (3/9 differ by f, 5/6 by e,
# 0/8 by g, 1/7 by a): glare or a weakly lit segment there would otherwise turn
# into a plausible but wrong temperature, so such glyphs are reported as
# unreadable and get read again. Other segments (such as a bottom bar cropped
# by the ROI) only take part in choosing the closest pattern. Measured on the
# real display (69 digit readings), the weakest lit critical segment is the 8's lower-left
# bar (0.39, weakened by the camera angle) and the strongest dark one is the 9's
# lower-left (0.29), so the threshold sits between them at 0.34.
_PATTERNS = {frozenset(pattern) for _, pattern in _SHAPES}
_LIT_THRESHOLD = 0.34
_MIN_SEPARATION = 0.04
_MAX_BEST_SCORE = 1.5


def _segment_occupancy(glyph: np.ndarray) -> np.ndarray | None:
    """Ink share of each segment region (a..g), or None for a degenerate glyph."""
    height, width = glyph.shape
    if height < 8 or width < 4:
        return None
    occupancy = np.empty(len(_SEGMENTS))
    for index, name in enumerate(_SEGMENTS):
        x0, y0, x1, y1 = _SEGMENTS[name]
        xa, xb = int(width * x0), max(int(width * x0) + 1, int(width * x1))
        ya, yb = int(height * y0), max(int(height * y0) + 1, int(height * y1))
        occupancy[index] = glyph[ya:yb, xa:xb].mean() / 255
    return occupancy


def _normalized_occupancy(glyph: np.ndarray) -> tuple[float, ...]:
    """Segment occupancy relative to the strongest segment (what is classified)."""
    occupancy = _segment_occupancy(glyph)
    if occupancy is None or occupancy.max() <= 0:
        return (0.0,) * len(_SEGMENTS)
    return tuple(float(value) for value in occupancy / occupancy.max())


def _classify_digit(glyph: np.ndarray) -> int | None:
    names = list(_SEGMENTS)
    occupancy = _segment_occupancy(glyph)
    if occupancy is None:
        return None
    strongest = occupancy.max()
    if strongest < 0.2:
        return None
    # Lit segments vary in brightness and a bar can be partly cropped, so
    # compare against the strongest segment instead of a fixed cutoff.
    occupancy = occupancy / strongest
    # First read the segments directly: lit above the threshold, dark below.
    # When that spells a digit exactly, trust it (an 8 whose lower bars are
    # weakened by the camera angle is still an 8). Otherwise fall back to the
    # closest pattern, which tolerates one cropped bar such as a 2's bottom.
    lit = frozenset(name for name, value in zip(names, occupancy) if value > _LIT_THRESHOLD)
    match = next(((candidate, shape) for candidate, shape in _SHAPES if frozenset(shape) == lit), None)
    if match is None:
        match = min(
            _SHAPES,
            key=lambda item: float(
                np.square(occupancy - [1.0 if name in item[1] else 0.0 for name in names]).sum()
            ),
        )
    digit, shape = match
    best = float(np.square(occupancy - [1.0 if name in shape else 0.0 for name in names]).sum())
    if best > _MAX_BEST_SCORE:
        return None
    pattern = frozenset(shape)
    for name, value in zip(names, occupancy, strict=True):
        if pattern ^ {name} not in _PATTERNS:
            continue
        separation = value - _LIT_THRESHOLD if name in pattern else _LIT_THRESHOLD - value
        if separation < _MIN_SEPARATION:
            return None
    return digit
