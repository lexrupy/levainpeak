import cv2
import numpy as np
import pytest

from levainpeak.temperature import _DIGITS, read_temperature

# Independent drawing geometry (full-width bars), not the classifier's regions.
DRAW = {
    "a": (0.15, 0.02, 0.85, 0.16), "b": (0.80, 0.10, 0.98, 0.50),
    "c": (0.80, 0.50, 0.98, 0.90), "d": (0.15, 0.84, 0.85, 0.98),
    "e": (0.02, 0.50, 0.20, 0.90), "f": (0.02, 0.10, 0.20, 0.50),
    "g": (0.15, 0.45, 0.85, 0.55),
}

FACE_W, FACE_H = 300, 140
CENTERS = (0.32, 0.53, 0.76)
CELL = 0.16


def render_display(text: str, *, noise: float = 0.0, glare: bool = False, seed: int = 0, weak: str = "", weak_scale: float = 0.22):
    """Dark bezel around a bright LCD face with three seven-segment digits."""
    face = np.full((FACE_H, FACE_W), 190, np.uint8)
    for center, char in zip(CENTERS, text.replace(".", ""), strict=True):
        x0 = (center - CELL / 2) * FACE_W
        y0, y1 = FACE_H * 0.08, FACE_H * 0.95
        height, width = y1 - y0, CELL * FACE_W
        for name in _DIGITS[int(char)]:
            sx0, sy0, sx1, sy1 = DRAW[name]
            if name in weak:  # a bar dimmed by the viewing angle: smaller footprint
                cx, cy, half_w, half_h = (sx0 + sx1) / 2, (sy0 + sy1) / 2, (sx1 - sx0) * weak_scale, (sy1 - sy0) * weak_scale
                sx0, sx1, sy0, sy1 = cx - half_w, cx + half_w, cy - half_h, cy + half_h
            cv2.rectangle(
                face,
                (int(x0 + sx0 * width), int(y0 + sy0 * height)),
                (int(x0 + sx1 * width), int(y0 + sy1 * height)),
                40, -1,
            )
    if glare:
        cv2.ellipse(face, (int(FACE_W * 0.2), int(FACE_H * 0.8)), (40, 12), 0, 0, 360, 235, -1)
    frame = cv2.copyMakeBorder(face, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=20)
    if noise:
        rng = np.random.default_rng(seed)
        frame = np.clip(frame + rng.normal(0, noise, frame.shape), 0, 255).astype(np.uint8)
    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    height, width = frame.shape[:2]
    return frame, (0, 0, width, height)


@pytest.mark.parametrize("text", ["21.8", "29.2", "24.0", "26.5", "30.9", "18.3", "25.6", "27.1"])
def test_reads_clean_display(text):
    frame, roi = render_display(text)
    assert read_temperature(frame, roi) == pytest.approx(float(text))


def test_every_digit_in_every_position():
    wrong = []
    for digit in range(10):
        for text in (f"{digit}0.0", f"2{digit}.0", f"20.{digit}"):
            frame, roi = render_display(text)
            value = read_temperature(frame, roi)
            if value is not None and value != pytest.approx(float(text)):
                wrong.append((text, value))
    assert not wrong, f"confident misreads: {wrong}"


def test_noise_never_yields_a_confident_wrong_value():
    wrong = []
    for seed in range(40):
        for text in ("21.8", "29.2", "25.6", "23.9"):
            frame, roi = render_display(text, noise=18, seed=seed)
            value = read_temperature(frame, roi)
            if value is not None and value != pytest.approx(float(text)):
                wrong.append((text, value, seed))
    assert not wrong, f"confident misreads: {wrong[:5]} ({len(wrong)} total)"


def test_glare_never_yields_a_confident_wrong_value():
    for text in ("21.8", "29.2", "25.6", "19.9"):
        frame, roi = render_display(text, glare=True)
        value = read_temperature(frame, roi)
        assert value is None or value == pytest.approx(float(text))


def test_an_eight_with_a_bar_dimmed_by_the_angle_is_still_an_eight():
    # lower-left bar at ~0.46 of the strongest: the old 0.45 threshold refused this
    frame, roi = render_display("21.8", weak="e")
    assert read_temperature(frame, roi) == pytest.approx(21.8)


