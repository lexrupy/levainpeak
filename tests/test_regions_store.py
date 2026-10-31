from levainpeak.regions_store import load_regions_file, save_regions_file


def test_the_marked_regions_survive_a_restart_without_a_session(tmp_path):
    path = tmp_path / "regions.json"
    save_regions_file(
        pot=(558, 175, 1450, 998), outline=((611, 383), (1376, 368), (1282, 924), (723, 926)),
        thermometer=(356, 545, 625, 723), face_quad=((383, 571), (599, 573), (599, 693), (382, 697)),
        marker_y=701, path=path,
    )
    saved = load_regions_file(path)
    assert saved["pot"] == (558, 175, 1450, 998) and saved["marker_y"] == 701
    assert saved["outline"][2] == (1282, 924) and saved["face_quad"][0] == (383, 571)
    assert not list(tmp_path.glob("*.tmp"))  # written whole, then moved into place


def test_empty_regions_come_back_as_none_and_a_missing_or_broken_file_as_nothing(tmp_path):
    path = tmp_path / "regions.json"
    assert load_regions_file(path) == {}
    save_regions_file(pot=None, outline=None, thermometer=None, face_quad=None, marker_y=None, path=path)
    assert load_regions_file(path) == {"pot": None, "outline": None, "thermometer": None, "face_quad": None, "marker_y": None}
    path.write_text("{ not json", encoding="utf-8")
    assert load_regions_file(path) == {}
