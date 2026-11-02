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
