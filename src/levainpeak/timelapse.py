"""Time-lapse window: steps through the raw frames a session kept beside its log."""

from __future__ import annotations

import json
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from levainpeak.stream import rotate_frame

MIN_FPS = 1
MAX_FPS = 30
DEFAULT_FPS = 5
CACHE_FRAMES = 24
RECENT_SESSIONS = 10


def session_photos(session_path: Path | None) -> list[Path]:
    """The frames kept for the session at `session_path`, oldest first (names are timestamps)."""
    if session_path is None:
        return []
    return sorted(session_path.with_name(f"{session_path.stem}_fotos").glob("*.jpg"))


def recent_sessions(directory: Path, current: Path | None = None, limit: int = RECENT_SESSIONS) -> list[tuple[str, list[Path]]]:
    """(menu label, frames) of the newest `limit` sessions that kept frames; the active one is always kept."""
    found: list[tuple[str, list[Path]]] = []
    for folder in sorted(directory.glob("*_fotos"), reverse=True):
        photos = sorted(folder.glob("*.jpg"))
        if not photos and (current is None or folder.name != f"{current.stem}_fotos"):
            continue
        started = photo_time(Path(folder.name.removesuffix("_fotos")))
        when = f"{started:%d/%m %H:%M}" if started else folder.name
        name = session_name(folder.with_name(folder.name.removesuffix("_fotos") + ".json"))
        count = f"{len(photos)} foto{'s' if len(photos) != 1 else ''}"
        found.append((" · ".join(part for part in (name, when, count) if part), photos))
    return found[:limit]


