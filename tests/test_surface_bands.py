import numpy as np

from levainpeak.vision import _find_surface_from_below, _find_surface_in_bands


def _jar_with_a_pale_intruder():
    gray = np.full((400, 400), 60, np.uint8)  # dim glass
    gray[300:, :] = 200  # the levain, surface at row 300
    gray[150:300, 100:220] = 210  # a label corner climbing the glass, over most of the middle half
    return gray


def test_bands_in_clean_light_read_the_surface_the_whole_width_misses():
    gray = _jar_with_a_pale_intruder()
    whole = _find_surface_from_below(gray, 0, 400, 0, 400)
    assert whole is not None and whole[0] < 200  # taken in by the intruder
    bands = _find_surface_in_bands(gray, 0, 400, 0, 400, False, ((0.55, 0.65), (0.7, 0.8)))
    assert bands is not None and abs(bands[0] - 300) <= 4 and bands[1] == 1.0


def test_one_band_is_not_enough_and_the_median_ignores_the_odd_one():
    gray = _jar_with_a_pale_intruder()
    assert _find_surface_in_bands(gray, 0, 400, 0, 400, False, ((0.7, 0.8),)) is None
    three = ((0.55, 0.65), (0.7, 0.8), (0.3, 0.4))  # the last sits over the intruder
    level, agreement = _find_surface_in_bands(gray, 0, 400, 0, 400, False, three)
    assert abs(level - 300) <= 4 and abs(agreement - 2 / 3) < 1e-9


def _app(bands=(), outline=((100, 100), (500, 100), (480, 400), (120, 400)), use=False):
    from types import SimpleNamespace

    from levainpeak.settings import Settings

    app = SimpleNamespace(
        _settings=Settings(surface_bands=bands, use_surface_bands=use), _pot_outline=outline,
        _source_frame_size=(1920, 1080), messages=[], saved=[],
    )
    app._set_message = app.messages.append
    app._set_bands = lambda new, use=None: app.saved.append(new)
    return app


def test_a_drawn_band_is_stored_as_fractions_of_the_glass_width():
    from levainpeak.app import LevainPeakApp

    app = _app()
    LevainPeakApp._add_band(app, 200, 240)  # the glass runs from x=120 to 480 at its narrowest
    (start, end), = app.saved[0]
    assert abs(start - (200 - 120) / 360) < 0.01 and abs(end - (240 - 120) / 360) < 0.01
    assert "pelo menos mais uma" in app.messages[-1]


def test_no_more_than_the_maximum_of_bands_and_none_without_a_contour():
    from levainpeak.app import LevainPeakApp
    from levainpeak.vision import MAX_SURFACE_BANDS

    full = _app(bands=tuple((i / 10, i / 10 + 0.05) for i in range(MAX_SURFACE_BANDS)))
    LevainPeakApp._add_band(full, 200, 240)
    assert not full.saved and "No máximo" in full.messages[-1]
    bare = _app(outline=None)
    LevainPeakApp._add_band(bare, 200, 240)
    assert not bare.saved and "contorno" in bare.messages[-1]


def test_the_bands_are_used_only_when_switched_on_and_at_least_two():
    from levainpeak.app import LevainPeakApp

    assert LevainPeakApp._active_bands(_app(bands=((0.1, 0.2), (0.5, 0.6)), use=True)) == ((0.1, 0.2), (0.5, 0.6))
    assert LevainPeakApp._active_bands(_app(bands=((0.1, 0.2), (0.5, 0.6)), use=False)) == ()
    assert LevainPeakApp._active_bands(_app(bands=((0.1, 0.2),), use=True)) == ()


def test_the_settings_keep_clean_sorted_bands_and_the_analyzer_key_changes_with_them(tmp_path):
    from levainpeak.analysis import ContainerAnalyzer
    from levainpeak.settings import Settings, load_settings, save_settings

    path = tmp_path / "s.json"
    save_settings(Settings(surface_bands=((0.6, 0.7), (-1, 0.2), (0.4, 0.4), (0.3, 0.35)), use_surface_bands=True), path)
    loaded = load_settings(path)
    assert loaded.surface_bands == ((0.0, 0.2), (0.3, 0.35), (0.6, 0.7)) and loaded.use_surface_bands
    analyzer = ContainerAnalyzer(stream=None)
    before = analyzer.region_key()
    analyzer.surface_bands = loaded.surface_bands
    assert analyzer.region_key() != before


def test_switching_the_bands_joins_the_series_at_the_level_it_had():
    from levainpeak.analysis import ContainerAnalyzer

    analyzer = ContainerAnalyzer(stream=None)
    analyzer.join_enabled = True
    analyzer._last_volume = 0.30
    analyzer.set_surface_bands(((0.4, 0.5), (0.6, 0.7)))
    assert analyzer.anchor_volume == 0.30 and analyzer._last_volume is None
    analyzer._last_volume = 0.31
    analyzer.set_surface_bands(((0.4, 0.5), (0.6, 0.7)))  # nothing changed: nothing to join
    assert analyzer._last_volume == 0.31


