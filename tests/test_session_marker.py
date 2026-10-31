import json

from levainpeak.sessions import SessionLog


def _row(volume):
    return type(
        "Measurement", (),
        {"volume_fraction": volume, "initial_volume_fraction": 0.2, "growth_factor": volume / 0.2},
    )()


def test_the_hand_placed_mark_is_saved_with_the_session_and_comes_back(tmp_path):
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((1, 2, 3, 4), (5, 6, 7, 8))
    log.save_vessel_bounds({"dia": (313, 930)})
    log.save_marker(790)
    path = log.path
    log.record(0.0, _row(0.3), "Crescendo")
    log.stop()
    saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["marker_y"] == 790
    assert saved["pot"] == [1, 2, 3, 4] and saved["vessel_bounds"] == {"dia": [313, 930]}  # nothing else lost
    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.manual_marker_y == 790
    assert resumed.regions["thermometer"] == (5, 6, 7, 8)
    resumed.stop()


def test_going_back_to_the_automatic_mark_is_saved_too(tmp_path):
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions(None, None)
    log.save_marker(790)
    log.save_marker(None)
    path = log.path
    log.record(0.0, _row(0.3), "Crescendo")
    log.stop()
    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.manual_marker_y is None
    resumed.stop()


def test_saving_regions_again_keeps_the_mark(tmp_path):
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((1, 2, 3, 4), None)
    log.save_marker(790)
    log.save_regions((9, 9, 9, 9), None)  # the user redraws the pot region
    saved = json.loads(log.metadata_path.read_text(encoding="utf-8"))
    assert saved["marker_y"] == 790
    log.stop()
