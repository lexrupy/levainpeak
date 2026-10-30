import numpy as np

from levainpeak.vision import _robust_wall


def test_robust_wall_ignores_rows_that_grabbed_other_edges():
    rows = np.arange(300, dtype=np.float32)
    wall = 100 + 0.05 * rows
    noisy = wall.copy()
    noisy[40:90] += 35  # wood-grain streak grabbed the edge
    noisy[200:230] -= 30
    fitted = _robust_wall(noisy)
    assert np.abs(fitted - wall).max() < 2.0


def test_robust_wall_keeps_a_conical_slope():
    rows = np.arange(200, dtype=np.float32)
    fitted = _robust_wall(80 + 0.04 * rows)
    assert fitted[-1] - fitted[0] > 7


def test_robust_wall_falls_back_to_vertical_when_the_fit_is_steep():
    rows = np.arange(300, dtype=np.float32)
    rng = np.random.default_rng(3)
    edge = 150 - 0.2 * rows + rng.normal(0, 3, 300)  # implausibly steep for glass
    fitted = _robust_wall(edge)
    assert np.ptp(fitted) == 0


def _jar_frame():
    import cv2

    frame = np.full((600, 900, 3), 40, np.uint8)
    frame[100:560, 300:580] = 150  # empty glass in front of a light background
    frame[400:470, 300:580] = 245  # pale levain
    frame[470:480, 300:580] = 100  # shading right above the mark
    frame[480:495, 300:580] = 25  # dark initial-level mark
    return frame, cv2


def test_surface_by_columns_ignores_wall_climb_and_reflection_streak():
    from levainpeak.vision import _find_surface_by_columns

    frame, _ = _jar_frame()
    frame[250:400, 300:345] = 245  # levain/label climbing the left wall
    frame[150:400, 438:441] = 245  # narrow white reflection streak
    surface, confidence = _find_surface_by_columns(frame, 300, 580, 100, 560, marker_y=480)
    assert abs(surface - 400) <= 3
    assert confidence > 0.7


def test_surface_by_columns_gives_up_when_levain_and_glass_look_alike():
    from levainpeak.vision import _find_surface_by_columns

    frame, _ = _jar_frame()
    frame[400:470, 300:580] = 160
    assert _find_surface_by_columns(frame, 300, 580, 100, 560, marker_y=480) is None
