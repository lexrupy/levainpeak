"""Background RTSP reader that keeps only the newest frame for the UI."""

from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from levainpeak.config import CameraConfig


class RtspStream:
    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = "Aguardando conexão"
        self._frame_number = 0
        self._frame: np.ndarray | None = None
        self._thread = threading.Thread(target=self._run, name="rtsp-reader", daemon=True)

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[int, np.ndarray | None]:
        with self._lock:
            return self._frame_number, self._frame

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _publish(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._frame_number += 1

    def _run(self) -> None:
        timeout_ms = max(1000, self.config.read_timeout_seconds * 1000)
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            f"rtsp_transport;{self.config.transport}|timeout;{timeout_ms * 1000}",
        )

        while not self._stop.is_set():
            self._set_status("Conectando à câmera…")
            capture = None
            try:
                open_params = []
                if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                    open_params.extend((cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms))
                if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                    open_params.extend((cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms))
                capture = cv2.VideoCapture(
                    self.config.stream_url(), cv2.CAP_FFMPEG, open_params
                )
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not capture.isOpened():
                    self._set_status("Câmera indisponível; tentando reconectar…")
                    continue

                self._set_status("Conectado")
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None or frame.size == 0:
                        self._set_status("Stream interrompido; tentando reconectar…")
                        break
                    self._publish(frame)
            except (cv2.error, ValueError):
                self._set_status("Falha ao abrir o stream; tentando reconectar…")
            finally:
                if capture is not None:
                    capture.release()

            self._stop.wait(2.0)

        self._set_status("Câmera desconectada")
