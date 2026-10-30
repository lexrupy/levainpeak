from levainpeak.settings import Settings, load_settings, save_settings


def test_defaults_when_missing(tmp_path):
    assert load_settings(tmp_path / "none.json").record_interval_seconds == 10


def test_round_trip_and_clamping(tmp_path):
    path = tmp_path / "s.json"
    save_settings(Settings(record_interval_seconds=500), path)
    assert load_settings(path).record_interval_seconds == 120
    save_settings(Settings(record_interval_seconds=45), path)
    assert load_settings(path).record_interval_seconds == 45


def test_corrupt_file_falls_back(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{oops", encoding="utf-8")
    assert load_settings(path).record_interval_seconds == 10


def test_latest_session_csv_picks_the_newest_file(tmp_path):
    import os

    from levainpeak.sessions import latest_session_csv

    assert latest_session_csv(tmp_path) is None
    old, new = tmp_path / "a.csv", tmp_path / "b.csv"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    (tmp_path / "c.json").write_text("{}")
    assert latest_session_csv(tmp_path) == new
