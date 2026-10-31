import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from levainpeak.phone_config import PhoneConfig  # noqa: E402
from test_phone_push import TOKEN, FakeAppsScript  # noqa: E402

# Builds the real window: run with `pytest --gui`.
pytestmark = pytest.mark.gui


def test_the_settings_tab_starts_restarts_and_stops_the_sender(tmp_path, monkeypatch):
    import tkinter as tk

    import levainpeak.app as app_module

    monkeypatch.chdir(tmp_path)  # data/settings.json and data/sessions stay out of the real project
    monkeypatch.setattr(app_module, "load_camera_config", lambda: (_ for _ in ()).throw(FileNotFoundError("sem câmera")))
    server = FakeAppsScript()
    monkeypatch.setattr(app_module, "load_phone_config", lambda: PhoneConfig(server.url, TOKEN))
    try:
        root = tk.Tk()
    except tk.TclError:
        server.close()
        pytest.skip("no display available")
    try:
        app = app_module.LevainPeakApp(root)
        assert app._phone_pusher is None and "Desligado" in app._phone_status.cget("text")

        app._phone_enabled_var.set(True)
        app._apply_settings()
        assert app._phone_pusher is not None and app._phone_pusher.interval_seconds == 120

        app._publish_live_state()
        assert app._live_state["fase"] == app._phase and app._live_state["intervalo_s"] == 120
        assert app._phone_pusher.push_once() is True
        assert server.packets[0]["live"]["fase"] == app._phase

        first = app._phone_pusher
        app._phone_interval_var.set("240")
        app._apply_settings()
        assert app._phone_pusher is not first and app._phone_pusher.interval_seconds == 240

        app._phone_enabled_var.set(False)
        app._apply_settings()
        assert app._phone_pusher is None and "Desligado" in app._phone_status.cget("text")

        monkeypatch.setattr(app_module, "load_phone_config", lambda: (_ for _ in ()).throw(FileNotFoundError("phone.ini ausente")))
        app._phone_enabled_var.set(True)
        app._apply_settings()
        assert app._phone_pusher is None and "phone.ini ausente" in app._phone_status.cget("text")
        assert TOKEN not in app._phone_status.cget("text")
    finally:
        root.destroy()
        server.close()
