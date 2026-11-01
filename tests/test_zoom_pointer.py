from types import SimpleNamespace

from levainpeak.app import LevainPeakApp


def geometry(app, zoom_center, zoom):
    """The same layout _show_frame computes, for a panel and a base size."""
    panel_w, panel_h = app.video.winfo_width(), app.video.winfo_height()
    base = app._base_display_size
    full = (round(base[0] * zoom), round(base[1] * zoom))
    visible = (min(panel_w, full[0]), min(panel_h, full[1]))
    crop = (
        max(0, min(full[0] - visible[0], round((full[0] - visible[0]) * zoom_center[0]))),
        max(0, min(full[1] - visible[1], round((full[1] - visible[1]) * zoom_center[1]))),
    )
    offsets = ((panel_w - visible[0]) // 2, (panel_h - visible[1]) // 2)
    return full, visible, crop, offsets


def make_app(zoom, center):
    app = SimpleNamespace(
        video=SimpleNamespace(winfo_width=lambda: 884, winfo_height=lambda: 494),
        _base_display_size=(878, 494), _zoom=zoom, _zoom_center=center,
    )
    full, visible, crop, offsets = geometry(app, center, zoom)
    app._displayed_image_size, app._display_visible_size = full, visible
    app._display_crop_origin, app._display_offsets = crop, offsets
    return app


def source_under(app, x, y):
    full, _, crop, offsets = geometry(app, app._zoom_center, app._zoom)
    return (x - offsets[0] + crop[0]) / full[0], (y - offsets[1] + crop[1]) / full[1]


def test_the_point_under_the_mouse_stays_under_the_mouse():
    for start_zoom, center in ((1.0, (0.5, 0.5)), (1.5625, (0.2, 0.7))):
        for x, y in ((100, 80), (600, 400), (440, 250)):
            app = make_app(start_zoom, center)
            before = source_under(app, x, y)
            LevainPeakApp._zoom_around_pointer(app, SimpleNamespace(x=x, y=y), start_zoom * 1.25)
            after = source_under(app, x, y)
            assert abs(before[0] - after[0]) < 0.004 and abs(before[1] - after[1]) < 0.004


def test_zoom_one_centers_the_picture():
    app = make_app(2.0, (0.1, 0.9))
    LevainPeakApp._zoom_around_pointer(app, SimpleNamespace(x=10, y=10), 1.0)
    assert app._zoom == 1.0 and app._zoom_center == (0.5, 0.5)
