from types import SimpleNamespace

import numpy as np

from levainpeak.app import LevainPeakApp


def test_region_mask_keeps_the_whole_picture_while_drawing_a_new_region():
    frame = np.full((200, 300, 3), 200, np.uint8)
    app = SimpleNamespace(_manual_pot_roi=(10, 10, 100, 150), _temperature_roi=(120, 20, 200, 80), _roi_mode=False,
                         _reposition_mode=False, _outline_mode=False, _outline_points=[], _pot_outline=None,
                         _outline_target="pot", _temperature_quad=None,
                         _settings=SimpleNamespace(overlay_brightness=50))
    dimmed = LevainPeakApp._apply_region_mask(app, frame)
    assert 60 < dimmed[190, 290].max() < 140  # outside the regions: dimmed, but still readable
    app._roi_mode = True
    drawing = LevainPeakApp._apply_region_mask(app, frame)
    assert drawing[190, 290].tolist() == [200, 200, 200]  # outside the regions: as it is
    assert drawing[10, 50].tolist() != [200, 200, 200]  # the old region is still outlined


def test_the_outline_is_drawn_on_the_picture_with_handles_while_editing():
    frame = np.full((200, 300, 3), 200, np.uint8)
    points = [[60, 30], [240, 30], [210, 170], [90, 170]]
    app = SimpleNamespace(_manual_pot_roi=None, _temperature_roi=None, _roi_mode=False,
                          _reposition_mode=False, _outline_mode=True, _outline_points=points, _pot_outline=None,
                          _outline_target="pot", _temperature_quad=None,
                         _settings=SimpleNamespace(overlay_brightness=50))
    shown = LevainPeakApp._apply_region_mask(app, frame)
    assert shown[100, 150].tolist() == [200, 200, 200]  # not dimmed
    assert shown[30, 60].tolist() != [200, 200, 200]  # a corner handle
    assert LevainPeakApp._outline_is_valid(points)
    assert not LevainPeakApp._outline_is_valid([[60, 30], [240, 170], [210, 30], [90, 170]])  # crossed sides
    assert not LevainPeakApp._outline_is_valid([[10, 10], [30, 10], [30, 40], [10, 40]])  # too small


def test_the_thermometer_face_is_outlined_in_its_own_color_and_edited_on_its_own():
    frame = np.full((200, 300, 3), 200, np.uint8)
    pot = ((60, 30), (240, 30), (210, 170), (90, 170))
    face = [[20, 20], [80, 20], [80, 50], [20, 50]]
    app = SimpleNamespace(_manual_pot_roi=None, _temperature_roi=None, _roi_mode=False,
                          _reposition_mode=False, _outline_mode=True, _outline_points=face, _pot_outline=pot,
                          _outline_target="thermometer", _temperature_quad=None,
                          _settings=SimpleNamespace(overlay_brightness=50))
    shown = LevainPeakApp._apply_region_mask(app, frame)
    assert shown[20, 20].tolist() != [200, 200, 200]  # the face corner being edited
    assert shown[30, 150].tolist() != [200, 200, 200]  # the saved pot outline stays visible
    assert LevainPeakApp._outline_is_valid(face, 40 * 20)
    assert not LevainPeakApp._outline_is_valid(face)  # too small for a pot


def test_the_hole_in_the_overlay_is_the_outer_box_and_the_brightness_is_a_setting():
    frame = np.full((200, 300, 3), 200, np.uint8)
    pot_box = (50, 20, 250, 180)
    outline = ((70, 40), (230, 40), (200, 160), (100, 160))  # inside the box
    def app(brightness):
        return SimpleNamespace(_manual_pot_roi=pot_box, _temperature_roi=None, _roi_mode=False,
                               _reposition_mode=False, _outline_mode=False, _outline_points=[], _pot_outline=outline,
                               _outline_target="pot", _temperature_quad=None,
                               _settings=SimpleNamespace(overlay_brightness=brightness))
    shown = LevainPeakApp._apply_region_mask(app(30), frame)
    assert shown[100, 60].tolist() == [200, 200, 200]  # inside the outer box but outside the contour: not dimmed
    assert shown[100, 10].tolist() == [60, 60, 60]  # outside the box: 30% of 200
    assert LevainPeakApp._apply_region_mask(app(100), frame)[100, 10].tolist() == [200, 200, 200]  # 100%: nothing dimmed


def _cut_app(infrared):
    from levainpeak.settings import Settings

    analyzer = SimpleNamespace(profile=SimpleNamespace(infrared=infrared), ink_cut_infrared=0.3, ink_cut_day=40)
    return SimpleNamespace(_settings=Settings(), _analyzer=analyzer, _running_infrared=lambda: infrared)


def test_the_cut_slider_changes_only_the_running_profile_and_auto_puts_the_default_back():
    infrared = _cut_app(True)
    LevainPeakApp._set_ink_cut(infrared, 100.0, False)  # most ink
    assert infrared._settings.ink_cut_infrared == 0.1 and infrared._analyzer.ink_cut_infrared == 0.1
    assert infrared._settings.ink_cut_day == 40  # the other profile is untouched
    assert LevainPeakApp._ink_control(SimpleNamespace(**{**vars(infrared), "_temperature_quad": (1,), "_temperature_roi": None}))["auto"] is False
    LevainPeakApp._set_ink_cut(infrared, None, False)  # auto
    assert infrared._settings.ink_cut_infrared == 0.3 and infrared._analyzer.ink_cut_infrared == 0.3

    day = _cut_app(False)
    LevainPeakApp._set_ink_cut(day, 100.0, False)
    assert day._settings.ink_cut_day == 60 and day._settings.ink_cut_infrared == 0.3
    LevainPeakApp._set_ink_cut(day, None, False)
    assert day._settings.ink_cut_day == 40


def test_repositioning_moves_resizes_and_keeps_the_mark_in_proportion():
    from levainpeak.app import LevainPeakApp

    app = SimpleNamespace(
        _pot_outline=((100, 100), (300, 100), (280, 300), (120, 300)),
        _manual_pot_roi=(90, 90, 310, 310), _manual_marker_y=250,
        _reposition_center=(500.0, 400.0), _reposition_scale=2.0, _source_frame_size=(1920, 1080),
    )
    outline, roi, marker = LevainPeakApp._repositioned(app)
    xs, ys = [x for x, _ in outline], [y for _, y in outline]
    assert (min(xs) + max(xs)) / 2 == 500 and abs(max(xs) - min(xs) - 2 * 200) <= 2
    assert abs((sum(ys) / 4) - 400) <= 1
    assert marker == round(400 + 2 * (250 - 200))
    assert roi[0] < min(xs) and roi[2] > max(xs)


def test_a_drag_past_the_picture_edge_counts_as_the_edge():
    app = SimpleNamespace(_source_frame_size=(1920, 1080), _displayed_image_size=(960, 540),
                          _display_visible_size=None, _display_offsets=(10, 20), _display_crop_origin=(0, 0))
    outside = SimpleNamespace(x=2000, y=-50)
    assert LevainPeakApp._source_point(app, outside) is None
    assert LevainPeakApp._source_point(app, outside, clamp=True) == (1918, 0)
    assert LevainPeakApp._source_point(app, SimpleNamespace(x=-5, y=900), clamp=True) == (0, 1078)


def test_every_diagnostic_panel_box_lies_inside_the_mosaic():
    from levainpeak.debug_view import HEIGHT, WIDTH, panel_boxes

    boxes = panel_boxes()
    assert len(boxes) == 7
    for left, top, width, height in boxes:
        assert 0 <= left and left + width <= WIDTH and 0 <= top and top + height <= HEIGHT
