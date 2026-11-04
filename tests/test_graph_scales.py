from datetime import datetime, timedelta, timezone

from levainpeak.graph_data import chart_payload, temperature_axis, volume_scale
from levainpeak.settings import Settings, load_settings, save_settings


def test_the_default_temperature_axis_is_wide_and_in_whole_degrees():
    low, high = temperature_axis([25.4, 25.7])  # a 0.3 degree wobble
    assert high - low >= 4 and low == int(low) and high == int(high) and low < 25.4 and high > 25.7
    assert (high - low) % 4 == 0  # five ticks on whole degrees
    assert 0.3 / (high - low) < 0.1  # the wobble is a small part of the chart, not all of it


def test_the_zoom_mode_keeps_the_old_fit_to_the_data():
    low, high = temperature_axis([25.4, 25.7], "zoom")
    assert abs((high - low) - 1.0) < 1e-9 and abs((low + high) / 2 - 25.55) < 1e-9
    low, high = temperature_axis([20.0, 26.0], "zoom")
    assert low < 20.0 and high > 26.0 and high - low < 8


def test_the_wide_mode_is_wider_and_every_mode_contains_the_data():
    for values in ([25.4, 25.7], [18.1, 30.2], [21.2, 21.3], [30.0, 40.0], [24.1, 25.9]):
        for mode in ("ampla", "zoom", "larga"):
            low, high = temperature_axis(values, mode)
            assert low <= min(values) and high >= max(values), (values, mode)
    assert temperature_axis([25.4, 25.7], "larga")[1] - temperature_axis([25.4, 25.7], "larga")[0] >= 12


def test_the_volume_modes():
    volumes = [0.2, 0.25, 0.3]
    assert volume_scale(0.2, volumes)[2] == "dobro"
    assert volume_scale(0.2, volumes, "máximo") == (0.2, 0.3, "máximo atingido")
    assert volume_scale(0.2, volumes, "capacidade") == (0.2, 1.0, "capacidade")
    assert volume_scale(0.2, [0.2, 0.2], "máximo")[1] > 0.2  # no flat peak: never a zero-height axis


def _data():
    start = datetime(2026, 10, 31, 16, 0, tzinfo=timezone(timedelta(hours=-3)))
    samples = [(start + timedelta(minutes=i), 0.2 + 0.001 * i, 25.5 + 0.1 * (i % 3)) for i in range(40)]
    return {"too_few": False, "samples": samples, "baseline": 0.2, "phases": ["Crescendo"] * len(samples), "session_name": "teste"}


def test_the_phone_chart_carries_the_temperature_axis_chosen_in_the_app():
    wide = chart_payload(_data())
    assert wide["temp_eixo"][1] - wide["temp_eixo"][0] >= 4
    zoom = chart_payload(_data(), temperature_scale="zoom")
    assert zoom["temp_eixo"][1] - zoom["temp_eixo"][0] < 2
    assert chart_payload(_data(), volume_mode="capacidade")["escala"] == "capacidade"


def test_the_graph_scales_are_saved_and_a_bad_value_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(Settings(graph_temperature_scale="zoom", graph_volume_scale="máximo"), path)
    loaded = load_settings(path)
    assert (loaded.graph_temperature_scale, loaded.graph_volume_scale) == ("zoom", "máximo")
    path.write_text('{"graph_temperature_scale": "xx", "graph_volume_scale": "yy"}', encoding="utf-8")
    loaded = load_settings(path)
    assert (loaded.graph_temperature_scale, loaded.graph_volume_scale) == ("ampla", "auto")


def test_the_chart_for_the_phone_carries_the_growth_rate_series():
    import math

    start = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    samples = [(start + timedelta(seconds=30 * i), 0.20 * math.exp(0.1 * i * 30 / 3600), 25.0) for i in range(300)]
    payload = chart_payload(
        {"too_few": False, "samples": samples, "phases": ["Crescendo"] * len(samples), "baseline": 0.20, "session_name": "S"}
    )
    taxa = payload["taxa"]
    assert 20 < len(taxa) <= 120 and all(abs(value - 10.5) < 0.5 for _t, value in taxa)  # 10 % per hour, compound 10.5
    few = samples[:4]
    assert chart_payload({"too_few": False, "samples": few, "phases": ["Crescendo"] * 4, "baseline": 0.2, "session_name": "S"}).get("taxa") is None
