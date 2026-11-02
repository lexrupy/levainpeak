from pathlib import Path

from levainpeak.timelapse import photo_time, session_photos, step_index


def test_photos_come_from_the_session_folder_oldest_first(tmp_path):
    session = tmp_path / "20261101_121008_790003.csv"
    folder = tmp_path / "20261101_121008_790003_fotos"
    folder.mkdir()
    for name in ("20261101_130000.jpg", "20261101_121521.jpg"):
        (folder / name).write_bytes(b"")
    assert [p.name for p in session_photos(session)] == ["20261101_121521.jpg", "20261101_130000.jpg"]
    assert session_photos(None) == []
    assert session_photos(tmp_path / "outra.csv") == []


def test_the_photo_time_comes_from_its_name():
    assert photo_time(Path("20261102_080040.jpg")).hour == 8
    assert photo_time(Path("x.jpg")) is None


def test_stepping_stops_at_both_ends():
    assert step_index(0, -1, 5) == 0
    assert step_index(4, 1, 5) == 4
    assert step_index(2, 1, 5) == 3