def session_name(metadata: Path) -> str:
    try:
        return str(json.loads(metadata.read_text(encoding="utf-8")).get("session_name") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def photo_time(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


MAX_ZOOM = 12.0
BACKGROUND = "#202723"


def view_box(
    image_size: tuple[int, int], canvas_size: tuple[int, int], zoom: float, center: tuple[float, float]
) -> tuple[float, float, float]:
    """Scale and top-left corner (picture pixels) of what the canvas shows.

    ``center`` is the middle of the view as a fraction of the picture. A picture smaller than the
    canvas along an axis stays centered there (the corner goes negative); a larger one never leaves a gap."""
    scale = min(canvas_size[0] / image_size[0], canvas_size[1] / image_size[1]) * zoom
    corner = []
    for axis in (0, 1):
        span = canvas_size[axis] / scale
        if span >= image_size[axis]:
            corner.append((image_size[axis] - span) / 2)
        else:
            corner.append(min(image_size[axis] - span, max(0.0, center[axis] * image_size[axis] - span / 2)))
    return scale, corner[0], corner[1]


def step_index(index: int, step: int, count: int) -> int:
    return min(count - 1, max(0, index + step))


class TimelapseWindow:
    def __init__(
        self, parent: tk.Misc, sessions: list[tuple[str, list[Path]]], chosen: int = 0, rotation: float = 0.0
    ) -> None:
        self._sessions = sessions
        self._photos = sessions[chosen][1] if sessions else []
        photos = self._photos
        self._rotation = rotation
        self._index = 0
        self._playing = False
        self._after_id: str | None = None
        self._cache: dict[Path, Image.Image] = {}
        self._tk_image: ImageTk.PhotoImage | None = None
        self._zoom = 1.0  # kept from one frame to the next, and between sessions
        self._center = (0.5, 0.5)
        self._drag: tuple[int, int] | None = None
        self.open = True

        self.window = tk.Toplevel(parent, class_="Pythontkfloating")  # the i3 rule floats this class
        self.window.title("Time-lapse da sessão")
        self.window.geometry("960x620")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        top = ttk.Frame(self.window)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Sessão").pack(side="left")
        self._menu = ttk.Combobox(
            top, state="readonly", values=[label for label, _ in sessions], takefocus=0, width=44,
        )
        if sessions:
            self._menu.current(chosen)
        self._menu.pack(side="left", padx=6)
        self._menu.bind("<<ComboboxSelected>>", self._chose_session)
        self.canvas = tk.Canvas(self.window, background="#202723", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._show())
        self.canvas.bind("<Button-1>", self._pressed)
        self.canvas.bind("<B1-Motion>", self._dragged)
        self.canvas.bind("<Double-Button-1>", self._reset_view)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(sequence, self._wheel)

        bar = ttk.Frame(self.window)
        bar.pack(fill="x", padx=8, pady=(6, 8))
        self._slider = tk.Scale(
            bar, from_=0, to=max(0, len(photos) - 1), orient="horizontal", showvalue=False,
            resolution=1, takefocus=0, command=self._slid,
        )
        self._slider.pack(fill="x")
        row = ttk.Frame(bar)
        row.pack(fill="x", pady=(4, 0))
        buttons = (("⏮", self.first), ("◀", lambda: self.step(-1)), ("▶", lambda: self.step(1)), ("⏭", self.last))
        self._play_button = ttk.Button(row, text="Play", width=7, command=self.toggle_play, takefocus=0)
        for text, command in buttons[:2]:
            ttk.Button(row, text=text, width=3, command=command, takefocus=0).pack(side="left")
        self._play_button.pack(side="left", padx=2)
        for text, command in buttons[2:]:
            ttk.Button(row, text=text, width=3, command=command, takefocus=0).pack(side="left")
        ttk.Label(row, text="fps").pack(side="left", padx=(12, 4))
        self._fps = tk.IntVar(value=DEFAULT_FPS)
        ttk.Spinbox(
            row, from_=MIN_FPS, to=MAX_FPS, width=4, textvariable=self._fps, takefocus=0,
            command=self.window.focus_set,
        ).pack(side="left")
        self._loop = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Repetir", variable=self._loop, takefocus=0).pack(side="left", padx=(12, 0))
        self._label = tk.StringVar()
        ttk.Label(row, textvariable=self._label, anchor="e").pack(side="right")

        for keys, command in (
            (("<Up>", "<Right>", "<space>"), lambda: self.step(1)),
            (("<Down>", "<Left>", "<Shift-space>"), lambda: self.step(-1)),
            (("<Home>",), self.first),
            (("<End>",), self.last),
            (("<Escape>",), self.close),
        ):
            for key in keys:
                self.window.bind(key, lambda _e, c=command: (c(), "break")[1])
        self.window.focus_set()
        self._show()

    def _chose_session(self, _event=None) -> None:
        self._stop()
        self._photos = self._sessions[self._menu.current()][1]
        self._cache.clear()
        self._slider.configure(to=max(0, len(self._photos) - 1))
        self._index = 0
        self.go_to(len(self._photos) - 1)
        self.window.focus_set()

    def lift(self) -> None:
        self.window.lift()

    def close(self) -> None:
        self._stop()
        self.open = False
        self.window.destroy()

    def go_to(self, index: int) -> None:
        if not self._photos:
            return
        self._index = step_index(index, 0, len(self._photos))
        self._slider.set(self._index)
        self._show()

    def step(self, step: int) -> None:
        self._stop()
        self.go_to(self._index + step)

    def first(self) -> None:
        self._stop()
        self.go_to(0)

    def last(self) -> None:
        self._stop()
        self.go_to(len(self._photos) - 1)

    def toggle_play(self) -> None:
        if self._playing:
            self._stop()
            return
        if self._index >= len(self._photos) - 1:
            self.go_to(0)
        self._playing = True
        self._play_button.configure(text="Pausar")
        self._tick()

    def _stop(self) -> None:
        self._playing = False
        if self._after_id is not None:
            self.window.after_cancel(self._after_id)
            self._after_id = None
        if self.open:
            self._play_button.configure(text="Play")

    def _tick(self) -> None:
        self._after_id = None
        if not self._playing:
            return
        if self._index >= len(self._photos) - 1:
            if not self._loop.get() or len(self._photos) < 2:
                self._stop()
                return
            self.go_to(0)  # "Repetir": start over by itself
        else:
            self.go_to(self._index + 1)
        try:
            fps = min(MAX_FPS, max(MIN_FPS, int(self._fps.get())))
        except (tk.TclError, ValueError):
            fps = DEFAULT_FPS
        self._after_id = self.window.after(int(1000 / fps), self._tick)

    def _slid(self, value: str) -> None:
        index = int(float(value))
        if index != self._index:
            self._index = index
            self._show()

    def _load(self, path: Path) -> Image.Image | None:
        image = self._cache.get(path)
        if image is None:
            frame = cv2.imread(str(path))
            if frame is None:
                return None
            frame = rotate_frame(frame, self._rotation)
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if len(self._cache) >= CACHE_FRAMES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[path] = image
        return image

    def _show(self) -> None:
        if not self.open:
            return
        self.canvas.delete("all")
        if not self._photos:
            self.canvas.create_text(
                20, 20, anchor="nw", fill="#d8e0da", text="Esta sessão ainda não tem fotos guardadas."
            )
            self._label.set("0/0")
            return
        path = self._photos[self._index]
        taken = photo_time(path)
        stamp = f"{taken:%d/%m %H:%M}" if taken else path.stem
        self._label.set(f"{self._index + 1}/{len(self._photos)}   {stamp}")
        image = self._load(path)
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if image is None or width < 4 or height < 4:
            return
        scale, left, top = view_box((image.width, image.height), (width, height), self._zoom, self._center)
        x0, y0 = max(0, int(left)), max(0, int(top))
        x1 = min(image.width, int(np.ceil(left + width / scale)))
        y1 = min(image.height, int(np.ceil(top + height / scale)))
        part = image.crop((x0, y0, x1, y1))
        part = part.resize((max(1, round(part.width * scale)), max(1, round(part.height * scale))), Image.BILINEAR)
        shown = Image.new("RGB", (width, height), BACKGROUND)
        shown.paste(part, (round((x0 - left) * scale), round((y0 - top) * scale)))
        self._tk_image = ImageTk.PhotoImage(shown)
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_image)

    def _current_view(self) -> tuple[Image.Image, float, float, float] | None:
        if not self._photos:
            return None
        image = self._load(self._photos[self._index])
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if image is None or width < 4 or height < 4:
            return None
        return (image, *view_box((image.width, image.height), (width, height), self._zoom, self._center))

    def _set_center(self, image: Image.Image, scale: float, left: float, top: float) -> None:
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._center = ((left + width / scale / 2) / image.width, (top + height / scale / 2) / image.height)

    def _wheel(self, event: tk.Event) -> None:
        """Zoom so that the point under the mouse stays under the mouse."""
        view = self._current_view()
        if view is None:
            return
        image, scale, left, top = view
        direction = 1 if getattr(event, "delta", 0) > 0 or getattr(event, "num", None) == 4 else -1
        new_zoom = min(MAX_ZOOM, max(1.0, self._zoom * (1.25 if direction > 0 else 0.8)))
        if new_zoom == self._zoom:
            return
        if new_zoom == 1.0:
            self._zoom, self._center = 1.0, (0.5, 0.5)
        else:
            new_scale = scale * new_zoom / self._zoom
            self._zoom = new_zoom
            self._set_center(image, new_scale, left + event.x / scale - event.x / new_scale,
                             top + event.y / scale - event.y / new_scale)
        self._show()

    def _pressed(self, event: tk.Event) -> None:
        self.window.focus_set()
        self._drag = (event.x, event.y)

    def _dragged(self, event: tk.Event) -> None:
        if self._drag is None or self._zoom <= 1.0:
            return
        view = self._current_view()
        if view is None:
            return
        image, scale, left, top = view
        self._set_center(image, scale, left - (event.x - self._drag[0]) / scale, top - (event.y - self._drag[1]) / scale)
        self._drag = (event.x, event.y)
        self._show()

    def _reset_view(self, _event=None) -> None:
        self._zoom, self._center = 1.0, (0.5, 0.5)
        self._show()
