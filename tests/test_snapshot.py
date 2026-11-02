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
