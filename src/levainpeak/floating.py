"""Move and resize for the floating popups (graph, diagnostics) without a title bar grip."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

BORDER = 10

_CURSORS = {
    "n": "top_side", "s": "bottom_side", "w": "left_side", "e": "right_side",
    "nw": "top_left_corner", "ne": "top_right_corner",
    "sw": "bottom_left_corner", "se": "bottom_right_corner",
}


class FloatingWindowControls:
    """Drag the content to move the window; drag its thin border to resize it.

    The content widget must be packed with ``padx=BORDER, pady=BORDER`` so the
    window's own background shows around it as the border.
    """

    def __init__(
        self,
        window: tk.Toplevel,
        content: tk.Widget,
        border: int = BORDER,
        on_close: Callable[[], object] | None = None,
    ) -> None:
        self.window = window
        self.border = border
        self._drag_origin = (0, 0)
        self._resize: tuple[str, int, int, int, int, int, int] | None = None
        content.configure(cursor="fleur")
        content.bind("<ButtonPress-1>", self._drag_start, add="+")
        content.bind("<B1-Motion>", self._drag_move, add="+")
        window.bind("<Motion>", self._hover, add="+")
        window.bind("<ButtonPress-1>", self._resize_start, add="+")
        window.bind("<B1-Motion>", self._resize_move, add="+")
        if on_close is not None:
            # Escape only reaches the window that has the keyboard focus, and
            # clicking a canvas or label does not give it focus. So take the
            # focus when the window opens and whenever it is clicked.
            window.bind("<Escape>", lambda _event: (on_close(), "break")[1], add="+")
            window.after(50, self._take_focus)

    def _take_focus(self) -> None:
        if self.window.winfo_exists():
            self.window.focus_force()

    def _drag_start(self, event: tk.Event) -> None:
        self._take_focus()
        self._drag_origin = (
            event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y()
        )

    def _drag_move(self, event: tk.Event) -> None:
        offset_x, offset_y = self._drag_origin
        self.window.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _edges(self, event: tk.Event) -> str:
        """Edges ("n", "s", "w", "e" combined) under the pointer in the border."""
        edges = ""
        if event.y < self.border:
            edges += "n"
        elif event.y > self.window.winfo_height() - self.border:
            edges += "s"
        if event.x < self.border:
            edges += "w"
        elif event.x > self.window.winfo_width() - self.border:
            edges += "e"
        return edges

    def _hover(self, event: tk.Event) -> None:
        if event.widget is not self.window:
            return
        self.window.configure(cursor=_CURSORS.get(self._edges(event), ""))

    def _resize_start(self, event: tk.Event) -> None:
        if event.widget is not self.window:
            return
        self._take_focus()
        window = self.window
        self._resize = (
            self._edges(event), event.x_root, event.y_root,
            window.winfo_x(), window.winfo_y(), window.winfo_width(), window.winfo_height(),
        )

    def _resize_move(self, event: tk.Event) -> None:
        if event.widget is not self.window or not self._resize or not self._resize[0]:
            return
        edges, start_x, start_y, x, y, width, height = self._resize
        min_width, min_height = self.window.minsize()
        delta_x, delta_y = event.x_root - start_x, event.y_root - start_y
        if "e" in edges:
            width = max(min_width, width + delta_x)
        if "s" in edges:
            height = max(min_height, height + delta_y)
        if "w" in edges:
            new_width = max(min_width, width - delta_x)
            x += width - new_width
            width = new_width
        if "n" in edges:
            new_height = max(min_height, height - delta_y)
            y += height - new_height
            height = new_height
        self.window.geometry(f"{width}x{height}+{x}+{y}")
