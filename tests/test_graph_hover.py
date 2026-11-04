from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from levainpeak.app import LevainPeakApp


class FakeCanvas:
    def __init__(self):
        self.texts = []

    def delete(self, *_):
        pass

    def create_line(self, *_a, **_k):
        pass

    def create_oval(self, *_a, **_k):
        pass

    def create_text(self, *_a, text="", **_k):
        self.texts.append(text)
        return 1

    def create_rectangle(self, *_a, **_k):
        return 2

    def bbox(self, _item):
        return (0, 0, 60, 40)

    def coords(self, *_a):
        pass

    def tag_lower(self, *_a):
        pass


def test_the_hover_names_the_time_the_growth_factor_and_the_temperature_of_the_nearest_reading():
    start = datetime(2026, 11, 2, 8, 0)
    samples = [(start + timedelta(minutes=10 * i), 0.20 + 0.02 * i, 25.0 + i) for i in range(4)]
    canvas = FakeCanvas()
    app = SimpleNamespace(
        _graph_canvas=canvas,
        _graph_hover={
            "samples": samples, "xs": [100, 200, 300, 400], "initial": 0.20,
            "y_volume": lambda v: 100.0, "y_temperature": lambda t: 120.0, "bounds": (50, 450, 50, 300),
        },
    )
    LevainPeakApp._graph_hover_at(app, SimpleNamespace(x=290, y=150))
    assert canvas.texts == ["02/11 08:20\n1.20x\n27.0 °C"]
    canvas.texts.clear()
    LevainPeakApp._graph_hover_at(app, SimpleNamespace(x=10, y=150))  # outside the plot
    assert canvas.texts == []


class RecordingCanvas(FakeCanvas):
    def __init__(self):
        super().__init__()
        self.lines = []

    def create_line(self, *coords, **options):
        self.lines.append((coords, options))

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return 900

    def winfo_height(self):
        return 500

    def tag_bind(self, *_a, **_k):
        pass

    def configure(self, **_k):
        pass


def _graph_app(prediction, path="sessao.csv"):
    from levainpeak.settings import Settings

    start = datetime(2026, 11, 2, 8, 0).astimezone()
    samples = [(start + timedelta(minutes=10 * i), 0.20 + 0.01 * i, 25.0) for i in range(12)]
    canvas = RecordingCanvas()
    app = SimpleNamespace(
        _graph_redraw_job=None, _graph_canvas=canvas, _graph_path=path, _settings=Settings(),
        _graph_data=lambda _path: {"session_name": "S", "too_few": False, "samples": samples, "phases": [], "baseline": 0.20},
        _draw_phase_markers=lambda *a, **k: None, _cycle_graph_scale=lambda *_: None,
        _forecast_prediction=None if prediction is None else samples[-1][0] + timedelta(hours=prediction),
        _session_log=SimpleNamespace(path=path), _toggle_graph_option=lambda *_: None,
    )
    app._graph_forecast_time = lambda _path: app._forecast_prediction
    return app, canvas


def test_the_graph_draws_the_forecast_as_a_pale_dotted_curve_up_to_the_doubling():
    app, canvas = _graph_app(prediction=3)
    LevainPeakApp._draw_session_graph(app)
    dotted = [(coords, options) for coords, options in canvas.lines if options.get("dash") == (2, 5) and len(coords) > 10]
    assert len(dotted) == 1
    coords, options = dotted[0]
    assert len(coords) == 122 and options["fill"] == "#a9c6d6"
    assert coords[-2] > coords[0] and coords[-1] < coords[1]  # it goes right, and up toward the doubling
    assert any(text.startswith("dobro ") for text in canvas.texts)


def test_the_graph_has_no_forecast_curve_without_a_forecast_or_when_it_is_switched_off():
    off, off_canvas = _graph_app(prediction=3)
    off._settings = replace(off._settings, graph_show_forecast=False)
    for app, canvas in (_graph_app(prediction=None), (off, off_canvas)):
        LevainPeakApp._draw_session_graph(app)
        assert not [1 for _c, options in canvas.lines if options.get("dash") == (2, 5) and len(_c) > 10]


def test_the_forecast_curve_stays_over_the_measured_one_after_the_doubling_time():
    app, canvas = _graph_app(prediction=-0.1)  # the forecast time is already past
    LevainPeakApp._draw_session_graph(app)
    dotted = [coords for coords, options in canvas.lines if options.get("dash") == (2, 5) and len(coords) > 10]
    assert len(dotted) == 1


def test_the_graph_starts_at_the_mix_and_marks_where_the_monitoring_began():
    app, canvas = _graph_app(prediction=None)
    data = app._graph_data("x")
    mix = data["samples"][0][0] - timedelta(hours=2)
    app._graph_data = lambda _path: {**data, "mix_time": mix}
    LevainPeakApp._draw_session_graph(app)
    assert "início do monitoramento" in canvas.texts
    assert mix.strftime("%d/%m\n%H:%M") in canvas.texts  # the first time on the axis is the mix, not the first reading
    pale = [coords for coords, options in canvas.lines if options.get("fill") == "#a9c6d6" and len(coords) == 4]
    assert pale and pale[0][0] < pale[0][2]  # joined from the zero of the mix to the first reading


def test_the_stretch_before_the_monitoring_can_be_switched_off():
    app, canvas = _graph_app(prediction=None)
    data = app._graph_data("x")
    mix = data["samples"][0][0] - timedelta(hours=2)
    app._graph_data = lambda _path: {**data, "mix_time": mix}
    app._settings = replace(app._settings, graph_show_start=False)
    LevainPeakApp._draw_session_graph(app)
    assert "início do monitoramento" not in canvas.texts
    assert data["samples"][0][0].strftime("%d/%m\n%H:%M") in canvas.texts  # the axis starts at the first reading


def test_the_growth_rate_gets_a_strip_under_the_plot_that_can_be_switched_off_or_left_out_when_short():
    app, canvas = _graph_app(prediction=None)
    start = datetime(2026, 11, 2, 8, 0).astimezone()
    dense = [(start + timedelta(seconds=30 * i), 0.20 * 1.0001 ** i, 25.0) for i in range(240)]  # two hours
    app._graph_data = lambda _path: {"session_name": "S", "too_few": False, "samples": dense, "phases": [], "baseline": 0.20}
    LevainPeakApp._draw_session_graph(app)
    assert "Taxa de crescimento (%/h, últimos 15 min)" in canvas.texts
    green = [coords for coords, options in canvas.lines if options.get("fill") == "#2f8a5f" and len(coords) > 10]
    assert green  # the rate curve itself
    assert app._graph_hover["rates"]  # and the hover can read it

    off, off_canvas = _graph_app(prediction=None)
    off._settings = replace(off._settings, graph_show_rate=False)
    LevainPeakApp._draw_session_graph(off)
    assert "Taxa de crescimento (%/h, últimos 15 min)" not in off_canvas.texts

    short, short_canvas = _graph_app(prediction=None)
    short_canvas.winfo_height = lambda: 330  # too low for two plots
    LevainPeakApp._draw_session_graph(short)
    assert "Taxa de crescimento (%/h, últimos 15 min)" not in short_canvas.texts
