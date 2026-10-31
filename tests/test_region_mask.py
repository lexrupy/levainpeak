from types import SimpleNamespace

import numpy as np

from levainpeak.app import LevainPeakApp


def test_region_mask_keeps_the_whole_picture_while_drawing_a_new_region():
    frame = np.full((200, 300, 3), 200, np.uint8)
    app = SimpleNamespace(_manual_pot_roi=(10, 10, 100, 150), _temperature_roi=(120, 20, 200, 80), _roi_mode=False)
    dimmed = LevainPeakApp._apply_region_mask(app, frame)
    assert dimmed[190, 290].max() < 100  # outside the regions: dark
    app._roi_mode = True
    drawing = LevainPeakApp._apply_region_mask(app, frame)
    assert drawing[190, 290].tolist() == [200, 200, 200]  # outside the regions: as it is
    assert drawing[10, 50].tolist() != [200, 200, 200]  # the old region is still outlined
