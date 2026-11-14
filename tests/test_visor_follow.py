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


def test_rotate_visor_follows_the_picture():
    import cv2
    import numpy as np
    from levainpeak.stream import rotate_frame
    from levainpeak.visor_follow import rotate_visor

    frame = np.zeros((200, 300, 3), np.uint8)
    cv2.rectangle(frame, (100, 40), (140, 60), (255, 255, 255), -1)
    box = (100, 40, 140, 60)
    quad = ((100, 40), (140, 40), (140, 60), (100, 60))
    for old, new in ((0, 20), (0, 90), (10, 100.5), (90, 0), (0, -180)):
        start = rotate_frame(frame, old)
        roi, quad_now = rotate_visor(box, quad, 0, old, (300, 200))
        roi_new, quad_new = rotate_visor(roi, quad_now, old, new, (start.shape[1], start.shape[0]))
        ys, xs = np.nonzero(rotate_frame(frame, new)[:, :, 0] > 128)
        centre = ((roi_new[0] + roi_new[2]) / 2, (roi_new[1] + roi_new[3]) / 2)
        assert abs(centre[0] - xs.mean()) < 3 and abs(centre[1] - ys.mean()) < 3
        roi_box, _ = rotate_visor(roi, None, old, new, (start.shape[1], start.shape[0]))
        assert abs((roi_box[0] + roi_box[2]) / 2 - xs.mean()) < 3
