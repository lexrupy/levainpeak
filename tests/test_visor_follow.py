import cv2
import numpy as np

from levainpeak.visor_follow import carry_visor, locate_visor, make_visor_reference


def scene(dx=0, dy=0):
    frame = np.full((600, 800, 3), 60, np.uint8)
    cv2.rectangle(frame, (300 + dx, 200 + dy), (460 + dx, 280 + dy), (200, 200, 200), -1)  # body
    cv2.rectangle(frame, (320 + dx, 215 + dy), (440 + dx, 265 + dy), (30, 30, 30), -1)  # face
    cv2.circle(frame, (470 + dx, 300 + dy), 6, (255, 255, 255), -1)  # a button
    cv2.line(frame, (200 + dx, 400 + dy), (330 + dx, 330 + dy), (150, 150, 150), 5)
    return frame


def test_the_area_follows_a_camera_nudge():
    roi, quad = (320, 215, 440, 265), ((320, 215), (440, 215), (440, 265), (320, 265))
    reference = make_visor_reference(scene(), roi, quad)
    dx, dy, score = locate_visor(reference, scene(-37, 22))
    assert score > 0.8 and abs(dx + 37) <= 2 and abs(dy - 22) <= 2
    moved_roi, moved_quad = carry_visor(reference, dx, dy)
    assert abs(moved_roi[0] - (320 - 37)) <= 2 and abs(moved_quad[2][1] - (265 + 22)) <= 2


def test_a_picture_with_nothing_like_it_scores_low():
    roi = (320, 215, 440, 265)
    reference = make_visor_reference(scene(), roi, None)
    blank = np.full((600, 800, 3), 60, np.uint8)
    found = locate_visor(reference, blank)
    assert found is None or found[2] < 0.6


def test_a_blank_area_gives_no_reference():
    assert make_visor_reference(np.full((600, 800, 3), 60, np.uint8), (320, 215, 440, 265), None) is None
