from levainpeak.sessions import SessionLog




def test_a_snapshot_is_kept_beside_the_session_every_ten_minutes(tmp_path):
    import numpy as np

    log = SessionLog(tmp_path)
    log.start()
    frame = np.zeros((40, 60, 3), np.uint8)
    first = log.snapshot(0.0, frame)
    assert first is not None and first.exists() and first.parent.name.endswith("_fotos")
    assert log.snapshot(599.0, frame) is None
    assert log.snapshot(600.0, frame) is not None
    assert log.snapshot(601.0, None) is None


def test_the_last_volume_written_is_kept_for_a_pause(tmp_path):
    from datetime import datetime

    from levainpeak.sessions import SessionLog
    from levainpeak.vision import ContainerMeasurement

    log = SessionLog(tmp_path)
    log.start(datetime.now().astimezone(), "t")
    assert log.last_volume is None
    measurement = ContainerMeasurement(
        left=0, right=1, top=0, bottom=1, surface_y=0, volume_fraction=0.37, confidence=1.0
    )
    log.record(0.0, measurement, "Aquecendo")
    assert log.last_volume == 0.37
    log.stop()


def test_the_snapshot_interval_is_kept_with_the_session_and_in_the_settings(tmp_path):
    from levainpeak.settings import Settings, load_settings, save_settings

    log = SessionLog(tmp_path)
    log.start()
    log.save_snapshot_seconds(300)
    import json

    assert json.loads(log.metadata_path.read_text())["snapshot_seconds"] == 300.0

    path = tmp_path / "settings.json"
    save_settings(Settings(snapshot_minutes=3), path)
    assert load_settings(path).snapshot_minutes == 3
    save_settings(Settings(snapshot_minutes=999), path)
    assert load_settings(path).snapshot_minutes == 120


def test_the_regions_and_the_last_forecast_survive_a_resume(tmp_path):
    from datetime import datetime

    from levainpeak.sessions import SessionLog

    log = SessionLog(tmp_path)
    log.start(datetime.now().astimezone(), "t")
    log.save_regions((1, 2, 300, 400), (1532, 133, 1909, 353), face_quad=((1632, 180), (1808, 154), (1816, 293), (1636, 311)))
    at = datetime.now().astimezone()
    log.save_forecast(at, "início marcado")
    from levainpeak.vision import ContainerMeasurement

    log.record(0.0, ContainerMeasurement(
        left=0, right=1, top=0, bottom=1, surface_y=0, volume_fraction=0.37, confidence=1.0
    ), "Aquecendo")
    path = log.path
    log.stop()
    again = SessionLog(tmp_path)
    _, regions = again.resume(path)
    assert regions["thermometer"] == (1532, 133, 1909, 353)
    assert again.last_forecast == {"at": at.isoformat(), "basis": "início marcado"}


def test_saving_the_regions_keeps_the_snapshot_interval_and_the_last_forecast(tmp_path):
    import json
    from datetime import datetime

    log = SessionLog(tmp_path)
    log.start(datetime.now().astimezone(), "t")
    log.save_snapshot_seconds(300)
    log.save_forecast(datetime.now().astimezone(), "início marcado")
    log.save_regions((1, 2, 3, 4), (5, 6, 7, 8))
    saved = json.loads(log.metadata_path.read_text())
    assert saved["snapshot_seconds"] == 300.0 and saved["last_forecast"]["basis"] == "início marcado"


def test_reached_at_and_repeats(tmp_path):
    from types import SimpleNamespace
    log = SessionLog(tmp_path, sample_seconds=0)
    log.start()

    def row(factor):
        return SimpleNamespace(volume_fraction=0.4, initial_volume_fraction=0.2, growth_factor=factor)

    for factor in (1.5, 1.99, 2.01, 2.012, 2.013):
        log.record(0.0, row(factor), "Crescendo", force=True)
    assert log.reached_at(2.0) is not None and log.reached_at(3.0) is None
    assert log.growth_repeats == 3 and log.last_growth_factor == 2.013


