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
