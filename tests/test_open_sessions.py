import json
import os
import subprocess
import sys

from levainpeak.sessions import SessionLog, open_sessions, session_is_running

HEADER = "session_id,timestamp,mix_time,temperature_c,volume_fraction,initial_volume_fraction,growth_factor,phase,session_name\n"


def _session(directory, stem, *, ended=False, rows=True, camera=None):
    (directory / f"{stem}.csv").write_text(
        HEADER + (f"{stem},2026-11-18T10:00:00-03:00,2026-11-18T09:00:00-03:00,,0.3,0.2,1.5,Crescendo,\n" if rows else ""),
        encoding="utf-8",
    )
    meta = {"session_name": stem}
    if ended:
        meta["ended"] = "2026-11-18T11:00:00-03:00"
    if camera:
        meta["camera"] = camera
    (directory / f"{stem}.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory / f"{stem}.csv"


def test_only_the_newest_four_that_were_left_open_are_offered(tmp_path):
    _session(tmp_path, "20261110_100000")  # older than the newest four: not offered
    _session(tmp_path, "20261111_100000", camera="A9")
    _session(tmp_path, "20261112_100000", ended=True)
    _session(tmp_path, "20261113_100000", rows=False)  # nothing to continue from
    _session(tmp_path, "20261114_100000")
    names = [entry.path.stem for entry in open_sessions(tmp_path, limit=4)]
    assert names == ["20261114_100000", "20261111_100000"]
    assert open_sessions(tmp_path)[1].camera == "A9"


def test_a_session_held_by_a_live_process_is_not_offered_but_a_dead_one_is(tmp_path):
    path = _session(tmp_path, "20261114_100000")
    log = SessionLog(tmp_path)
    log.resume(path)  # this process now holds it
    assert session_is_running(path) and open_sessions(tmp_path) == []
    log.stop()  # ended and released
    assert not session_is_running(path)

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    path.with_suffix(".lock").write_text(f"{gone.pid} 1", encoding="utf-8")  # a crash left its lock behind
    json.loads(path.with_suffix(".json").read_text())  # still not ended
    meta = json.loads(path.with_suffix(".json").read_text())
    meta.pop("ended", None)
    path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    assert not session_is_running(path)
    assert [entry.path for entry in open_sessions(tmp_path)] == [path]


def test_a_reused_pid_does_not_keep_the_lock_alive(tmp_path):
    path = _session(tmp_path, "20261114_100000")
    path.with_suffix(".lock").write_text(f"{os.getpid()} 1", encoding="utf-8")  # same pid, another start time
    if os.path.exists(f"/proc/{os.getpid()}/stat"):
        assert not session_is_running(path)


def test_suspending_leaves_the_session_open_and_offered_again(tmp_path):
    path = _session(tmp_path, "20261114_100000")
    log = SessionLog(tmp_path)
    log.resume(path)
    log.suspend()  # the window closes
    meta = json.loads(path.with_suffix(".json").read_text())
    assert "ended" not in meta and not session_is_running(path)
    assert [entry.path for entry in open_sessions(tmp_path)] == [path]
    again = SessionLog(tmp_path)
    again.resume(path)
    again.stop()  # the button: now it is ended and no longer offered
    assert open_sessions(tmp_path) == []


def test_a_backup_beside_a_session_is_not_a_session(tmp_path):
    from levainpeak.sessions import find_session_csv, latest_session_csv, list_sessions

    path = _session(tmp_path, "20261114_100000")
    backup = _session(tmp_path, "20261114_100000.antes_do_ajuste")
    os.utime(backup, None)  # the newest file by time
    assert [entry.path for entry in open_sessions(tmp_path)] == [path]
    assert [entry.path for entry in list_sessions(tmp_path)] == [path]
    assert latest_session_csv(tmp_path) == path
    assert find_session_csv("20261114_100000.antes_do_ajuste", tmp_path) is None
