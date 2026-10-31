import base64
import csv
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import pytest

from levainpeak.phone_config import PhoneConfig
from levainpeak.phone_push import MAX_POINTS_PER_PACKET, MAX_SNAPSHOT_CHARS, PhonePusher, make_snapshot
from levainpeak.vision import ContainerMeasurement

TOKEN = "segredo-bem-longo-123456"
COLUMNS = ["session_id", "timestamp", "mix_time", "temperature_c", "volume_fraction",
           "initial_volume_fraction", "growth_factor", "phase", "session_name"]
START = datetime(2026, 10, 30, 8, 0, tzinfo=timezone(timedelta(hours=-3)))


class FakeAppsScript:
    """Answers like the receiver: a POST gets a redirect, the result comes with a GET."""

    def __init__(self, fail_first=0):
        self.packets, self.attempts, self.fail_first, self.answers = [], 0, fail_first, {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.attempts += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.attempts <= outer.fail_first:
                    self.send_response(500)
                    self.end_headers()
                    return
                if body.get("token") != TOKEN:
                    answer = {"ok": False, "erro": "token inválido"}
                else:
                    outer.packets.append(body)
                    answer = {"ok": True, "limpou": {"serie": 7, "grafico": 2}} if body.get("acao") == "limpar" else {"ok": True, "recebido": {"pontos": len(body.get("points", []))}}
                key = str(len(outer.answers))
                outer.answers[key] = answer
                self.send_response(302)
                self.send_header("Location", f"/echo?id={key}")
                self.end_headers()

            def do_GET(self):
                key = parse_qs(urlparse(self.path).query)["id"][0]
                data = json.dumps(outer.answers[key]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/exec"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    server = FakeAppsScript()
    yield server
    server.close()


def append_rows(path, first, count):
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        if new_file:
            writer.writerow(COLUMNS)
        for index in range(first, first + count):
            stamp = (START + timedelta(seconds=10 * index)).isoformat(timespec="seconds")
            volume = 0.2 + 0.2 * index / 1999
            writer.writerow(["x", stamp, "", "21.8", f"{volume:.4f}", "0.2", f"{volume / 0.2:.3f}", "Crescendo", "Bradok"])


def pusher(url, csv_path, *, snapshot=None, retries=(0.01, 0.01), token=TOKEN):
    return PhonePusher(
        PhoneConfig(url, token), lambda: {"fase": "Crescendo", "volume_pct": 42.0},
        lambda: csv_path, snapshot, interval_seconds=0.05, first_delay=0.0, retry_delays=retries,
    )


def test_a_cycle_sends_live_points_and_the_chart_together(receiver, tmp_path):
    path = tmp_path / "s.csv"
    append_rows(path, 0, 120)
    sender = pusher(receiver.url, path)
    assert sender.push_once() is True
    packet = receiver.packets[0]
    assert packet["live"]["fase"] == "Crescendo" and packet["session"] == "Bradok"
    assert len(packet["points"]) == 120 and packet["chart"]["pts"] and "snapshot" not in packet
    status = sender.status
    assert status.last_error is None and status.cycles == 1 and status.points_sent == 120


def test_the_next_cycle_sends_only_what_is_new(receiver, tmp_path):
    path = tmp_path / "s.csv"
    append_rows(path, 0, 50)
    sender = pusher(receiver.url, path)
    sender.push_once()
    append_rows(path, 50, 20)
    sender.push_once()
    second = receiver.packets[1]["points"]
    assert len(second) == 20 and second[0]["t"] == (START + timedelta(seconds=500)).isoformat(timespec="seconds")
    assert sender.push_once() and "points" not in receiver.packets[2]  # nothing new: no points at all


def test_the_first_cycle_sends_at_most_the_newest_points(receiver, tmp_path):
    path = tmp_path / "s.csv"
    append_rows(path, 0, MAX_POINTS_PER_PACKET + 300)
    pusher(receiver.url, path).push_once()
    points = receiver.packets[0]["points"]
    assert len(points) == MAX_POINTS_PER_PACKET
    assert points[-1]["t"] == (START + timedelta(seconds=10 * (MAX_POINTS_PER_PACKET + 299))).isoformat(timespec="seconds")


def test_a_failed_request_is_retried_within_the_cycle(tmp_path):
    server = FakeAppsScript(fail_first=2)
    try:
        path = tmp_path / "s.csv"
        append_rows(path, 0, 10)
        sender = pusher(server.url, path, retries=(0.01, 0.01))
        assert sender.push_once() is True
        assert server.attempts == 3 and len(server.packets) == 1
    finally:
        server.close()


def test_when_every_try_fails_the_points_stay_pending_for_the_next_cycle(tmp_path):
    server = FakeAppsScript(fail_first=3)
    try:
        path = tmp_path / "s.csv"
        append_rows(path, 0, 10)
        sender = pusher(server.url, path, retries=(0.01, 0.01))
        assert sender.push_once() is False
        assert sender.status.last_error == "HTTP 500" and sender.status.cycles == 0
        assert sender.push_once() is True  # the server recovered
        assert len(server.packets[0]["points"]) == 10  # nothing was lost
    finally:
        server.close()


def test_a_refused_token_is_not_retried_and_never_shows_in_the_status_or_logs(receiver, tmp_path, caplog):
    path = tmp_path / "s.csv"
    append_rows(path, 0, 10)
    wrong = "outro-token-bem-longo-9999"
    sender = pusher(receiver.url, path, token=wrong)
    with caplog.at_level(logging.DEBUG):
        assert sender.push_once() is False
    assert receiver.attempts == 1  # no point insisting on a token that was refused
    assert sender.status.last_error == "token inválido"
    assert wrong not in sender.status.last_error and wrong not in caplog.text and TOKEN not in caplog.text


def test_the_snapshot_goes_only_when_provided_and_small_enough(receiver, tmp_path):
    path = tmp_path / "s.csv"
    append_rows(path, 0, 10)
    pusher(receiver.url, path, snapshot=lambda: {"t": "x", "jpeg_b64": "QUJD"}).push_once()
    pusher(receiver.url, path, snapshot=lambda: None).push_once()
    pusher(receiver.url, path, snapshot=lambda: {"t": "x", "jpeg_b64": "A" * (MAX_SNAPSHOT_CHARS + 1)}).push_once()
    assert [("snapshot" in p) for p in receiver.packets] == [True, False, False]


def test_the_snapshot_is_a_small_jpeg_of_the_pot_with_the_lines_drawn():
    rng = np.random.default_rng(3)
    frame = rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
    measurement = ContainerMeasurement(left=840, right=1115, top=313, bottom=930, surface_y=700,
                                       volume_fraction=0.4, confidence=0.9, marker_y=780)
    snapshot = make_snapshot(frame, measurement)
    assert snapshot is not None and len(snapshot["jpeg_b64"]) <= MAX_SNAPSHOT_CHARS  # noise is the worst case
    picture = cv2.imdecode(np.frombuffer(base64.b64decode(snapshot["jpeg_b64"]), np.uint8), cv2.IMREAD_COLOR)
    assert picture.shape[1] <= 300 and picture.shape[0] > picture.shape[1]  # a tall crop of the pot
    assert make_snapshot(frame, None) is None


def test_the_background_thread_sends_every_cycle_and_stops_quickly(receiver, tmp_path):
    path = tmp_path / "s.csv"
    append_rows(path, 0, 10)
    sender = pusher(receiver.url, path)
    sender.start()
    deadline = time.time() + 5
    while len(receiver.packets) < 3 and time.time() < deadline:
        time.sleep(0.02)
    began = time.time()
    sender.stop()
    assert len(receiver.packets) >= 3 and time.time() - began < 2


def test_clearing_asks_the_receiver_and_then_sends_the_current_session_again(receiver, tmp_path):
    csv_path = tmp_path / "s.csv"
    append_rows(csv_path, 0, 5)
    sender = pusher(receiver.url, csv_path)
    assert sender.push_once()
    done, message = sender.clear_remote()
    assert done and message == "9 linhas apagadas"
    assert receiver.packets[-1] == {"token": TOKEN, "acao": "limpar"}
    assert sender.push_once()
    assert len(receiver.packets[-1]["points"]) == 5


def test_a_new_session_file_is_sent_whole_from_the_start(receiver, tmp_path):
    old, new = tmp_path / "old.csv", tmp_path / "new.csv"
    append_rows(old, 0, 5)
    append_rows(new, 0, 3)
    current = [old]
    sender = PhonePusher(
        PhoneConfig(receiver.url, TOKEN), dict, lambda: current[0],
        interval_seconds=1, first_delay=0.0, retry_delays=(0.01,),
    )
    assert sender.push_once() and sender.push_once()
    assert len(receiver.packets[-1].get("points", [])) == 0
    current[0] = new
    assert sender.push_once()
    assert len(receiver.packets[-1]["points"]) == 3


def test_a_refused_clear_reports_the_reason_without_the_token(receiver, tmp_path):
    csv_path = tmp_path / "s.csv"
    append_rows(csv_path, 0, 3)
    done, message = pusher(receiver.url, csv_path, token="outro-token-comprido").clear_remote()
    assert not done and message == "token inválido" and "outro-token" not in message


def test_the_snapshot_is_the_area_the_user_drew_with_pote():
    from levainpeak.vision import ContainerMeasurement

    frame = np.zeros((600, 800, 3), np.uint8)
    frame[:, :400] = (0, 0, 255)  # left half red, right half blue (BGR)
    frame[:, 400:] = (255, 0, 0)
    measurement = ContainerMeasurement(
        left=300, right=500, top=200, bottom=500, surface_y=300, volume_fraction=0.4, confidence=0.9,
    )
    # the box the user drew: all of the left half, taller than the vessel
    snapshot = make_snapshot(frame, measurement, width=300, crop=(0, 100, 400, 560))
    picture = cv2.imdecode(np.frombuffer(base64.b64decode(snapshot["jpeg_b64"]), np.uint8), cv2.IMREAD_COLOR)
    assert picture.shape[1] == 300 and picture[picture.shape[0] // 2, 250, 2] > 150  # red, not blue: that box, not the vessel's
    assert picture[picture.shape[0] // 2, 250, 0] < 100
