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
