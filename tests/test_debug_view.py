import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from levainpeak.debug_view import HEIGHT, WIDTH, build_debug_mosaic  # noqa: E402
from levainpeak.temperature import inspect_temperature, read_temperature  # noqa: E402
from test_temperature import render_display  # noqa: E402


def test_mosaic_without_a_camera_frame_still_renders():
    mosaic = build_debug_mosaic(None, None, None)
    assert mosaic.shape == (HEIGHT, WIDTH, 3) and mosaic.dtype == np.uint8


def test_mosaic_when_nothing_is_detected_does_not_fail():
    blank = np.full((1080, 1920, 3), 90, np.uint8)
    mosaic = build_debug_mosaic(blank, (654, 221, 1259, 1075), (1384, 839, 1586, 943))
    assert mosaic.shape == (HEIGHT, WIDTH, 3)


def test_mosaic_shows_the_thermometer_stages():
    frame, roi = render_display("21.8")
    mosaic = build_debug_mosaic(frame, None, roi, temperature_c=21.8)
    assert mosaic.shape == (HEIGHT, WIDTH, 3)
    thermometer_area = mosaic[HEIGHT - 96 - 250:HEIGHT - 96, 238:]
    assert thermometer_area.std() > 20  # something was drawn, not an empty panel


def test_inspection_matches_the_plain_reading():
    frame, roi = render_display("29.2")
    inspection = inspect_temperature(frame, roi)
    assert inspection.reading == read_temperature(frame, roi) == 29.2
    assert inspection.digits == (2, 9, 2)
    assert len(inspection.cells) == 3 and len(inspection.occupancy) == 3
    assert all(len(values) == 7 for values in inspection.occupancy)


def test_inspection_without_a_region_is_empty():
    inspection = inspect_temperature(np.zeros((100, 100, 3), np.uint8), None)
    assert inspection.reading is None and inspection.cells == ()


def _measurement(marker_y, manual):
    from levainpeak.vision import ContainerMeasurement

    return ContainerMeasurement(
        left=10, right=100, top=0, bottom=500, surface_y=250, volume_fraction=0.3,
        confidence=0.9, marker_y=marker_y, marker_manual=manual,
    )


def test_the_diagnostics_show_the_mark_the_app_uses_not_the_raw_detection():
    from levainpeak.debug_view import marker_rows

    raw = _measurement(779, manual=False)  # the detector sits on the middle of the dark band
    stable = _measurement(772, manual=True)  # the user put the mark at the top of the band
    assert marker_rows(raw, stable) == (772, 779, True)


def test_without_a_hand_placed_mark_the_detection_is_what_is_shown():
    from levainpeak.debug_view import marker_rows

    raw = _measurement(779, manual=False)
    assert marker_rows(raw, None) == (779, 779, False)
    assert marker_rows(raw, _measurement(779, manual=False)) == (779, 779, False)


def test_mosaic_draws_with_a_hand_placed_mark_that_differs_from_the_detection():
    import numpy as np

    from levainpeak.debug_view import HEIGHT, WIDTH, build_debug_mosaic

    frame = np.full((1080, 1920, 3), 90, np.uint8)
    mosaic = build_debug_mosaic(frame, (654, 221, 1259, 1075), None, stable=_measurement(772, True))
    assert mosaic.shape == (HEIGHT, WIDTH, 3)


@pytest.mark.gui
def test_dragging_the_picture_moves_the_diagnostics_window():
    import tkinter as tk

    import pytest

    from levainpeak.debug_view import DebugWindow

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    try:
        window = DebugWindow(root, lambda: {})
        root.update()
        label = window._label
        x0, y0 = window.window.winfo_x(), window.window.winfo_y()
        label.event_generate("<ButtonPress-1>", x=50, y=50, rootx=x0 + 50, rooty=y0 + 50)
        label.event_generate("<B1-Motion>", x=90, y=70, rootx=x0 + 90, rooty=y0 + 70, state=0x100)
        root.update()
        assert (window.window.winfo_x() - x0, window.window.winfo_y() - y0) == (40, 20)
        window.close()
    finally:
        root.destroy()


def test_the_fourth_panel_shows_the_strips_when_the_contour_is_drawn():
    from levainpeak.debug_view import GREEN, PANEL_W

    frame = np.full((1080, 1920, 3), 60, np.uint8)
    frame[640:800, 700:1300] = 200  # levain, its surface at row 640
    outline = ((700, 300), (1300, 300), (1280, 800), (720, 800))
    mosaic = build_debug_mosaic(frame, (650, 250, 1350, 850), None, outline=outline, auto_strips=True)
    panel = mosaic[22:22 + 380, 3 * (PANEL_W + 8):3 * (PANEL_W + 8) + PANEL_W]  # panel 4, under its title bar
    assert (np.all(panel == GREEN, axis=2)).sum() > 20  # the voting strips' dots


def test_the_fourth_panel_follows_the_method_that_measured():
    from levainpeak.debug_view import GREEN, PANEL_W

    frame = np.full((1080, 1920, 3), 60, np.uint8)
    frame[640:800, 700:1300] = 200
    outline = ((700, 300), (1300, 300), (1280, 800), (720, 800))
    def green(**options):
        mosaic = build_debug_mosaic(frame, (650, 250, 1350, 850), None, outline=outline, **options)
        panel = mosaic[22:22 + 380, 3 * (PANEL_W + 8):3 * (PANEL_W + 8) + PANEL_W]
        return (np.all(panel == GREEN, axis=2)).sum()

    assert green(auto_strips=True) > 20  # automatic strips measured: they are shown
    assert green(auto_strips=False) == 0  # the old central reading measured: the old panel
    assert green(auto_strips=False, surface_bands=((0.2, 0.3), (0.45, 0.55))) > 20  # drawn bands measured
