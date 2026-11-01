import csv
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from levainpeak.graph_data import chart_payload, prepare_graph_data, volume_scale

COLUMNS = ["session_id", "timestamp", "mix_time", "temperature_c", "volume_fraction",
           "initial_volume_fraction", "growth_factor", "phase", "session_name"]


def write_session(tmp_path, volumes, *, baseline=0.2, step_seconds=10):
    path = tmp_path / "s.csv"
    start = datetime(2026, 10, 30, 8, 0, tzinfo=timezone(timedelta(hours=-3)))
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(COLUMNS)
        for index, volume in enumerate(volumes):
            stamp = (start + timedelta(seconds=index * step_seconds)).isoformat(timespec="seconds")
            writer.writerow(["x", stamp, "", f"{22 + math.sin(index / 50):.1f}", f"{volume:.4f}",
                             f"{baseline:.4f}", f"{volume / baseline:.3f}", "Crescendo", "Bradok"])
    return path


def growth(count, start=0.2, end=0.45):
    return [start + (end - start) / (1 + math.exp(-12 * (i / count - 0.5))) for i in range(count)]


def test_the_axis_is_the_doubling_until_it_is_passed_then_the_peak():
    assert volume_scale(0.2, [0.2, 0.3]) == (0.2, 0.4, "dobro")
    assert volume_scale(0.2, [0.2, 0.5, 0.45]) == (0.2, 0.5, "máximo atingido")
    assert volume_scale(0.6, [0.6, 0.7]) == (0.6, 1.0, "capacidade")
    assert volume_scale(None, [0.25, 0.3]) == (0.25, 0.5, "dobro")


def test_the_chart_puts_the_initial_volume_at_zero_and_the_doubling_at_one_hundred(tmp_path):
    volumes = [0.2 + 0.2 * i / 1999 for i in range(2000)]  # a slow, realistic rise to double
    chart = chart_payload(prepare_graph_data(write_session(tmp_path, volumes)))
    percents = [point[1] for point in chart["pts"]]
    assert chart["escala"] == "dobro"
    assert percents[0] == pytest.approx(0, abs=1)
    assert max(percents) == pytest.approx(100, abs=3)


def test_the_chart_is_small_and_keeps_the_newest_reading(tmp_path):
    chart = chart_payload(prepare_graph_data(write_session(tmp_path, growth(3000))))
    assert len(chart["pts"]) <= 401
    assert chart["pts"][-1][0] == 2999 * 10  # seconds since t0 of the last row
    assert len(json.dumps(chart)) < 25000  # far below the 49,000 characters of a cell


def test_the_chart_has_the_phases_in_the_graph_names(tmp_path):
    chart = chart_payload(prepare_graph_data(write_session(tmp_path, growth(1500))))
    names = {phase[0] for phase in chart["fases"]}
    assert names <= {"Aquecimento", "Crescimento", "Pico", "Declínio"} and names
    assert all(start <= end for _, start, end in chart["fases"])


def test_the_chart_is_none_while_there_is_too_little_data(tmp_path):
    assert chart_payload(prepare_graph_data(write_session(tmp_path, [0.2]))) is None