def test_a_bar_too_dim_to_judge_is_never_turned_into_another_digit():
    # lower-left bar at ~0.37: the old threshold read this 8 as a 9 (21.9)
    frame, roi = render_display("21.8", weak="e", weak_scale=0.16)
    assert read_temperature(frame, roi) in (None, pytest.approx(21.8))


def test_a_nine_is_still_a_nine_next_to_dimmed_bars():
    frame, roi = render_display("21.9", weak="c")
    assert read_temperature(frame, roi) == pytest.approx(21.9)


def _infrared_like(text, *, seed=0):
    """Grayscale, blurry, low contrast, brighter on one side: the night-mode look."""
    frame, roi = render_display(text, noise=3, seed=seed)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.9)  # strong blur: 2.2 makes a 3 leak into an 8
    gray = 150 + 0.45 * (gray - 120)  # lower contrast, lifted
    ramp = np.linspace(40, -25, gray.shape[1], dtype=np.float32)  # one side brighter
    gray = np.clip(gray + ramp[None, :], 0, 255).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), roi


def test_infrared_look_reads_the_digits_or_gives_up_never_a_wrong_value():
    wrong, read = [], 0
    for text in ("21.8", "21.9", "23.5", "27.1", "18.3", "25.6", "30.4", "19.7"):
        for seed in range(3):
            frame, roi = _infrared_like(text, seed=seed)
            value = read_temperature(frame, roi, infrared=True)
            if value is None:
                continue
            read += 1
            if value != pytest.approx(float(text)):
                wrong.append((text, value))
    assert not wrong, f"confident misreads in infrared: {wrong}"
    assert read >= 16  # it must not simply refuse everything


