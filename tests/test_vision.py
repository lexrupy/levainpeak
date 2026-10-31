import cv2
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


def _jar_with_a_base(base_row=540):
    """Glass, pale levain on both sides of the dark mark, and a dark gap where the jar ends."""
    import cv2  # noqa: F401  (the frame is plain numpy; cv2 is only needed by the code under test)

    frame = np.full((620, 900, 3), 40, np.uint8)
    frame[100:560, 300:580] = 150
    frame[400:480, 300:580] = 245  # levain above the mark
    frame[480:495, 300:580] = 25  # the dark initial-level mark
    frame[495:base_row, 300:580] = 245  # levain under the mark, down to the base
    frame[base_row:, 300:580] = 60  # the bowl under the jar
    return frame


def test_the_levain_base_is_where_the_pale_mass_ends():
    from levainpeak.vision import _levain_base

    base = _levain_base(_jar_with_a_base(540), 300, 580, surface_y=400, marker_y=480, bottom=600)
    assert abs(base - 540) <= 1


def test_the_levain_base_needs_the_levain_to_be_above_the_mark():
    from levainpeak.vision import _levain_base

    # surface below the mark: the rows under the mark are not levain, nothing to anchor on
    assert _levain_base(_jar_with_a_base(), 300, 580, surface_y=500, marker_y=480, bottom=600) is None
    assert _levain_base(_jar_with_a_base(), 300, 580, surface_y=400, marker_y=None, bottom=600) is None


def test_the_levain_base_gives_up_when_nothing_pale_lies_under_the_mark():
    from levainpeak.vision import _levain_base

    frame = _jar_with_a_base()
    frame[495:, 300:580] = 100
    assert _levain_base(frame, 300, 580, surface_y=400, marker_y=480, bottom=600) is None


def test_the_levain_base_gives_up_when_the_mass_never_ends_before_the_bottom():
    from levainpeak.vision import _levain_base

    frame = _jar_with_a_base(base_row=620)  # pale all the way down
    assert _levain_base(frame, 300, 580, surface_y=400, marker_y=480, bottom=600) is None


def test_surface_cluster_ignores_columns_that_ran_past_the_surface():
    from levainpeak.vision import _surface_cluster

    tops = np.array([704] * 40 + [705] * 10 + [616] * 15 + [660, 682, 643, 689, 671, 684] * 3)
    assert 703 <= _surface_cluster(tops) <= 705
    assert _surface_cluster(np.array([500, 501, 499, 502, 500, 498, 503])) == 500


def _tapered_jar(with_levain: bool):
    img = np.full((600, 800, 3), 120, np.uint8)
    cv2.fillPoly(img, [np.array([(200, 100), (600, 100), (540, 520), (260, 520)])], (150, 150, 150))
    if with_levain:
        cv2.fillPoly(img, [np.array([(231, 330), (569, 330), (540, 520), (260, 520)])], (225, 225, 225))
        cv2.line(img, (222, 400), (578, 400), (20, 20, 20), 9)
    cv2.rectangle(img, (0, 520), (800, 600), (30, 30, 30), -1)
    return cv2.GaussianBlur(img, (0, 0), 1.2)


OUTLINE = ((200, 100), (600, 100), (540, 520), (260, 520))


def test_a_drawn_outline_measures_a_tapered_jar_the_automatic_search_cannot():
    from levainpeak.vision import measure_container

    measurement = measure_container(_tapered_jar(True), (150, 60, 650, 560), outline=OUTLINE)
    assert measurement is not None and not measurement.empty
    assert abs(measurement.surface_y - 330) <= 4
    assert measurement.marker_y is None  # no automatic mark on a drawn contour; the user places it
    assert (measurement.top, measurement.bottom) == (100, 520)
    # the cone is narrower at the bottom, and the volume counts that
    top_width = measurement.edge_profile[0][1] - measurement.edge_profile[0][0]
    bottom_width = measurement.edge_profile[-1][1] - measurement.edge_profile[-1][0]
    assert bottom_width < 0.8 * top_width
    assert 0.2 < measurement.volume_fraction < 0.5


def test_a_drawn_outline_around_an_empty_jar_reports_empty_not_nothing():
    from levainpeak.vision import measure_container

    measurement = measure_container(_tapered_jar(False), (150, 60, 650, 560), outline=OUTLINE)
    assert measurement is not None and measurement.empty
    assert measurement.volume_fraction == 0.0 and measurement.confidence == 0.0
