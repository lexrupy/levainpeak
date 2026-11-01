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


def test_phone_options_default_to_off_and_two_minutes(tmp_path):
    settings = load_settings(tmp_path / "none.json")
    assert (settings.phone_enabled, settings.phone_interval_seconds, settings.phone_snapshot) == (False, 120, False)


def test_phone_options_round_trip_and_clamp(tmp_path):
    path = tmp_path / "s.json"
    save_settings(Settings(phone_enabled=True, phone_interval_seconds=240, phone_snapshot=True), path)
    loaded = load_settings(path)
    assert (loaded.phone_enabled, loaded.phone_interval_seconds, loaded.phone_snapshot) == (True, 240, True)
    save_settings(Settings(phone_interval_seconds=5), path)
    assert load_settings(path).phone_interval_seconds == 60
    save_settings(Settings(phone_interval_seconds=100000), path)
    assert load_settings(path).phone_interval_seconds == 900


def test_a_file_from_before_the_phone_options_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"record_interval_seconds": 30}', encoding="utf-8")
    settings = load_settings(path)
    assert settings.record_interval_seconds == 30 and settings.phone_enabled is False


def test_rotation_is_kept_within_a_half_turn_either_way():
    from levainpeak.settings import Settings
    assert Settings(rotation=12.34).clamped().rotation == 12.3
    assert Settings(rotation=400).clamped().rotation == 195.0
