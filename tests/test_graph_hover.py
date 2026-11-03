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
        _session_log=SimpleNamespace(path=path),
    )
    return app, canvas


def test_the_graph_draws_the_forecast_as_a_pale_dotted_curve_up_to_the_doubling():
    app, canvas = _graph_app(prediction=3)
    LevainPeakApp._draw_session_graph(app)
    dotted = [(coords, options) for coords, options in canvas.lines if options.get("dash") == (2, 5)]
    assert len(dotted) == 1
    coords, options = dotted[0]
    assert len(coords) == 82 and options["fill"] == "#a9c6d6"
    assert coords[-2] > coords[0] and coords[-1] < coords[1]  # it goes right, and up toward the doubling
    assert any(text.startswith("dobro ") for text in canvas.texts)


def test_the_graph_has_no_forecast_curve_without_a_forecast_or_for_another_session():
    for app, canvas in (_graph_app(prediction=None), _graph_app(prediction=3)):
        if app._forecast_prediction is not None:
            app._session_log = SimpleNamespace(path="outra.csv")  # the graph shows a different session
        LevainPeakApp._draw_session_graph(app)
        assert not [1 for _c, options in canvas.lines if options.get("dash") == (2, 5)]
