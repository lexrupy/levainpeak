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
