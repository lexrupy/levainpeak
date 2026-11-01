import cv2
import numpy as np

from levainpeak.alignment import Alignment, make_reference
from levainpeak.analysis import ContainerAnalyzer
from levainpeak.target import find_plus


def _frame(shift_x=0, shift_y=0):
    frame = np.full((600, 900, 3), 110, np.uint8)
    cv2.rectangle(frame, (300 + shift_x, 150 + shift_y), (600 + shift_x, 500 + shift_y), (225, 225, 225), -1)
    cv2.line(frame, (390 + shift_x, 320 + shift_y), (510 + shift_x, 320 + shift_y), (30, 30, 30), 9)
    cv2.line(frame, (450 + shift_x, 260 + shift_y), (450 + shift_x, 380 + shift_y), (30, 30, 30), 9)
    return cv2.GaussianBlur(frame, (0, 0), 1.2)


OUTLINE = ((300, 150), (600, 150), (600, 500), (300, 500))
ROI = (200, 100, 700, 560)


def _analyzer_with_reference():
    analyzer = ContainerAnalyzer.__new__(ContainerAnalyzer)
    analyzer.use_mark = True
    analyzer.mark_seen = None
    analyzer._mark_slide = 0.0
    analyzer.profile = type("Profile", (), {"infrared": False})()
    base = _frame()
    plus = find_plus(base, ROI)
    assert plus is not None
    return analyzer, make_reference(base, OUTLINE, ROI, None, (plus.x, plus.y))


def test_the_mark_alone_moves_the_drawing_sideways_and_up_when_the_edges_find_nothing():
    analyzer, reference = _analyzer_with_reference()
    moved = analyzer._with_plus(reference, None, _frame(shift_x=40, shift_y=-12), ROI)
    assert moved is not None
    assert abs((moved.dx - reference.origin[0]) - 40) <= 2 and abs((moved.dy - reference.origin[1]) + 12) <= 2


def test_with_the_edges_found_the_mark_corrects_only_the_height():
    analyzer, reference = _analyzer_with_reference()
    found = Alignment(1.0, reference.origin[0] + 40.0, reference.origin[1] + 0.0, 0.9)
    moved = analyzer._with_plus(reference, found, _frame(shift_x=40, shift_y=-6), ROI)
    assert moved.dx == found.dx and abs(moved.dy - (found.dy - 6)) <= 2


def test_a_label_that_turned_is_not_taken_for_the_pot_sliding():
    analyzer, reference = _analyzer_with_reference()
    # The edges say the pot did not move, the label is 30 px to the right: the pot turned.
    found = Alignment(1.0, float(reference.origin[0]), float(reference.origin[1]), 0.9)
    analyzer._with_plus(reference, found, _frame_with_label_slid(30), ROI)
    moved = analyzer._with_plus(reference, None, _frame_with_label_slid(30), ROI)
    assert abs(moved.dx - reference.origin[0]) <= 2


def _frame_with_label_slid(slide):
    frame = _frame()
    cv2.line(frame, (390, 320), (510, 320), (225, 225, 225), 14)
    cv2.line(frame, (450, 260), (450, 380), (225, 225, 225), 14)
    cv2.line(frame, (390 + slide, 320), (510 + slide, 320), (30, 30, 30), 9)
    cv2.line(frame, (450 + slide, 260), (450 + slide, 380), (30, 30, 30), 9)
    return cv2.GaussianBlur(frame, (0, 0), 1.2)
