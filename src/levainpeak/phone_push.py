"""Sends one bundle per cycle to the Google Sheets receiver (Apps Script)."""

from __future__ import annotations

import base64
import csv
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from levainpeak.graph_data import chart_payload, prepare_graph_data
from levainpeak.phone_config import PhoneConfig

MAX_POINTS_PER_PACKET = 1000
MAX_SNAPSHOT_CHARS = 25_000  # the receiver refuses a cell above 49,000; stay well under it


def read_new_points(
    path: Path, after: str | None, limit: int = MAX_POINTS_PER_PACKET
) -> tuple[list[dict], str]:
    """Rows of the session CSV newer than ``after`` as compact points, plus the session name.

    Without ``after`` (first cycle) it takes the newest ``limit`` rows; with it,
    the oldest ``limit`` rows after it, so a long outage is caught up over a few cycles.
    """
    points: list[dict] = []
    session = path.stem
    with path.open("r", newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            stamp = row.get("timestamp", "")
            if not stamp or (after is not None and stamp <= after):
                continue
            if row.get("session_name"):
                session = row["session_name"]
            temperature = row.get("temperature_c") or None
            points.append({
                "t": stamp,
                "v": float(row["volume_fraction"]),
                "c": None if temperature is None else float(temperature),
                "p": row.get("phase", ""),
            })
    return (points[-limit:] if after is None else points[:limit]), session


def build_packet(
    config: PhoneConfig,
    live: dict,
    session_csv: Path | None,
    *,
    after: str | None,
    snapshot: dict | None,
    now: datetime | None = None,
) -> tuple[dict, str | None]:
    """The bundle for one cycle, and the timestamp of its newest point (None if no points)."""
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    packet: dict = {"token": config.token, "sent_at": stamp, "live": live}
    newest = None
    if session_csv is not None and session_csv.is_file():
        points, session = read_new_points(session_csv, after)
        packet["session"] = session
        if points:
            packet["points"] = points
            newest = points[-1]["t"]
        chart = chart_payload(prepare_graph_data(session_csv))
        if chart is not None:
            packet["chart"] = chart
    if snapshot and len(snapshot.get("jpeg_b64", "")) <= MAX_SNAPSHOT_CHARS:
        packet["snapshot"] = snapshot
    return packet, newest


def make_snapshot(frame: np.ndarray, measurement, *, width: int = 300) -> dict | None:
    """A small JPEG of the pot with the surface and the initial mark drawn, as base64.

    Shrinks the picture and the quality until it fits MAX_SNAPSHOT_CHARS. None
    when there is no measurement yet.
    """
    if frame is None or measurement is None:
        return None
    height_px, width_px = frame.shape[:2]
    margin_x = int((measurement.right - measurement.left) * 0.06)
    margin_y = int((measurement.bottom - measurement.top) * 0.04)
    left, right = max(0, measurement.left - margin_x), min(width_px, measurement.right + margin_x)
    top, bottom = max(0, measurement.top - margin_y), min(height_px, measurement.bottom + margin_y)
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        return None
    for target in (width, int(width * 0.8), int(width * 0.64)):
        scale = target / crop.shape[1]
        picture = cv2.resize(crop, (target, max(1, int(crop.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        surface = int((measurement.surface_y - top) * scale)
        cv2.line(picture, (0, surface), (target, surface), (40, 210, 255), 1)
        if measurement.marker_y is not None:
            mark = int((measurement.marker_y - top) * scale)
            for x in range(0, target, 10):
                cv2.line(picture, (x, mark), (min(x + 5, target), mark), (255, 160, 50), 1)
        for quality in (60, 50, 40, 30):
            ok, encoded = cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, quality])
            text = base64.b64encode(encoded.tobytes()).decode("ascii")
            if ok and len(text) <= MAX_SNAPSHOT_CHARS:
                return {"t": datetime.now().astimezone().isoformat(timespec="seconds"), "jpeg_b64": text}
    return None


@dataclass
class PusherStatus:
    last_ok: float | None = None  # time.time() of the last accepted bundle
    last_error: str | None = None
    cycles: int = 0
    points_sent: int = 0


class PhonePusher:
    """Background sender: one bundle every ``interval_seconds``, retried on failure."""

    def __init__(
        self,
        config: PhoneConfig,
        get_live: Callable[[], dict],
        get_session_csv: Callable[[], Path | None],
        get_snapshot: Callable[[], dict | None] | None = None,
        *,
        interval_seconds: float = 120,
        first_delay: float = 10,
        timeout: float = 60,
        retry_delays: tuple[float, ...] = (5.0, 15.0),
    ) -> None:
        self._config = config
        self._get_live = get_live
        self._get_session_csv = get_session_csv
        self._get_snapshot = get_snapshot
        self.interval_seconds = interval_seconds
        self._first_delay = first_delay
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._after: str | None = None  # newest point the receiver has accepted
        self._status = PusherStatus()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()  # a cycle and a clear never overlap
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> PusherStatus:
        with self._lock:
            return PusherStatus(**vars(self._status))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="phone-push", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        """Ask the sender to finish. ``wait=False`` is for the UI thread: a request
        in flight can take a minute, and the window must not freeze meanwhile."""
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        if self._stop.wait(self._first_delay):
            return
        while not self._stop.is_set():
            try:
                self.push_once()
            except Exception:  # noqa: BLE001 - one bad cycle must not end the sender
                logging.exception("Falha inesperada no envio ao celular")
            if self._stop.wait(self.interval_seconds):
                break

    def push_once(self) -> bool:
        """Send one bundle now. True when the receiver accepted it."""
        with self._send_lock:
            return self._push_once()

    def clear_remote(self) -> tuple[bool, str]:
        """Ask the receiver to empty the spreadsheet. Returns (done, message for the user).

        Points already in the session CSV are not sent again afterwards, so the
        sheet stays empty until new readings arrive (the live state and the chart
        come back with the next cycle).
        """
        with self._send_lock:
            body = json.dumps({"token": self._config.token, "acao": "limpar"}).encode("utf-8")
            try:
                answer = self._post(body)
            except Exception as exc:  # noqa: BLE001 - described, never re-raised with the URL
                return False, self._describe(exc)
            if not answer.get("ok"):
                return False, str(answer.get("erro") or "resposta inesperada").replace(self._config.token, "<token>")
            if "limpou" not in answer:
                return False, "o script do Google ainda é a versão antiga (cole o Code.gs novo e crie uma nova versão)"
            csv_path = self._get_session_csv()
            if csv_path is not None and csv_path.is_file():
                newest, _ = read_new_points(csv_path, None, limit=1)
                if newest:
                    with self._lock:
                        self._after = newest[-1]["t"]
            rows = sum(int(n) for n in (answer.get("limpou") or {}).values() if isinstance(n, (int, float)))
            return True, f"{rows} linhas apagadas"

    def _push_once(self) -> bool:
        snapshot = self._get_snapshot() if self._get_snapshot is not None else None
        packet, newest = build_packet(
            self._config, self._get_live() or {}, self._get_session_csv(),
            after=self._after, snapshot=snapshot,
        )
        body = json.dumps(packet).encode("utf-8")
        error = "sem resposta"
        for delay in (0.0, *self._retry_delays):
            if delay and self._stop.wait(delay):
                return False
            try:
                answer = self._post(body)
            except Exception as exc:  # noqa: BLE001 - described below, never re-raised with the URL
                error = self._describe(exc)
                continue
            if answer.get("ok"):
                with self._lock:
                    if newest is not None:
                        self._after = newest
                    self._status.last_ok = time.time()
                    self._status.last_error = None
                    self._status.cycles += 1
                    self._status.points_sent += len(packet.get("points", []))
                return True
            error = str(answer.get("erro") or "resposta inesperada")
            if "token" in error:
                break  # a refused token will not be accepted a few seconds later
        error = error.replace(self._config.token, "<token>")
        with self._lock:
            self._status.last_error = error
        logging.warning("Envio ao celular falhou: %s", error)
        return False

    def _post(self, body: bytes) -> dict:
        request = urllib.request.Request(
            self._config.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        # The receiver answers a POST with a redirect to the result, which urllib follows with a GET.
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _describe(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTP {exc.code}"
        if isinstance(exc, urllib.error.URLError):
            return f"sem conexão ({exc.reason})"
        if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
            return "resposta inválida do receptor"
        return type(exc).__name__