def test_phone_choice_goes_with_the_session(tmp_path):
    from types import SimpleNamespace
    log = SessionLog(tmp_path, sample_seconds=0)
    log.start()
    log.record(0.0, SimpleNamespace(volume_fraction=0.4, initial_volume_fraction=0.2, growth_factor=2.0), "Crescendo", force=True)
    assert log.phone is None
    log.save_phone(False, 300, True)
    log.stop()
    again = SessionLog(tmp_path, sample_seconds=0)
    again.resume(log.path)
    assert again.phone == {"enabled": False, "interval_seconds": 300, "snapshot": True}


def test_find_session_csv_by_name(tmp_path):
    import json, os
    from levainpeak.sessions import find_session_csv

    for index, name in enumerate(("Bradok 2L", "Bradok 2L", "Outro")):
        csv_path = tmp_path / f"20261102_00000{index}_{index}.csv"
        csv_path.write_text("a\n", encoding="utf-8")
        csv_path.with_suffix(".json").write_text(json.dumps({"session_name": name}), encoding="utf-8")
        os.utime(csv_path, (1000 + index, 1000 + index))
    assert find_session_csv("bradok 2l", tmp_path).stem.endswith("_1")  # the newer of the two
    assert find_session_csv("outro", tmp_path).stem.endswith("_2")
    assert find_session_csv("brad", tmp_path).stem.endswith("_1")  # part of a name
    assert find_session_csv("20261102_000000_0", tmp_path).stem.endswith("_0")  # the file name
    assert find_session_csv("nada", tmp_path) is None


def test_rotation_goes_with_the_session(tmp_path):
    from types import SimpleNamespace
    log = SessionLog(tmp_path, sample_seconds=0)
    log.start()
    log.record(0.0, SimpleNamespace(volume_fraction=0.4, initial_volume_fraction=0.2, growth_factor=2.0), "Crescendo", force=True)
    assert log.rotation is None
    log.save_rotation(-0.2)
    log.stop()
    again = SessionLog(tmp_path, sample_seconds=0)
    again.resume(log.path)
    assert again.rotation == -0.2


def test_clear_forecast_removes_it_from_the_session_file(tmp_path):
    from datetime import datetime
    from types import SimpleNamespace
    log = SessionLog(tmp_path, sample_seconds=0)
    log.start()
    log.record(0.0, SimpleNamespace(volume_fraction=0.4, initial_volume_fraction=0.2, growth_factor=2.0), "Crescendo", force=True)
    log.save_forecast(datetime.now().astimezone(), "início marcado")
    log.clear_forecast()
    log.stop()
    again = SessionLog(tmp_path, sample_seconds=0)
    again.resume(log.path)
    assert again.last_forecast is None


def test_ratio_is_read_from_text_and_kept_with_the_session(tmp_path):
    from types import SimpleNamespace
    from levainpeak.sessions import parse_ratio
    assert parse_ratio("Bdk 1:5:5 02/11 22:26") == (1.0, 5.0, 5.0)
    assert parse_ratio("Levain 1 : 2,5 : 3") == (1.0, 2.5, 3.0)
    assert parse_ratio("Levain 02/11 22:26") is None and parse_ratio("1:0:5") is None
    log = SessionLog(tmp_path, sample_seconds=0)
    log.start()
    log.record(0.0, SimpleNamespace(volume_fraction=0.4, initial_volume_fraction=0.2, growth_factor=2.0), "Crescendo", force=True)
    log.save_ratio((1, 10, 10))
    log.stop()
    again = SessionLog(tmp_path, sample_seconds=0)
    again.resume(log.path)
    assert again.ratio == (1.0, 10.0, 10.0)


def test_a_repeated_volume_reading_is_flagged():
    from types import SimpleNamespace
    from levainpeak.app import LevainPeakApp

    measurement = SimpleNamespace(volume_fraction=0.5, growth_factor=3.04)
    assert "repetida" not in LevainPeakApp._volume_text(measurement, 3.02, 1)
    assert LevainPeakApp._volume_text(measurement, 3.02, 2).endswith("anterior 3.02× (2x) ⚠ repetida")
