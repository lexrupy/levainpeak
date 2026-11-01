import numpy as np

from levainpeak.stream import rotate_frame


def test_quarter_turn_swaps_the_sides_clockwise():
    frame = np.zeros((2, 4, 3), np.uint8)
    frame[0, 0] = 255  # top-left
    turned = rotate_frame(frame, 90)
    assert turned.shape[:2] == (4, 2)
    assert turned[0, 1].all()  # top-left goes to top-right


def test_a_small_angle_keeps_the_size():
    frame = np.full((40, 60, 3), 200, np.uint8)
    assert rotate_frame(frame, 7.5).shape == frame.shape
    assert rotate_frame(frame, 0) is frame
