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


def test_the_menu_lists_the_newest_sessions_with_frames(tmp_path):
    from levainpeak.timelapse import recent_sessions

    for day in range(1, 13):
        folder = tmp_path / f"202611{day:02d}_100000_000001_fotos"
        folder.mkdir()
        (folder / f"202611{day:02d}_100500.jpg").write_bytes(b"")
    (tmp_path / "20261112_100000_000001.json").write_text('{"session_name": "Bradok 2L"}')
    (tmp_path / "20261120_100000_000001_fotos").mkdir()  # no frames: left out
    found = recent_sessions(tmp_path)
    assert len(found) == 10
    assert found[0][0] == "Bradok 2L · 12/11 10:00 · 1 foto"
    assert found[-1][0].startswith("03/11")


def test_the_view_keeps_zoom_and_position_inside_the_picture():
    from levainpeak.timelapse import view_box

    scale, left, top = view_box((1000, 500), (500, 500), 1.0, (0.5, 0.5))
    assert scale == 0.5 and left == 0 and top == -250  # fitted: centered vertically
    scale, left, top = view_box((1000, 500), (500, 500), 4.0, (0.5, 0.5))
    assert scale == 2.0 and (left, top) == (375, 125)  # the middle of the picture
    assert view_box((1000, 500), (500, 500), 4.0, (0.0, 0.0))[1:] == (0, 0)  # never past the edge
    assert view_box((1000, 500), (500, 500), 4.0, (1.0, 1.0))[1:] == (750, 250)


def _player(loop, index, count=3):
    from types import SimpleNamespace

    calls = []
    player = SimpleNamespace(
        _after_id=None, _playing=True, _index=index, _photos=[0] * count,
        _loop=SimpleNamespace(get=lambda: loop), _fps=SimpleNamespace(get=lambda: 10),
        go_to=lambda i: calls.append(i), _stop=lambda: calls.append("stop"),
        window=SimpleNamespace(after=lambda *_a: "job"), _tick=None,
    )
    return player, calls


def test_the_play_starts_over_at_the_end_only_when_the_loop_is_on():
    from levainpeak.timelapse import TimelapseWindow

    player, calls = _player(loop=True, index=2)
    TimelapseWindow._tick(player)
    assert calls == [0] and player._after_id == "job"
    player, calls = _player(loop=False, index=2)
    TimelapseWindow._tick(player)
    assert calls == ["stop"]
    player, calls = _player(loop=True, index=0)
    TimelapseWindow._tick(player)
    assert calls == [1]
