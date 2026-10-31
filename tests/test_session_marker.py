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


def test_a_new_session_records_the_current_volume_geometry(tmp_path):
    from levainpeak.sessions import GEOMETRY_VERSION

    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions(None, None)
    saved = json.loads(log.metadata_path.read_text(encoding="utf-8"))
    assert saved["geometry"] == GEOMETRY_VERSION
    log.stop()


def test_an_older_session_file_is_version_one_until_it_is_brought_up_to_date(tmp_path):
    from levainpeak.sessions import GEOMETRY_VERSION

    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((1, 2, 3, 4), None)
    log.save_marker(772)
    path = log.path
    log.record(0.0, _row(0.3), "Crescendo")
    log.stop()
    metadata = path.with_suffix(".json")
    saved = json.loads(metadata.read_text(encoding="utf-8"))
    del saved["geometry"]  # what a session written before the version existed looks like
    metadata.write_text(json.dumps(saved), encoding="utf-8")

    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.geometry == 1 and resumed.manual_marker_y == 772
    resumed.save_geometry(GEOMETRY_VERSION)
    assert json.loads(metadata.read_text(encoding="utf-8"))["geometry"] == GEOMETRY_VERSION
    assert json.loads(metadata.read_text(encoding="utf-8"))["marker_y"] == 772  # nothing else lost
    resumed.stop()

    again = SessionLog(tmp_path)
    again.resume(path)
    assert again.geometry == GEOMETRY_VERSION
    again.stop()


def test_the_drawn_pot_outline_is_saved_and_comes_back_on_resume(tmp_path):
    outline = ((10, 20), (300, 20), (260, 400), (50, 400))
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((0, 0, 320, 420), None, outline=outline)
    log.save_marker(300)  # saving something else must not lose the outline
    path = log.path
    log.record(0.0, _row(0.3), "Crescendo")
    log.stop()
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["pot_outline"] == [list(p) for p in outline]
    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.pot_outline == outline
    resumed.save_regions((0, 0, 320, 420), None, outline=None)  # back to automatic detection
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["pot_outline"] is None
    resumed.stop()


def test_the_thermometer_face_corners_are_saved_and_come_back_on_resume(tmp_path):
    quad = ((40, 50), (200, 60), (195, 130), (38, 120))
    outline = ((10, 20), (300, 20), (260, 400), (50, 400))
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((0, 0, 320, 420), (30, 40, 210, 140), outline=outline, face_quad=quad)
    log.save_regions((0, 0, 320, 420), (30, 40, 210, 140), outline=outline)  # leaving the quad out keeps it
    path = log.path
    log.record(0.0, _row(0.3), "Crescendo")
    log.stop()
    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.face_quad == quad and resumed.pot_outline == outline
    resumed.stop()