def test_a_thin_band_of_a_few_pixels_is_taken_and_a_sliver_is_not():
    from levainpeak.app import LevainPeakApp

    thin = _app()
    LevainPeakApp._add_band(thin, 300, 307)  # 7 px of a 360 px glass: about 2%
    assert len(thin.saved) == 1
    sliver = _app()
    LevainPeakApp._add_band(sliver, 300, 302)
    assert not sliver.saved and "estreita" in sliver.messages[-1]


def _app_with_bands(bands, zoom=1.0):
    app = _app(bands=bands)
    app._zoom = zoom
    app._band_drag = None
    app._band_override = None
    app._band_span = lambda: (120, 480, 100, 400)  # the glass is 360 px wide
    app._band_at = lambda x: __import__("levainpeak.app", fromlist=["x"]).LevainPeakApp._band_at(app, x)
    app._moved_bands = lambda x: __import__("levainpeak.app", fromlist=["x"]).LevainPeakApp._moved_bands(app, x)
    return app


def test_the_band_under_the_mouse_is_found_and_a_miss_finds_none():
    from levainpeak.app import LevainPeakApp

    app = _app_with_bands(((0.25, 0.30), (0.70, 0.75)))
    assert LevainPeakApp._band_at(app, 120 + int(0.27 * 360)) == 0
    assert LevainPeakApp._band_at(app, 120 + int(0.72 * 360)) == 1
    assert LevainPeakApp._band_at(app, 120 + int(0.50 * 360)) is None


def test_a_dragged_band_moves_inside_the_glass_and_a_click_removes_it():
    from levainpeak.app import LevainPeakApp

    app = _app_with_bands(((0.25, 0.30), (0.70, 0.75)))
    app._band_drag = (0, 200, (0.25, 0.30))
    (start, end), _other = LevainPeakApp._moved_bands(app, 200 + 36)  # 36 px = 10% of the glass
    assert abs(start - 0.35) < 1e-9 and abs(end - 0.40) < 1e-9
    (start, end), _ = LevainPeakApp._moved_bands(app, 200 - 1000)  # far past the left wall
    assert start == 0.0 and abs(end - 0.05) < 1e-9
    LevainPeakApp._finish_band_move(app, 201)  # a click, hardly any travel
    assert app.saved[-1] == ((0.70, 0.75),)
    app._band_drag = (0, 200, (0.25, 0.30))
    LevainPeakApp._finish_band_move(app, 236)
    assert abs(app.saved[-1][0][0] - 0.35) < 1e-9


def test_the_bands_show_on_the_picture_only_while_drawing_or_when_always_shown():
    import numpy as np
    from types import SimpleNamespace

    from levainpeak.app import LevainPeakApp
    from levainpeak.settings import Settings

    frame = np.full((1080, 1920, 3), 90, np.uint8)
    outline = ((700, 300), (1300, 300), (1250, 800), (750, 800))

    def shown(show, roi_mode, target="bands"):
        app = SimpleNamespace(
            _settings=Settings(surface_bands=((0.2, 0.3), (0.6, 0.7)), show_surface_bands=show),
            _pot_outline=outline, _band_override=None, _roi_mode=roi_mode, _roi_target=target,
            _active_bands=lambda: (),
        )
        return LevainPeakApp._draw_bands(app, frame)

    assert (shown(False, False) == frame).all()  # idle: nothing over the picture
    assert (shown(False, True, "pot") == frame).all()  # drawing something else
    assert not (shown(False, True) == frame).all()  # the band draw mode shows them
    assert not (shown(True, False) == frame).all()  # or the option does


def test_the_strips_vote_for_the_lowest_supported_surface_and_drop_a_label_corner():
    from levainpeak.surface_strips import read_strips

    gray = np.full((400, 400), 60, np.uint8)
    gray[300:, :] = 200  # the levain, surface at row 300
    gray[200:300, 40:120] = 210  # a label corner standing up on the left
    reading = read_strips(gray, 0, 400, 0, 400)
    assert reading.level is not None and abs(reading.level - 300) < 3 and not reading.dome
    corner = [kept for x, kept in zip(reading.xs, reading.kept) if 40 <= x < 120]
    assert corner and not any(corner)
    assert reading.confidence > 0.6


def test_a_smooth_arch_is_a_dome_and_its_level_is_the_mean_of_the_wall_and_the_top():
    from levainpeak.surface_strips import read_strips

    gray = np.full((400, 400), 60, np.uint8)
    for x in range(400):
        surface = 310 - int(round(12 * (1 - ((x - 200) / 200) ** 2)))  # 12 px higher in the middle
        gray[surface:, x] = 200
    reading = read_strips(gray, 0, 400, 0, 400)
    assert reading.dome and reading.level is not None and 301 < reading.level < 309
