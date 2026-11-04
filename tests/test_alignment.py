import cv2
import numpy as np

from levainpeak.alignment import (
    MIN_SCORE, carry, load_reference, locate, make_reference, reference_path, save_reference,
)

OUTLINE = ((330, 260), (690, 250), (640, 560), (380, 570))


def _scene():
    rng = np.random.default_rng(3)
    frame = cv2.GaussianBlur(rng.integers(60, 200, (720, 1024, 3), dtype=np.uint8), (0, 0), 6)
    cv2.polylines(frame, [np.array(OUTLINE)], True, (235, 235, 235), 5)  # glass walls
    cv2.rectangle(frame, (310, 215), (710, 262), (200, 220, 230), -1)  # lid
    cv2.rectangle(frame, (360, 568), (660, 600), (120, 120, 130), -1)  # base
    cv2.line(frame, (350, 400), (670, 400), (30, 30, 30), 3)  # in the levain's own area
    return frame


def _moved(frame, scale, dx, dy):
    center = np.mean(OUTLINE, axis=0)
    matrix = np.float32([[scale, 0, center[0] * (1 - scale) + dx], [0, scale, center[1] * (1 - scale) + dy]])
    return cv2.warpAffine(frame, matrix, (frame.shape[1], frame.shape[0]), borderMode=cv2.BORDER_REPLICATE)


def _expected(scale, dx, dy):
    center = np.mean(OUTLINE, axis=0)
    return [(scale * (x - center[0]) + center[0] + dx, scale * (y - center[1]) + center[1] + dy) for x, y in OUTLINE]


def test_the_drawing_follows_a_pot_that_slid_or_moved_closer():
    frame = _scene()
    reference = make_reference(frame, OUTLINE, (300, 200, 720, 610), 450)
    for scale, dx, dy in ((1.0, 0, 0), (1.0, 45, -20), (1.0, -60, 15), (1.05, 10, 5), (0.94, -20, 10)):
        found = locate(reference, _moved(frame, scale, dx, dy))
        outline, roi, marker = carry(reference, found)
        assert found.score >= MIN_SCORE
        error = np.abs(np.array(outline) - np.array(_expected(scale, dx, dy))).max()
        assert error <= 7, (scale, dx, dy, error)
        assert abs(marker - (450 + dy)) <= 12 + abs(scale - 1) * 150


def test_a_covered_pot_is_not_found():
    frame = _scene()
    reference = make_reference(frame, OUTLINE, None, None)
    covered = frame.copy()
    cv2.rectangle(covered, (250, 150), (760, 650), (40, 40, 40), -1)
    found = locate(reference, covered)
    assert found is None or found.score < MIN_SCORE


def test_the_reference_survives_a_restart(tmp_path):
    reference = make_reference(_scene(), OUTLINE, (300, 200, 720, 610), 450)
    path = reference_path("dia", tmp_path)
    save_reference(reference, path)
    loaded = load_reference(path)
    assert loaded.outline == reference.outline and loaded.roi == reference.roi
    assert loaded.marker_y == 450 and loaded.origin == reference.origin
    assert load_reference(tmp_path / "missing.npz") is None


def test_the_analyzer_moves_the_drawing_after_two_agreeing_searches(tmp_path, monkeypatch):
    from levainpeak.analysis import ContainerAnalyzer

    analyzer = ContainerAnalyzer(stream=None)
    analyzer.reference_base = tmp_path / "sessao"
    analyzer.outline, analyzer.roi, analyzer.marker_y = OUTLINE, (300, 200, 720, 610), 450
    frame = _scene()

    def look(image):
        analyzer._last_alignment = 0.0
        analyzer._follow_pot(image)

    look(frame)  # the first look only takes the reference
    moved = _moved(frame, 1.0, 40, -12)
    look(moved)
    assert analyzer.aligned is None and analyzer.outline == OUTLINE  # one search is not enough
    look(moved)
    outline, roi, marker = analyzer.aligned
    assert abs(outline[0][0] - (OUTLINE[0][0] + 40)) <= 3 and abs(outline[0][1] - (OUTLINE[0][1] - 12)) <= 3
    assert abs(marker - 438) <= 4 and analyzer.outline == outline
    covered = moved.copy()
    cv2.rectangle(covered, (250, 150), (800, 650), (40, 40, 40), -1)
    look(covered)
    assert analyzer.alignment_status == "pote não localizado" and analyzer.outline == outline


