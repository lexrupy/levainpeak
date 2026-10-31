from types import SimpleNamespace

import numpy as np

from levainpeak.app import LevainPeakApp


def test_region_mask_keeps_the_whole_picture_while_drawing_a_new_region():
    frame = np.full((200, 300, 3), 200, np.uint8)
    app = SimpleNamespace(_manual_pot_roi=(10, 10, 100, 150), _temperature_roi=(120, 20, 200, 80), _roi_mode=False,
                         _outline_mode=False, _outline_points=[], _pot_outline=None)
    dimmed = LevainPeakApp._apply_region_mask(app, frame)
    assert dimmed[190, 290].max() < 100  # outside the regions: dark
    app._roi_mode = True
    drawing = LevainPeakApp._apply_region_mask(app, frame)
    assert drawing[190, 290].tolist() == [200, 200, 200]  # outside the regions: as it is
    assert drawing[10, 50].tolist() != [200, 200, 200]  # the old region is still outlined


def test_the_outline_is_drawn_on_the_picture_with_handles_while_editing():
    frame = np.full((200, 300, 3), 200, np.uint8)
    points = [[60, 30], [240, 30], [210, 170], [90, 170]]
    app = SimpleNamespace(_manual_pot_roi=None, _temperature_roi=None, _roi_mode=False,
                          _outline_mode=True, _outline_points=points, _pot_outline=None)
    shown = LevainPeakApp._apply_region_mask(app, frame)
    assert shown[100, 150].tolist() == [200, 200, 200]  # not dimmed
    assert shown[30, 60].tolist() != [200, 200, 200]  # a corner handle
    assert LevainPeakApp._outline_is_valid(points)
    assert not LevainPeakApp._outline_is_valid([[60, 30], [240, 170], [210, 30], [90, 170]])  # crossed sides
    assert not LevainPeakApp._outline_is_valid([[10, 10], [30, 10], [30, 40], [10, 40]])  # too small
