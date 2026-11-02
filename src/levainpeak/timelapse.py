"""Time-lapse window: steps through the raw frames a session kept beside its log."""

from __future__ import annotations

import json
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import cv2
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
        self.open = True

        self.window = tk.Toplevel(parent)
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
        self.canvas.bind("<Button-1>", lambda _e: self.window.focus_set())

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
            self._stop()
            return
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
        scale = min(width / image.width, height / image.height)
        shown = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.BILINEAR)
        self._tk_image = ImageTk.PhotoImage(shown)
        self.canvas.create_image(width // 2, height // 2, image=self._tk_image)