def test_the_volume_is_joined_when_the_camera_profile_changes_and_when_a_session_resumes():
    from levainpeak.analysis import ContainerAnalyzer
    from levainpeak.profiles import DAY, INFRARED

    analyzer = ContainerAnalyzer(stream=None)
    analyzer.anchor_volume = 0.657  # last row of the resumed session
    analyzer.profile = INFRARED
    assert abs(analyzer._join_volume(0.600) - 0.657) < 1e-9  # first reading joins the CSV
    assert abs(analyzer._join_volume(0.610) - 0.667) < 1e-9  # then it follows the levain
    analyzer.profile = DAY  # lamp on: the same levain reads 0.05 higher
    assert abs(analyzer._join_volume(0.665) - 0.667) < 1e-9
    assert abs(analyzer._join_volume(0.675) - 0.677) < 1e-9
    analyzer.profile = INFRARED
    assert abs(analyzer._join_volume(0.620) - 0.677) < 1e-9


def _pot_over_a_static_room(dx):
    """The room behind the glass stays put; only the pot (walls, lid, base) slides by ``dx``."""
    rng = np.random.default_rng(5)
    frame = cv2.GaussianBlur(rng.integers(40, 220, (720, 1024, 3), dtype=np.uint8), (0, 0), 4)
    cv2.rectangle(frame, (700, 100), (760, 620), (160, 90, 40), -1)  # a pipe, strong edges, never moves
    for x in (400, 470, 540, 610):  # what shows through the glass: streaks that stay where they are
        cv2.line(frame, (x, 270), (x, 560), (250, 250, 250), 6)
    cv2.rectangle(frame, (200, 575), (820, 640), (230, 230, 235), -1)  # the plate under the pot
    outline = np.array(OUTLINE) + [dx, 0]
    cv2.polylines(frame, [outline], True, (235, 235, 235), 5)
    cv2.rectangle(frame, (310 + dx, 215), (710 + dx, 262), (200, 220, 230), -1)
    cv2.rectangle(frame, (360 + dx, 568), (660 + dx, 600), (120, 120, 130), -1)
    return frame


def test_a_pot_sliding_over_a_static_room_is_followed_not_the_room():
    reference = make_reference(_pot_over_a_static_room(0), OUTLINE, None, None)
    found = locate(reference, _pot_over_a_static_room(38))
    assert found is not None and found.score >= MIN_SCORE
    assert abs((found.dx - reference.origin[0]) - 38) <= 3


def test_references_are_kept_per_session(tmp_path):
    from levainpeak.analysis import ContainerAnalyzer

    frame = _scene()
    first, second = ContainerAnalyzer(stream=None), ContainerAnalyzer(stream=None)
    first.reference_base, second.reference_base = tmp_path / "um", tmp_path / "dois"
    first.outline, first.roi = OUTLINE, (300, 200, 720, 610)
    first._follow_pot(frame)  # the first look stores the reference of the first session only
    second.load_references()
    assert second._references == {}
    first._references.clear()
    first.load_references()
    assert first._references
    loose = ContainerAnalyzer(stream=None)  # no session: nothing is read from or written to disk
    loose.load_references()
    assert loose._references == {}


def test_with_no_session_nothing_is_joined():
    from levainpeak.analysis import ContainerAnalyzer
    from levainpeak.profiles import INFRARED

    analyzer = ContainerAnalyzer(stream=None)
    analyzer.join_enabled = False
    analyzer.anchor_volume = 0.657  # left over from a session that is closed
    analyzer.profile = INFRARED
    assert analyzer._join_volume(0.600) == 0.600
    assert analyzer.volume_shift == 0.0
