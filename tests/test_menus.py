from types import SimpleNamespace

from levainpeak.sessions import describe_session, list_sessions


def _session(directory, stem, name, rows, ended=None):
    import json

    (directory / f"{stem}.csv").write_text(
        "session_id,timestamp,mix_time\\n".replace("\\n", "\n")
        + "".join(f"{stem},{stamp},x\n" for stamp in rows), encoding="utf-8",
    )
    metadata = {"session_name": name}
    if ended:
        metadata["ended"] = ended
    (directory / f"{stem}.json").write_text(json.dumps(metadata), encoding="utf-8")


def test_the_session_list_is_newest_first_with_name_span_and_end(tmp_path):
    _session(tmp_path, "20261107_165646_1", "Levain A", ["2026-11-07T16:56:46-03:00", "2026-11-07T23:10:58-03:00"], ended="x")
    _session(tmp_path, "20261108_102642_2", "Levain B", ["2026-11-08T10:26:42-03:00", "2026-11-08T14:59:00-03:00"])
    entries = list_sessions(tmp_path)
    assert [entry.name for entry in entries] == ["Levain B", "Levain A"]
    assert entries[0].label == "Levain B · 08/11 10:26 → 14:59"
    assert entries[1].label.endswith("→ 23:10 · encerrada")
    assert describe_session(tmp_path / "20261108_102642_2.csv").last.strftime("%H:%M") == "14:59"
    assert list_sessions(tmp_path / "missing") == []


def test_a_menu_entry_answers_like_the_button_it_replaced():
    from levainpeak.app import _MenuEntry

    calls = []
    menu = SimpleNamespace(index=lambda _end: 3, entryconfigure=lambda index, **options: calls.append((index, options)))
    entry = _MenuEntry(menu, {"Pote": "Marcar pote", "Cancelar": "Cancelar marcação do pote"})
    entry.configure(text="Cancelar")
    entry.configure(text="Pote", state="disabled")
    assert calls == [(3, {"label": "Cancelar marcação do pote"}), (3, {"label": "Marcar pote"}), (3, {"state": "disabled"})]


def test_automatic_detection_sets_the_drawing_aside_and_brings_it_back():
    from levainpeak.app import LevainPeakApp

    analyzer = SimpleNamespace(
        roi=(1, 2, 3, 4), outline=((0, 0), (1, 1)), vessel_bounds={"day": (10, 20)}, joined=0,
        join_here=lambda: None,
    )
    app = SimpleNamespace(
        _auto_detect=False, _auto_detect_var=SimpleNamespace(get=lambda: True, set=lambda value: None),
        _analyzer=analyzer, _manual_pot_roi=(1, 2, 3, 4), _pot_outline=((0, 0), (1, 1)),
        _last_container_box="box", _measurement="m", _set_message=lambda text: None,
    )
    LevainPeakApp._toggle_auto_detect(app)
    assert app._auto_detect and analyzer.roi is None and analyzer.outline is None and analyzer.vessel_bounds == {}
    assert app._manual_pot_roi == (1, 2, 3, 4) and app._pot_outline == ((0, 0), (1, 1))  # nothing was erased
    app._auto_detect_var = SimpleNamespace(get=lambda: False, set=lambda value: None)
    LevainPeakApp._toggle_auto_detect(app)
    assert not app._auto_detect and analyzer.roi == (1, 2, 3, 4) and analyzer.outline == ((0, 0), (1, 1))
    assert analyzer.vessel_bounds == {"day": (10, 20)}
