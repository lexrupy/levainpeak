import cv2
import numpy as np

from levainpeak.target import find_plus


def _label(draw):
    frame = np.full((400, 600, 3), 120, np.uint8)
    cv2.rectangle(frame, (100, 80), (500, 340), (235, 235, 235), -1)  # the white label
    draw(frame)
    return cv2.GaussianBlur(frame, (0, 0), 1.5)


def test_a_thick_plus_on_the_label_is_found_at_its_crossing():
    frame = _label(lambda f: (cv2.line(f, (230, 200), (370, 200), (30, 30, 30), 9),
                              cv2.line(f, (300, 140), (300, 260), (30, 30, 30), 9)))
    found = find_plus(frame, (0, 0, 600, 400))
    assert found is not None and abs(found.x - 300) <= 3 and abs(found.y - 200) <= 3


def test_a_thick_x_is_found_at_its_center():
    frame = _label(lambda f: (cv2.line(f, (240, 140), (360, 260), (30, 30, 30), 9),
                              cv2.line(f, (360, 140), (240, 260), (30, 30, 30), 9)))
    found = find_plus(frame, (0, 0, 600, 400))
    assert found is not None and abs(found.x - 300) <= 3 and abs(found.y - 200) <= 3


def test_a_plain_label_or_a_dark_blob_has_no_mark():
    assert find_plus(_label(lambda f: None), (0, 0, 600, 400)) is None
    assert find_plus(_label(lambda f: cv2.circle(f, (300, 200), 40, (30, 30, 30), -1)), (0, 0, 600, 400)) is None
