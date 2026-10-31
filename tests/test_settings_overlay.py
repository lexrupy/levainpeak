import json

from levainpeak.settings import MAX_OVERLAY_BRIGHTNESS, MIN_OVERLAY_BRIGHTNESS, Settings, load_settings, save_settings


def test_the_overlay_brightness_is_saved_clamped_and_defaults_for_old_files(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(Settings(overlay_brightness=35), path)
    assert load_settings(path).overlay_brightness == 35
    save_settings(Settings(overlay_brightness=5), path)
    assert load_settings(path).overlay_brightness == MIN_OVERLAY_BRIGHTNESS
    save_settings(Settings(overlay_brightness=900), path)
    assert load_settings(path).overlay_brightness == MAX_OVERLAY_BRIGHTNESS
    path.write_text(json.dumps({"record_interval_seconds": 20}), encoding="utf-8")  # a file from before the option
    assert load_settings(path).overlay_brightness == 50
