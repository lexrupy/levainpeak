"""Background camera readers (RTSP, A9 over the LAN, UVC) that keep only the newest frame for the UI."""

from __future__ import annotations

import os
import io
import sys
import threading

import cv2
import numpy as np
from PIL import Image

from levainpeak.config import CameraConfig, KIND_LABELS


def rotate_frame(frame: np.ndarray, degrees: float) -> np.ndarray:
    """Turn the picture clockwise: whole quarter turns change its size, the rest keeps it (black corners)."""
    turns = round(degrees / 90)
    if turns % 4:
        frame = np.ascontiguousarray(np.rot90(frame, -turns))
    rest = degrees - turns * 90
    if abs(rest) >= 0.05:
        height, width = frame.shape[:2]
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), -rest, 1.0)
        frame = cv2.warpAffine(frame, matrix, (width, height))
    return frame


class FrameSource:
    """What the app needs from any camera: status, rotation, snapshot(), start() and stop()."""

    thread_name = "camera-reader"

    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = "Aguardando conexão"
        self._frame_number = 0
        self._frame: np.ndarray | None = None
        self.rotation = 0.0  # degrees clockwise; applied to every frame as it arrives
        self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)

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
        frame = rotate_frame(frame, self.rotation)
        with self._lock:
            self._frame = frame
            self._frame_number += 1

    def _run(self) -> None:
        raise NotImplementedError


class RtspStream(FrameSource):
    thread_name = "rtsp-reader"

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


class UvcStream(FrameSource):
    """A webcam or other UVC device read through OpenCV (V4L2 on Linux)."""

    thread_name = "uvc-reader"

    def _open(self) -> cv2.VideoCapture:
        index = self.config.device
        if sys.platform.startswith("linux"):
            return cv2.VideoCapture(index, cv2.CAP_V4L2)
        return cv2.VideoCapture(index)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._set_status("Conectando à câmera…")
            capture = None
            try:
                capture = self._open()
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not capture.isOpened():
                    self._set_status(f"Câmera /dev/video{self.config.device} indisponível; tentando de novo…")
                else:
                    self._set_status("Conectado")
                    while not self._stop.is_set():
                        ok, frame = capture.read()
                        if not ok or frame is None or frame.size == 0:
                            self._set_status("Câmera parou de responder; tentando reconectar…")
                            break
                        self._publish(frame)
            except cv2.error:
                self._set_status("Falha ao abrir a câmera; tentando reconectar…")
            finally:
                if capture is not None:
                    capture.release()
            self._stop.wait(2.0)
        self._set_status("Câmera desconectada")


class A9Stream(FrameSource):
    """An A9/Linklemo camera on the LAN: the protocol thread keeps the latest JPEG, decoded here."""

    thread_name = "a9-reader"

    def __init__(self, config: CameraConfig) -> None:
        super().__init__(config)
        self._state: dict = {"jpeg": None, "seq": 0, "status": "iniciando…", "connected": False}
        self._state_lock = threading.Lock()
        self._worker = None
        self._size: tuple | None = None
        self.rejected_frames = 0

    def start(self) -> None:
        from levainpeak.a9_protocol import CameraWorker  # the cipher tables build on import

        self._worker = CameraWorker(
            self.config.ip.strip(), self.config.port, self.config.did.strip(), self.config.secret.strip(),
            self._state, self._state_lock,
        )
        self._worker.start()
        super().start()

    def stop(self) -> None:
        super().stop()
        if self._worker is not None:
            self._worker.stop()

    def _run(self) -> None:
        last_seq = -1
        while not self._stop.is_set():
            with self._state_lock:
                seq, jpeg = self._state.get("seq", 0), self._state.get("jpeg")
                status, connected = self._state.get("status", ""), self._state.get("connected", False)
            self._set_status("Conectado" if connected and jpeg else f"A9: {status}")
            if jpeg and seq != last_seq:
                last_seq = seq
                frame = decode_whole_jpeg(jpeg)
                if frame is None or (self._size is not None and frame.shape != self._size):
                    self.rejected_frames += 1  # cut short, damaged or of another size: never shown or saved
                    continue
                self._size = frame.shape
                self._publish(frame)
            self._stop.wait(0.03)
        self._set_status("Câmera desconectada")


def decode_whole_jpeg(jpeg: bytes) -> np.ndarray | None:
    """The picture of a JPEG, or None when it is cut short or damaged.

    OpenCV hands back a half-grey picture for a truncated file; Pillow refuses it, so it decides.
    """
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            image.load()  # raises on truncated or corrupt data
            rgb = np.asarray(image.convert("RGB"))
    except (OSError, ValueError, SyntaxError):
        return None
    return np.ascontiguousarray(rgb[:, :, ::-1])


STREAM_KINDS = {"rtsp": RtspStream, "a9": A9Stream, "uvc": UvcStream}


def open_stream(config: CameraConfig) -> FrameSource:
    """The reader that fits the camera's kind (not started yet)."""
    try:
        return STREAM_KINDS[config.kind](config)
    except KeyError:
        raise ValueError(f"Tipo de câmera sem suporte: {KIND_LABELS.get(config.kind, config.kind)}") from None