def _in_a_big_region(text, padding, *, seed=0):
    """The display lost in a much larger selected area with bright objects around it."""
    frame, _ = render_display(text, noise=2, seed=seed)
    height, width = frame.shape[:2]
    big = np.full((height + 2 * padding, width + 2 * padding, 3), 55, np.uint8)
    big[padding:padding + height, padding:padding + width] = frame
    cv2.rectangle(big, (5, 5), (padding + 40, padding // 2), (215, 215, 215), -1)  # a bright shelf edge
    cv2.circle(big, (big.shape[1] - 30, big.shape[0] - 30), 22, (230, 230, 230), -1)  # a lamp reflection
    return big, (0, 0, big.shape[1], big.shape[0])


@pytest.mark.parametrize("padding", [20, 60, 120, 200])
def test_a_much_larger_region_still_reads_or_gives_up_never_a_wrong_value(padding):
    wrong, read = [], 0
    for text in ("21.8", "21.9", "29.2", "25.6"):
        frame, roi = _in_a_big_region(text, padding)
        value = read_temperature(frame, roi)
        if value is None:
            continue
        read += 1
        if value != pytest.approx(float(text)):
            wrong.append((text, value))
    assert not wrong, f"confident misreads with padding {padding}: {wrong}"
    assert read >= 3


@pytest.mark.parametrize("text", ["23.7", "27.0", "20.7", "17.7"])
def test_a_seven_drawn_with_the_upper_left_bar_is_still_a_seven(monkeypatch, text):
    monkeypatch.setitem(_DIGITS, 7, "abcf")
    frame, roi = render_display(text)
    assert read_temperature(frame, roi) == pytest.approx(float(text))


@pytest.mark.parametrize("angle", [-9, -5, 6, 11])
@pytest.mark.parametrize("text", ["21.8", "26.6", "23.7"])
def test_a_display_stuck_on_the_wall_at_a_tilt_is_still_read(text, angle):
    frame, _ = render_display(text)
    frame = cv2.copyMakeBorder(frame, 50, 50, 50, 50, cv2.BORDER_CONSTANT, value=(190, 190, 190))  # the wall around it
    height, width = frame.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    tilted = cv2.warpAffine(frame, matrix, (width, height), flags=cv2.INTER_CUBIC, borderValue=(190, 190, 190))
    assert read_temperature(tilted, (0, 0, width, height)) == pytest.approx(float(text))


@pytest.mark.parametrize("text", ["21.8", "26.6", "24.5", "30.9"])
def test_a_drawn_face_quad_reads_a_display_seen_at_an_angle(text):
    frame, _ = render_display(text)
    frame = cv2.copyMakeBorder(frame, 80, 80, 80, 80, cv2.BORDER_CONSTANT, value=(190, 190, 190))
    face = np.float32([(110, 110), (110 + FACE_W, 110), (110 + FACE_W, 110 + FACE_H), (110, 110 + FACE_H)])
    # seen from the side and a little rotated: a perspective warp, as a wall-mounted display is
    seen = np.float32([(130, 120), (400, 150), (395, 290), (125, 270)])
    matrix = cv2.getPerspectiveTransform(face, seen)
    warped = cv2.warpPerspective(frame, matrix, (frame.shape[1], frame.shape[0]), borderValue=(190, 190, 190))
    quad = tuple((int(x), int(y)) for x, y in seen)
    assert read_temperature(warped, None, face_quad=quad) == pytest.approx(float(text))


def _dim_blurred(frame):
    """What a display out of focus and behind a weak infrared light looks like: little contrast, soft."""
    low = (150 + (frame.astype(np.float32) - 190) * 0.3).clip(0, 255).astype(np.uint8)
    return cv2.GaussianBlur(low, (0, 0), 1.5)


@pytest.mark.parametrize("text", ["25.6", "21.8", "30.9", "24.0", "29.5"])
@pytest.mark.parametrize("left_margin", [0.0, 0.10, 0.18])
def test_a_dim_blurred_face_is_read_wherever_the_user_put_its_left_edge(text, left_margin):
    frame, _ = render_display(text)
    frame = _dim_blurred(frame)
    # the face sits at (30, 30) .. (330, 170); the user's corners may hug the digits more or less
    x0 = 30 + int(left_margin * FACE_W)
    quad = ((x0, 30), (330, 30), (330, 170), (x0, 170))
    assert read_temperature(frame, None, infrared=True, face_quad=quad) == pytest.approx(float(text))


def test_the_dim_blurred_face_from_the_diagnostics_window_reads_25_6():
    """A real face (user's drawn corners, infrared, out of focus), 190 x 90 grey pixels."""
    from pathlib import Path

    sample = Path(__file__).parent / "data" / "face_ir_desfocada.png"
    face = cv2.imread(str(sample), cv2.IMREAD_GRAYSCALE)
    from levainpeak.temperature import _inspect_face

    result = _inspect_face(cv2.cvtColor(face, cv2.COLOR_GRAY2BGR), face, True, drawn=True)
    assert result.reading == pytest.approx(25.6)


def test_a_live_dim_face_from_the_camera_reads_25_6_with_boxes_around_whole_digits():
    """Out of focus, infrared, seen through the corners drawn by the user (a live frame, 217 x 126 grey pixels)."""
    from pathlib import Path

    from levainpeak.temperature import _inspect_face

    face = cv2.imread(str(Path(__file__).parent / "data" / "face_ir_viva.png"), cv2.IMREAD_GRAYSCALE)
    result = _inspect_face(cv2.cvtColor(face, cv2.COLOR_GRAY2BGR), face, True, drawn=True)
    assert result.reading == pytest.approx(25.6)
    # each box holds its whole digit: the ink does not run past the box edges
    width = result.binary.shape[1]
    for left, _, right, _ in result.cell_boxes:
        cell = result.binary[:, left:right] > 0
        assert cell[:, :3].sum() + cell[:, -3:].sum() < 0.02 * cell.size
        assert 0.1 * width < right - left < 0.3 * width


def test_the_black_white_cut_of_the_drawn_face_changes_what_counts_as_ink():
    from pathlib import Path

    from levainpeak.temperature import _flat_field_ink, inspect_temperature

    face = cv2.imread(str(Path(__file__).parent / "data" / "face_ir_viva.png"), cv2.IMREAD_GRAYSCALE)
    fainter = int((_flat_field_ink(face, 0.15) > 0).sum())
    default = int((_flat_field_ink(face, 0.3) > 0).sum())
    stricter = int((_flat_field_ink(face, 0.6) > 0).sum())
    assert fainter > default > stricter  # a lower cut reads fainter strokes as ink
    bgr = cv2.cvtColor(face, cv2.COLOR_GRAY2BGR)
    h, w = bgr.shape[:2]
    quad = ((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1))
    assert inspect_temperature(bgr, None, infrared=True, face_quad=quad, ink_cut=0.3).reading == pytest.approx(25.6)


def test_a_nine_whose_bottom_bar_faded_is_not_read_as_a_four():
    import numpy as np
    from levainpeak.temperature import _SEGMENTS, _classify_digit

    glyph = np.zeros((80, 40), np.uint8)
    for name, strength in {"a": 0.6, "b": 0.95, "c": 0.8, "d": 0.3, "e": 0.0, "f": 1.0, "g": 0.75}.items():
        x0, y0, x1, y1 = _SEGMENTS[name]
        glyph[int(80 * y0):max(int(80 * y0) + 1, int(80 * y1)), int(40 * x0):max(int(40 * x0) + 1, int(40 * x1))] = int(255 * strength)
    assert _classify_digit(glyph) == 9


def test_a_lit_lower_left_bar_leaves_only_digits_that_have_it():
    import numpy as np
    from levainpeak.temperature import _SEGMENTS, _classify_digit

    glyph = np.zeros((80, 40), np.uint8)
    for name, strength in {"a": 0.9, "b": 1.0, "c": 0.7, "d": 0.1, "e": 0.65, "f": 0.9, "g": 0.2}.items():
        x0, y0, x1, y1 = _SEGMENTS[name]
        glyph[int(80 * y0):max(int(80 * y0) + 1, int(80 * y1)), int(40 * x0):max(int(40 * x0) + 1, int(40 * x1))] = int(255 * strength)
    assert _classify_digit(glyph) in (0, 2, 6, 8, None)  # never a 7 (or a 4, a 9, a 3, a 5, a 1)


def test_a_dark_top_bar_leaves_only_digits_without_it():
    import numpy as np
    from levainpeak.temperature import _SEGMENTS, _classify_digit

    glyph = np.zeros((80, 40), np.uint8)
    for name, strength in {"a": 0.05, "b": 1.0, "c": 0.9, "d": 0.0, "e": 0.0, "f": 0.45, "g": 0.4}.items():
        x0, y0, x1, y1 = _SEGMENTS[name]
        glyph[int(80 * y0):max(int(80 * y0) + 1, int(80 * y1)), int(40 * x0):max(int(40 * x0) + 1, int(40 * x1))] = int(255 * strength)
    assert _classify_digit(glyph) in (1, 4, None)  # f and g are in between: a 7, a 3 or a 9 have a top bar


def test_no_digit_comes_out_that_contradicts_a_lit_or_a_dark_segment():
    import numpy as np
    from levainpeak.temperature import (
        _DARK_SEGMENT, _FRAGILE_SEGMENTS, _SEGMENTS, _SHAPES, _STRONG_SEGMENT, _classify_digit,
    )

    rng = np.random.default_rng(7)
    names = list(_SEGMENTS)
    checked = 0
    for _ in range(400):
        strengths = rng.uniform(0.0, 1.0, len(names))
        strengths[rng.integers(len(names))] = 1.0
        glyph = np.zeros((80, 40), np.uint8)
        for name, strength in zip(names, strengths):
            x0, y0, x1, y1 = _SEGMENTS[name]
            glyph[int(80 * y0):max(int(80 * y0) + 1, int(80 * y1)), int(40 * x0):max(int(40 * x0) + 1, int(40 * x1))] = int(255 * strength)
        digit = _classify_digit(glyph)
        if digit is None:
            continue
        lit = {name for name, value in zip(names, strengths) if value >= _STRONG_SEGMENT}
        dark = {name for name, value in zip(names, strengths) if value <= _DARK_SEGMENT} - _FRAGILE_SEGMENTS
        allowed = [shape for _, shape in _SHAPES if lit <= set(shape) and not dark & set(shape)]
        exact = [shape for _, shape in _SHAPES if set(shape) == {n for n, v in zip(names, strengths) if v > 0.34}]
        if allowed and not exact:
            assert any(digit == d for d, shape in _SHAPES if shape in allowed), (strengths, digit)
            checked += 1
    assert checked > 20


def test_a_five_without_its_bottom_bar_is_still_a_five():
    import numpy as np
    from levainpeak.temperature import _SEGMENTS, _classify_digit

    for bottom in (0.0, 0.15, 0.3):
        glyph = np.zeros((80, 40), np.uint8)
        for name, strength in {"a": 0.9, "b": 0.0, "c": 0.8, "d": bottom, "e": 0.0, "f": 0.95, "g": 0.85}.items():
            x0, y0, x1, y1 = _SEGMENTS[name]
            glyph[int(80 * y0):max(int(80 * y0) + 1, int(80 * y1)), int(40 * x0):max(int(40 * x0) + 1, int(40 * x1))] = int(255 * strength)
        assert _classify_digit(glyph) == 5
