"""Tk screen for viewing the live camera stream."""

from __future__ import annotations

import argparse
import csv
import functools
import json
import math
import logging
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from levainpeak.analysis import ContainerAnalyzer
from levainpeak.config import CameraConfig, load_camera_config, save_camera_config
from levainpeak.debug_view import DebugWindow
from levainpeak.floating import FloatingWindowControls
from levainpeak.forecast import DoublingForecaster
from levainpeak.graph_data import (TEMPERATURE_SCALES, VOLUME_SCALES, prepare_graph_data,
                                   temperature_axis, volume_scale)
from levainpeak.graph_phases import (DECLINE, EXPONENTIAL, LATENCY, STATIONARY,
                                     graph_phase_segments)
from levainpeak.phone_config import PhoneConfig, load_phone_config, local_link, online_link, save_phone_config
from levainpeak.phone_push import PhonePusher, make_snapshot
from levainpeak.rate import growth_rate, rate_series
from levainpeak.sessions import (GEOMETRY_VERSION, PhaseTracker, SessionLog, describe_session, list_sessions, parse_ratio,
                                 find_session_csv, latest_session_csv)
from levainpeak.settings import (DEFAULT_INK_CUT_DAY, DEFAULT_INK_CUT_INFRARED, INK_CUT_DAY,
                                 INK_CUT_INFRARED, MAX_OVERLAY_BRIGHTNESS,
                                 MAX_PHONE_SECONDS, MAX_RECORD_SECONDS, MIN_OVERLAY_BRIGHTNESS,
                                 MIN_PHONE_SECONDS, MIN_RECORD_SECONDS,
                                 ARTIFACT_MINUTES, LAMP_MINUTES, MARK_SHAPES, PEAK_MINUTES, ROTATIONS, SNAPSHOT_MINUTES, Settings, load_settings, save_settings)
from levainpeak.stream import RtspStream
from levainpeak.timelapse import TimelapseWindow, recent_sessions
from levainpeak.visor_follow import rotate_visor

FINE_TURN_DEGREES = 15.0  # the slider's reach either way; whole quarter turns come from the menu
from levainpeak.vision import (MAX_SURFACE_BANDS, MIN_SURFACE_BANDS, ContainerMeasurement,
                               draw_measurement, inner_span, set_initial_marker,
                               set_initial_volume_reference)

GRAPH_BORDER = 10


@functools.lru_cache(maxsize=16)
def _lowercase_rows(scale: float, thickness: int) -> tuple[int, int]:
    """Rows above the baseline where a lowercase letter like "e" or "x" is inked: (top, bottom).

    cv2.getTextSize reports the height up to the tallest letter, which puts the middle too high."""
    canvas = np.zeros((120, 120), np.uint8)
    baseline = 80
    cv2.putText(canvas, "ex", (10, baseline), cv2.FONT_HERSHEY_SIMPLEX, scale, 255, thickness, cv2.LINE_AA)
    rows = np.where(canvas.max(axis=1) > 60)[0]
    return baseline - int(rows.max()), baseline - int(rows.min())


RATE_STRIP_MIN_HEIGHT = 300  # plot height (px) under which the rate strip is not drawn
RATE_STRIP_SHARE = 0.30  # share of the plot height the strip takes
REPEAT_WARNING = 2  # the same growth factor logged this many times in a row is flagged
MAX_ZOOM = 12.0  # the wheel zoom of the video; only the visible part is scaled, so it stays cheap

class _MenuEntry:
    """A menu item that answers like the button it replaced: ``configure(text=..., state=...)``.

    The old buttons changed their own text to say what a second click would do ("Cancelar", "Confirmar");
    ``labels`` maps those short texts to the menu's wording."""

    def __init__(self, menu: tk.Menu, labels: dict[str, str]) -> None:
        self.menu, self.index, self.labels = menu, menu.index("end"), labels

    def configure(self, text: str | None = None, state: str | None = None) -> None:
        if text is not None:
            self.menu.entryconfigure(self.index, label=self.labels.get(text, text))
        if state is not None:
            self.menu.entryconfigure(self.index, state=state)


class _Tooltip:
    """A small hint under the widget after the pointer rests on it; ``text`` may be a callable."""

    def __init__(self, widget, text) -> None:
        self.widget, self.text, self._job, self._tip = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self._job = self.widget.after(450, self._show)

    def _show(self) -> None:
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        tk.Label(self._tip, text=text, background="#fffbe6", foreground="#202723", relief="solid",
                 borderwidth=1, padx=5, pady=2).pack()
        self._tip.wm_geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")

    def _hide(self, _event=None) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class _IconButton(ttk.Button):
    """A toolbar button that answers like the text button it replaced: ``configure(text="Encerrar sessão")``
    swaps its icon and hint through ``states`` ({text: (icon, hint)}); an unknown text is shown as it is."""

    def __init__(self, master, states: dict[str, tuple[str, str]], text: str, command, **options) -> None:
        self.states, self.hint = states, ""
        super().__init__(master, command=command, width=3, **options)
        self.configure(text=text)
        _Tooltip(self, lambda: self.hint)

    def configure(self, cnf=None, **options):
        if "text" in options:
            icon, self.hint = self.states.get(options["text"], (options["text"], options["text"]))
            options["text"] = icon
        return super().configure(cnf, **options)

    config = configure


class _ScrollFrame(ttk.Frame):
    """A frame that scrolls vertically when its content is taller than the window."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0, background="#f4f1e8")
        bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._resize, add="+")
        self._canvas.bind("<Configure>", self._resize, add="+")
        for widget in (self._canvas, self.inner):
            widget.bind("<Enter>", self._bind_wheel, add="+")
            widget.bind("<Leave>", self._unbind_wheel, add="+")

    def _resize(self, _event=None) -> None:
        # Fill the window when there is room (so expanding children still grow); scroll when there is not.
        width, height = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.itemconfigure(
            self._window, width=width, height=max(height, self.inner.winfo_reqheight())
        )
        self._canvas.configure(scrollregion=(0, 0, width, max(height, self.inner.winfo_reqheight())))

    def _bind_wheel(self, _event=None) -> None:
        self._canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self._canvas.bind_all("<Button-4>", self._wheel, add="+")
        self._canvas.bind_all("<Button-5>", self._wheel, add="+")

    def _unbind_wheel(self, event=None) -> None:
        if event is not None:  # moving between the canvas and its content is not leaving
            inside = self.winfo_containing(event.x_root, event.y_root)
            if inside is not None and str(inside).startswith(str(self)):
                return
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.unbind_all(sequence)

    def _wheel(self, event) -> None:
        if isinstance(event.widget, (tk.Canvas, ttk.Scale, ttk.Spinbox)) and event.widget is not self._canvas:
            return  # the video zoom, the sliders and the spin boxes keep their own wheel
        if self._canvas.yview() == (0.0, 1.0):
            return
        step = -1 if (event.num == 4 or getattr(event, "delta", 0) > 0) else 1
        self._canvas.yview_scroll(step * 2, "units")


class LevainPeakApp:
    def __init__(self, root: tk.Tk, *, resume_last: bool | str = False) -> None:
        self.root = root
        self.root.title("LevainPeak")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1180, max(640, screen_width - 80))
        height = min(760, max(440, screen_height - 80))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(640, 440)
        self.root.configure(background="#f4f1e8")

        self._image: ImageTk.PhotoImage | None = None
        self._video_image_item: int | None = None
        self._stream: RtspStream | None = None
        self._analyzer: ContainerAnalyzer | None = None
        self._measurement: ContainerMeasurement | None = None
        self._last_displayed_frame_number = 0
        self._last_stream_status: str | None = None
        self._manual_marker_y: int | None = None
        self._baseline_locked = False  # a resumed session keeps the initial level it had; only a new mark changes it
        self._marker_automatic = False  # the mark was put at the first reading's surface, not by hand
        self._realigned_at = float("-inf")  # when the contour last followed the pot by itself
        self._marker_mode = False
        self._roi_mode = False
        self._roi_target = "pot"
        self._roi_drag_start: tuple[int, int] | None = None
        self._roi_preview_item: int | None = None
        self._band_drag: tuple[int, int, tuple[float, float]] | None = None  # (band, start x, its fractions) while one is moved
        self._band_override: tuple | None = None  # the bands as shown while one is being moved
        self._manual_pot_roi: tuple[int, int, int, int] | None = None
        self._pot_outline: tuple[tuple[int, int], ...] | None = None  # pot contour drawn by the user
        self._temperature_quad: tuple[tuple[int, int], ...] | None = None  # LCD face corners drawn by the user
        self._outline_target = "pot"
        self._outline_mode = False
        self._outline_points: list[list[int]] = []
        self._outline_drag: int | None = None
        # A side grabbed away from its corners: (first corner, second corner, the two corners at the grab,
        # the side's unit normal, the grab point). It slides along the normal, so it never tilts.
        self._outline_edge_drag: tuple | None = None
        self._reposition_mode = False  # "r": the pot's drawing follows the mouse, the wheel resizes it
        self._reposition_center: tuple[float, float] | None = None
        self._reposition_scale = 1.0
        self._reposition_redrawn = 0.0
        self._temperature_roi: tuple[int, int, int, int] | None = None
        self._temperature_c: float | None = None
        self._last_known_temperature: float | None = None
        self._temperature_filled = False
        self._preview_marker_item: int | None = None
        self._preview_marker_y: int | None = None
        self._last_container_box: tuple[int, int, int, int] | None = None
        self._last_raw_frame = None
        self._source_frame_size: tuple[int, int] | None = None
        self._displayed_image_size: tuple[int, int] | None = None
        self._display_offsets = (0, 0)
        self._display_crop_origin = (0, 0)
        self._display_visible_size: tuple[int, int] | None = None
        self._zoom = 1.0
        self._zoom_center = (0.5, 0.5)
        self._pan_drag_start: tuple[int, int] | None = None
        self._pan_start_center = (0.5, 0.5)
        self._base_display_size: tuple[int, int] | None = None
        self._settings = load_settings()
        self._session_log = SessionLog(sample_seconds=self._settings.record_interval_seconds)
        self._session_baseline: float | None = None
        self._session_name = ""
        self._show_detail = False
        self._debug_window: DebugWindow | None = None
        self._timelapse: TimelapseWindow | None = None
        self._phone_pusher: PhonePusher | None = None
        self._phone_applied: tuple | None = None
        self._phone_status_text = ""
        self._live_state: dict = {}
        self._phase_tracker = PhaseTracker(
            plateau_seconds=self._settings.peak_minutes * 60,
            artifact_seconds=self._settings.artifact_minutes * 60,
        )
        self._forecaster = DoublingForecaster()
        self._forecast_saved_at = 0.0
        self._rate_text: str | None = None
        self._rate_text_at: float | None = None
        self._forecast_prediction: datetime | None = None  # when the doubling is expected: the graph draws it
        self._phase = "Aguardando sessão"
        self._paused = False  # the session is open but nothing is logged (the fermenter is being handled)
        self._mix_time: datetime | None = None
        self._graph_followed = None  # the session file the open chart follows as the current one
        self._line_height = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        try:
            self._connect_camera(load_camera_config())
        except (FileNotFoundError, ValueError) as exc:
            self._set_message(str(exc))
            self.status_value.configure(text="Configuração necessária", foreground="#a43d32")

        self._apply_phone_settings()
        self._tick_phone_status()
        self.root.after(40, self._refresh)
        if resume_last:
            self.root.after(200, self._resume_latest_session, resume_last if isinstance(resume_last, str) else None)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding=(10, 6))
        header.pack(fill="x")
        ttk.Label(header, text="LevainPeak", font=("TkDefaultFont", 13, "bold")).pack(side="left")
        self.status_value = ttk.Label(header, text="Aguardando conexão", font=("TkDefaultFont", 10, "bold"))
        self.status_value.pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        monitor_scroll = _ScrollFrame(self.notebook)
        settings_scroll = _ScrollFrame(self.notebook)
        monitor = monitor_scroll.inner
        settings_tab = ttk.Frame(settings_scroll.inner, padding=12)
        settings_tab.pack(fill="both", expand=True)
        self.notebook.add(monitor_scroll, text="Monitor")
        self.notebook.add(settings_scroll, text="Configurações")
        self._build_settings_tab(settings_tab)

        body = ttk.Frame(monitor, padding=(8, 0, 8, 6))
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body)
        side.pack(side="right", fill="y", padx=(8, 0))
        video_panel = ttk.Frame(body)
        video_panel.pack(side="left", fill="both", expand=True)
        # The picture keeps 16:9: the canvas takes the biggest such rectangle the panel leaves free above
        # the toolbar, the rotation row and the message, and the box shrinks to it so nothing is left between.
        video_box = ttk.Frame(video_panel, width=320, height=180)
        video_box.pack(fill="x")
        video_box.pack_propagate(False)
        self.video = tk.Canvas(video_box, background="#202723", highlightthickness=0, width=480, height=270)
        self.video.place(x=0, y=0, width=480, height=270)

        def fit_video(_event: tk.Event | None = None) -> None:
            below = sum(child.winfo_reqheight() for child in video_panel.winfo_children() if child is not video_box)
            width = max(160, video_panel.winfo_width())
            height = max(90, video_panel.winfo_height() - below - 6)
            if width * 9 > height * 16:
                width = height * 16 // 9
            else:
                height = width * 9 // 16
            if (video_box.winfo_height(), self.video.winfo_width()) != (height, width):
                video_box.configure(height=height)
                self.video.place(x=0, y=0, width=width, height=height)

        video_panel.bind("<Configure>", fit_video)
        self._fit_video = fit_video
        self._toolbar = ttk.Frame(video_panel)
        self._toolbar.pack(fill="x", pady=(6, 0))
        turn_row = ttk.Frame(video_panel)
        turn_row.pack(fill="x", pady=(4, 0))
        ttk.Label(turn_row, text="Girar").pack(side="left")
        self._rotation_var = tk.DoubleVar(value=self._fine_part(self._settings.rotation))
        self._rotation_text = tk.StringVar(value=f"{self._settings.rotation:.1f}°")
        self._turn_scale = ttk.Scale(
            turn_row, from_=-FINE_TURN_DEGREES, to=FINE_TURN_DEGREES,
            variable=self._rotation_var, command=self._fine_turned,
        )
        self._turn_scale.pack(side="left", fill="x", expand=True, padx=6)
        self._turn_scale.bind("<MouseWheel>", lambda e: self._fine_turned(self._rotation_var.get() + (0.1 if e.delta > 0 else -0.1)))
        self._turn_scale.bind("<Button-4>", lambda e: self._fine_turned(self._rotation_var.get() + 0.1))
        self._turn_scale.bind("<Button-5>", lambda e: self._fine_turned(self._rotation_var.get() - 0.1))
        ttk.Label(turn_row, textvariable=self._rotation_text, width=7, anchor="e").pack(side="left")
        ttk.Button(turn_row, text="Zerar", width=6, command=self._reset_rotation).pack(side="left", padx=(6, 0))
        ttk.Button(turn_row, text="Time-lapse", command=self._open_timelapse).pack(side="left", padx=(6, 0))
        self._mark_var = tk.BooleanVar(value=self._settings.mark_alignment)
        self._video_menu = tk.Menu(self.video, tearoff=False)
        self._video_menu.add_checkbutton(
            label='Detectar marca "+" ou "X" no pote (alinha a altura)',
            variable=self._mark_var, command=self._toggle_mark_alignment,
        )
        self._video_menu.add_separator()
        for degrees in ROTATIONS:
            self._video_menu.add_radiobutton(
                label="Imagem sem girar" if degrees == 0 else f"Girar a imagem {degrees}°",
                command=lambda degrees=degrees: self._turn_quarters(degrees),
            )
        self._shape_var = tk.StringVar(value=self._settings.mark_shape)
        self._video_menu.add_separator()
        for shape in MARK_SHAPES:
            self._video_menu.add_radiobutton(
                label=f"Marcar com {'círculo' if shape == 'círculo' else 'quadrado'}",
                variable=self._shape_var, value=shape, command=self._choose_mark_shape,
            )
        self.video.bind("<Button-3>", lambda event: self._video_menu.tk_popup(event.x_root, event.y_root))
        self._placeholder_item = self.video.create_text(
            240,
            135,
            text="Aguardando imagem da câmera…",
            fill="#f4f1e8",
            font=("TkDefaultFont", 13),
        )
        self.video.bind("<Configure>", self._center_video_items, add="+")
        self.video.bind("<Button-1>", self._set_marker_from_click, add="+")
        self.video.bind("<ButtonPress-1>", self._start_roi_draw, add="+")
        self.video.bind("<B1-Motion>", self._drag_video, add="+")
        self.video.bind("<ButtonRelease-1>", self._finish_roi_draw, add="+")
        self.video.bind("<Motion>", self._preview_marker, add="+")
        self.video.bind("<Motion>", self._preview_roi, add="+")
        self.video.bind("<Leave>", self._hide_marker_preview, add="+")
        self.video.bind("<MouseWheel>", self._on_mouse_wheel, add="+")
        self.video.bind("<Button-4>", self._on_mouse_wheel, add="+")
        self.video.bind("<Button-5>", self._on_mouse_wheel, add="+")
        self.root.bind("<Escape>", self._cancel_marker_mode, add="+")
        self.root.bind("<Return>", self._confirm_current_action, add="+")
        self.root.bind("<d>", self._toggle_detail, add="+")
        self.root.bind("<r>", self._toggle_reposition, add="+")
        self.root.bind("<Escape>", self._cancel_reposition, add="+")
        self.video.bind("<Button-1>", self._confirm_reposition, add="+")
        self.video.bind("<Motion>", self._preview_reposition, add="+")

        metrics = ttk.Frame(side)
        metrics.pack(fill="both", expand=True)
        self._metric_tiles = []
        self.metric_values = {}
        metric_data = (
            ("Sessão", "Nenhuma sessão iniciada"),
            ("Pote", "Aguardando calibração"),
            ("Volume relativo", "—"),
            ("Estado do levain", "Aguardando análise"),
            ("Temperatura", "Marque a região do visor"),
            ("Previsão do dobro", "Inicie uma sessão para estimar"),
        )
        for index, (title, value) in enumerate(metric_data):
            tile = ttk.LabelFrame(metrics, text=title, padding=(8, 3))
            label = ttk.Label(tile, text=value, anchor="nw", justify="left", wraplength=190)
            label.pack(fill="x", anchor="w")
            label.bind("<Configure>", self._fit_wraplength, add="+")
            self._metric_tiles.append(tile)
            self.metric_values[title] = label
        self._metrics_container = metrics
        self._metrics_container.columnconfigure(0, weight=1, minsize=190)
        for index, tile in enumerate(self._metric_tiles):
            tile.grid(row=index, column=0, sticky="nsew", pady=2)
        self._reserve_metric_rows(lines=2)

        footer = ttk.Frame(video_panel, padding=(0, 4, 0, 0))
        footer.pack(fill="x")
        # The message area always holds two lines, so a longer message never
        # pushes the rest of the window around.
        message_area = ttk.Frame(footer, height=2 * self._line_height + 2)
        message_area.pack(fill="x")
        message_area.pack_propagate(False)
        self.message = ttk.Label(message_area, text="", anchor="nw", justify="left")
        self.message.pack(fill="both", expand=True)
        # The status line: what the current action is and which keys finish it.
        self.instructions = ttk.Label(footer, text="", anchor="w", justify="left", foreground="#59645e")
        self.instructions.pack(fill="x", pady=(2, 0))
        footer.bind("<Configure>", self._wrap_footer_labels, add="+")

        items: list[tuple[tk.Widget, bool]] = []  # (widget, ends a group)

        def add(widget, hint: str | None, ends_group: bool = False):
            if hint:
                _Tooltip(widget, hint)
            items.append((widget, ends_group))
            return widget

        def icon_button(icon, command, hint, states=None, ends_group=False, state="normal"):
            widget = _IconButton(self._toolbar, states or {icon: (icon, hint)}, icon, command, state=state)
            return add(widget, None, ends_group)

        def menu_button(icon, hint, ends_group=False):
            widget = ttk.Menubutton(self._toolbar, text=icon, width=2, direction="below")
            return add(widget, hint, ends_group)

        self._auto_detect = False  # the pot is found by the automatic search while the drawing is kept aside
        self._auto_detect_var = tk.BooleanVar(value=False)
        self.draw_thermometer_button = icon_button("▭", lambda: self._toggle_roi_mode("thermometer"), "", {
            "Termômetro": ("▭", "Marcar a região do termômetro (arraste no vídeo)"),
            "Cancelar": ("✕", "Cancelar a marcação do termômetro (Esc)"),
        })
        self.draw_thermometer_button.configure(text="Termômetro")
        self.draw_face_button = icon_button("◫", lambda: self._toggle_outline_mode("thermometer"), "", {
            "Visor": ("◫", "Marcar o visor do termômetro (4 cantos)"),
            "Confirmar": ("✔", "Confirmar o visor (Enter)"),
        }, ends_group=True)
        self.draw_face_button.configure(text="Visor")
        vessel_button = menu_button("◯", "Recipiente: marcar pote, contorno, detecção automática")
        vessel_menu = tk.Menu(vessel_button, tearoff=False)
        vessel_menu.add_command(label="Marcar pote", command=lambda: self._toggle_roi_mode("pot"))
        self.draw_pot_button = _MenuEntry(
            vessel_menu, {"Pote": "Marcar pote", "Cancelar": "Cancelar marcação do pote"}
        )
        vessel_menu.add_command(label="Delimitar contorno", command=lambda: self._toggle_outline_mode("pot"))
        self.draw_outline_button = _MenuEntry(
            vessel_menu, {"Contorno": "Delimitar contorno", "Confirmar": "Confirmar contorno"}
        )
        vessel_menu.add_checkbutton(
            label="Detectar automaticamente (guarda o desenho)", variable=self._auto_detect_var,
            command=self._toggle_auto_detect, state="disabled",
        )
        self.auto_pot_button = _MenuEntry(vessel_menu, {})
        vessel_button.configure(menu=vessel_menu)
        self.bands_button = menu_button("▥", "Faixas de leitura da superfície", ends_group=True)
        bands_menu = tk.Menu(self.bands_button, tearoff=False)
        bands_menu.add_command(label="Desenhar faixa", command=lambda: self._toggle_roi_mode("bands"))
        bands_menu.add_command(label="Apagar faixas", command=self._clear_bands)
        self._method_var = tk.StringVar(value=self._settings.reading_method)
        for label, value in (
            ("Leitura: Automático (pote todo, cai para o central)", "automático"),
            ("Leitura: Pote todo (fixo)", "pote todo"),
            ("Leitura: Só as faixas (fixo)", "faixas"),
            ("Leitura: Central (fixo)", "central"),
        ):
            bands_menu.add_radiobutton(label=label, value=value, variable=self._method_var, command=self._choose_method)
        self._show_bands_var = tk.BooleanVar(value=self._settings.show_surface_bands)
        bands_menu.add_checkbutton(
            label="Sempre exibir as faixas", variable=self._show_bands_var, command=self._toggle_show_bands
        )
        self.bands_button.configure(menu=bands_menu)
        self.marker_button = icon_button("⚑", self._toggle_marker_mode, "", {
            "Marcar início": ("⚑", "Marcar o nível inicial (clique no pote)"),
            "Cancelar marcação": ("✕", "Cancelar a marcação (Esc)"),
        })
        self.marker_button.configure(text="Marcar início")
        self.detail_button = icon_button("◉", self._toggle_detail, "", {
            "Ver detecção": ("◉", "Ver a detecção (D)"),
            "Ocultar detecção": ("○", "Ocultar a detecção (D)"),
        }, ends_group=True)
        self.detail_button.configure(text="Ver detecção")
        self.session_button = icon_button("▶", self._toggle_session, "", {
            "Iniciar sessão": ("▶", "Iniciar a sessão"),
            "Encerrar sessão": ("■", "Encerrar a sessão"),
        })
        self.session_button.configure(text="Iniciar sessão")
        self.pause_button = icon_button("‖", self._toggle_pause, "", {
            "Pausar": ("‖", "Pausar o registro"),
            "Continuar": ("▶", "Continuar o registro"),
        }, state="disabled")
        self.pause_button.configure(text="Pausar")
        self.clear_button = icon_button("⌫", self._clear_session, "Limpar a sessão", ends_group=True)
        self.resume_button = menu_button("⟲", "Retomar uma sessão salva")
        self._resume_menu = tk.Menu(self.resume_button, tearoff=False, postcommand=self._fill_resume_menu)
        self.resume_button.configure(menu=self._resume_menu)
        self.open_csv_button = icon_button("▤", self._open_session_csv, "Abrir o CSV da sessão")
        self.graph_button = icon_button("∿", self._show_session_graph, "Gráfico da sessão atual")
        self.graph_pick_button = icon_button(
            "☰", lambda: self._show_session_graph(choose=True), "Gráfico de uma sessão salva…"
        )
        self.debug_button = icon_button("⌕", self._open_debug_view, "Diagnóstico da detecção", ends_group=True)
        # The mix time is a clickable label at the end of the bar.
        self.mix_time_button = ttk.Label(self._toolbar, text="Hora da mistura", cursor="hand2", foreground="#2f5d8a")
        self.mix_time_button.bind("<Button-1>", lambda _e: self._set_mix_time())
        add(self.mix_time_button, "Clique para definir a hora da mistura")
        self.ratio_button = ttk.Label(self._toolbar, text="Proporção", cursor="hand2", foreground="#2f5d8a")
        self.ratio_button.bind("<Button-1>", lambda _e: self._set_ratio())
        add(self.ratio_button, "Clique para mudar a proporção isca : água : farinha da sessão")

        def reflow(event: tk.Event | None = None) -> None:
            """Lay the bar out in as many rows as the width needs."""
            width = max(1, self._toolbar.winfo_width())
            row = x = 0
            for column, (widget, ends_group) in enumerate(items):
                need = widget.winfo_reqwidth() + (12 if ends_group else 4)
                if x and x + need > width:
                    row, x = row + 1, 0
                # A column of its own for each widget: columns are shared by every row, so a wide label
                # that wrapped to the start of a row would widen the cell of the button above it.
                widget.grid(row=row, column=column, padx=(0, 12 if ends_group else 4), pady=1, sticky="w")
                x += need

        self._toolbar.bind("<Configure>", reflow, add="+")
        self.root.after_idle(reflow)  # the first Configure may have come before the bind

    def _wrap_footer_labels(self, event: tk.Event) -> None:
        wrap = max(200, event.width - 8)
        for label in (self.instructions, self.message):
            if int(float(str(label.cget("wraplength")) or 0)) != wrap:
                label.configure(wraplength=wrap)

    @staticmethod
    def _fit_wraplength(event: tk.Event) -> None:
        """Wrap a label at its own width. Only touched when it changes, so the
        resulting size change cannot feed back into another resize."""
        label = event.widget
        wrap = max(120, event.width - 4)
        if int(float(str(label.cget("wraplength")) or 0)) != wrap:
            label.configure(wraplength=wrap)

    def _reserve_metric_rows(self, lines: int) -> None:
        """Make every metric row as tall as ``lines`` lines of text, so a text that
        wraps to more lines does not resize the rest of the window."""
        probe = self.metric_values["Sessão"]
        original, wrap = probe.cget("text"), probe.cget("wraplength")
        probe.configure(text="\n".join(["Ag"] * lines), wraplength=9999)
        self.root.update_idletasks()
        needed = probe.master.winfo_reqheight() + 4  # the grid pads each tile by 2 px
        probe.configure(text=original, wraplength=wrap)
        for row in range(len(self._metric_tiles)):
            self._metrics_container.rowconfigure(row, minsize=needed)

    def _annotate(self, frame, measurement):
        shown = draw_measurement(
            frame, measurement, detail=self._show_detail, faint_box=self._pot_outline is not None,
            outline=tuple(tuple(point) for point in self._pot_outline) if self._pot_outline else None,
        )
        shown = self._draw_bands(shown)
        seen = self._analyzer.mark_seen if self._analyzer is not None else None
        if seen is not None:  # the "+" or "X" the app is following, in red
            x, y, size = (int(round(v)) for v in seen)
            arm = max(12, size // 2)
            red = (40, 40, 255)
            layer = shown.copy()
            cv2.line(layer, (x - arm, y), (x + arm, y), red, 3, cv2.LINE_AA)
            cv2.line(layer, (x, y - arm), (x, y + arm), red, 3, cv2.LINE_AA)
            if self._settings.mark_shape == "quadrado":
                cv2.rectangle(layer, (x - arm, y - arm), (x + arm, y + arm), red, 3, cv2.LINE_AA)
            else:
                cv2.circle(layer, (x, y), arm, red, 3, cv2.LINE_AA)
            shown = cv2.addWeighted(layer, 0.4, shown, 0.6, 0)  # translucent: the mark below shows through
        return shown

    def _draw_bands(self, shown):
        """The surface strips over the picture: solid when the reading uses them, faint while they only wait."""
        drawing = self._roi_mode and self._roi_target == "bands"
        bands = self._band_override or self._settings.surface_bands
        if not bands or not self._pot_outline or not (drawing or self._settings.show_surface_bands):
            return shown
        span = inner_span(tuple(tuple(point) for point in self._pot_outline), (shown.shape[1], shown.shape[0]))
        if span is None:
            return shown
        left, right, top, bottom = span
        layer = shown.copy()
        for start, end in bands:
            x0, x1 = left + round(start * (right - left)), left + round(end * (right - left))
            cv2.rectangle(layer, (x0, top), (x1, bottom), (230, 200, 40), -1)
        alpha = 0.28 if self._active_bands() else 0.12
        return cv2.addWeighted(layer, alpha, shown, 1 - alpha, 0)

    def _toggle_detail(self, event: tk.Event | None = None) -> None:
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox)):
            return  # typing a "d" in a field must not toggle the overlay
        self._show_detail = not self._show_detail
        self.detail_button.configure(
            text="Ocultar detecção" if self._show_detail else "Ver detecção"
        )
        self._redraw_current_frame()

    def _session_summary(self, *, resumed: bool = False) -> str:
        """Up to three short lines for the session card."""
        lines = [self._session_name or "Sessão"]
        try:
            started = datetime.strptime(
                (self._session_log.session_id or "")[:15], "%Y%m%d_%H%M%S"
            ).strftime("%d/%m %H:%M")
            lines.append(f"Retomada de {started}" if resumed else f"Iniciada {started}")
        except ValueError:
            pass
        if self._mix_time is not None:
            lines.append(f"Mistura {self._mix_time.strftime('%d/%m %H:%M')}")
        return "\n".join(lines)

    def _running_infrared(self) -> bool:
        return self._analyzer is not None and self._analyzer.profile.infrared

    def _ink_cut_of_profile(self) -> float:
        """The black/white cut of the profile that is running (never the other one's)."""
        return self._settings.ink_cut_infrared if self._running_infrared() else float(self._settings.ink_cut_day)

    def _ink_control(self) -> dict | None:
        """What the Diagnóstico slider shows: 0-100%, where more means more of the display read as ink."""
        if self._temperature_quad is None and self._temperature_roi is None:
            return None
        if self._running_infrared():
            low, high = INK_CUT_INFRARED
            percent = (high - self._settings.ink_cut_infrared) / (high - low) * 100  # a lower cut reads fainter strokes
            return {
                "profile": "infravermelho", "percent": percent,
                "auto": abs(self._settings.ink_cut_infrared - DEFAULT_INK_CUT_INFRARED) < 1e-6,
            }
        low, high = INK_CUT_DAY
        return {
            "profile": "dia", "percent": (self._settings.ink_cut_day - low) / (high - low) * 100,
            "auto": self._settings.ink_cut_day == DEFAULT_INK_CUT_DAY,
        }

    def _set_ink_cut(self, percent: float | None, save: bool) -> None:
        """Slider moved: apply at once to the running profile; write to disk on release.

        ``percent`` None is the "auto" button: the running profile goes back to its default.
        """
        infrared = self._running_infrared()
        if percent is None:
            cut = DEFAULT_INK_CUT_INFRARED if infrared else DEFAULT_INK_CUT_DAY
        else:
            percent = min(100.0, max(0.0, percent))
            if infrared:
                low, high = INK_CUT_INFRARED
                cut = round(high - percent / 100 * (high - low), 2)  # a lower cut reads fainter strokes
            else:
                low, high = INK_CUT_DAY
                cut = round(low + percent / 100 * (high - low))
        field = "ink_cut_infrared" if infrared else "ink_cut_day"
        self._settings = replace(self._settings, **{field: cut}).clamped()
        if self._analyzer is not None:
            setattr(self._analyzer, field, getattr(self._settings, field))
        if save:
            try:
                save_settings(self._settings)
            except OSError:
                logging.exception("Falha ao salvar o corte do visor")

    def _persist_marker(self) -> None:
        """Keep the hand-placed initial-level mark in the session file."""
        if not self._session_log.active:
            return  # written when the session starts
        try:
            self._session_log.save_marker(self._manual_marker_y)
        except OSError:
            logging.exception("Falha ao salvar a marca de início")

    def _capture_pot_reference(self) -> None:
        """The pot as drawn is where it is now: remember how it looks, to find it if it is moved."""
        if self._analyzer is None or self._stream is None:
            return
        self._analyzer.set_reference(
            self._stream.snapshot()[1], self._pot_outline, self._manual_pot_roi, self._manual_marker_y
        )

    def _sync_analyzer_regions(self) -> None:
        """The window's pot (box and contour) is the one the reading uses. When they differ the reading is
        measuring something else (the automatic search, another pot) while the screen shows the drawing."""
        analyzer = self._analyzer
        if self._auto_detect or analyzer.aligned is not None or self._outline_mode or self._roi_mode or self._reposition_mode:
            return  # the reading just moved the drawing and the window has not taken it yet
        outline = None if self._pot_outline is None else tuple(tuple(point) for point in self._pot_outline)
        held = None if analyzer.outline is None else tuple(tuple(point) for point in analyzer.outline)
        roi = None if self._manual_pot_roi is None else tuple(self._manual_pot_roi)
        held_roi = None if analyzer.roi is None else tuple(analyzer.roi)
        if outline == held and roi == held_roi:
            return
        logging.warning("O pote da leitura (%s, %s) diferia do desenhado (%s, %s); reaplicado", held_roi, held, roi, outline)
        analyzer.outline, analyzer.roi = outline, roi

    def _take_alignment(self) -> None:
        """The analyzer carried the drawing to where the pot now is: follow it in the window and on disk."""
        aligned = self._analyzer.aligned if self._analyzer is not None else None
        if aligned is None:
            return
        self._analyzer.aligned = None
        old = self._pot_outline
        self._pot_outline, self._manual_pot_roi, marker = aligned
        if marker is not None:
            self._manual_marker_y = marker
        self._persist_marker()
        if self._session_log.active:
            self._session_log.save_regions(
                self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline,
                face_quad=self._temperature_quad,
            )
        text = "Pote reposicionado: o contorno e a marca acompanharam."
        if old:
            width = lambda outline: max(x for x, _ in outline) - min(x for x, _ in outline)
            dx = (sum(x for x, _ in self._pot_outline) - sum(x for x, _ in old)) // len(old)
            dy = (sum(y for _, y in self._pot_outline) - sum(y for _, y in old)) // len(old)
            size = (width(self._pot_outline) / max(1, width(old)) - 1) * 100
            logging.warning("Contorno do pote acompanhou o pote: deslocou %d,%d px e mudou %+.1f%% de largura", dx, dy, size)
            text = (
                f"Pote reposicionado: {dx:+d} px na horizontal, {dy:+d} px na vertical"
                f"{f', tamanho {size:+.0f}%' if abs(size) >= 1 else ''}. O contorno e a marca acompanharam."
            )
        logging.info(text)
        self._realigned_at = time.monotonic()
        self._set_message(text)

    def _take_visor_alignment(self) -> None:
        """The analyzer carried the thermometer area to where the thermometer now is: follow it."""
        aligned = self._analyzer.visor_aligned if self._analyzer is not None else None
        if aligned is None:
            return
        self._analyzer.visor_aligned = None
        self._temperature_roi, self._temperature_quad = aligned
        if self._session_log.active:
            self._session_log.save_regions(
                self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline,
                face_quad=self._temperature_quad,
            )
        logging.info("Termômetro reposicionado: a região do visor acompanhou.")
        self._set_message("Termômetro reposicionado: a região do visor acompanhou.")

    def _sync_vessel_bounds(self) -> None:
        """Keep what daylight measured about the vessel in the session file."""
        if self._analyzer is None or not self._session_log.active:
            return
        bounds = dict(self._analyzer.vessel_bounds)
        if bounds and bounds != self._session_log.vessel_bounds:
            try:
                self._session_log.save_vessel_bounds(bounds)
            except OSError:
                logging.exception("Falha ao salvar os limites do pote")

    def _open_timelapse(self) -> None:
        if self._timelapse is not None and self._timelapse.open:
            self._timelapse.lift()
            return
        sessions = recent_sessions(self._session_log.directory, self._session_log.path)
        if not sessions:
            messagebox.showinfo("Time-lapse", "Ainda não há fotos guardadas.")
            return
        current = self._session_log.path
        chosen = 0
        if current is not None:
            chosen = next((i for i, (_, p) in enumerate(sessions) if p and p[0].parent.name == f"{current.stem}_fotos"), 0)
        self._timelapse = TimelapseWindow(self.root, sessions, chosen, self._settings.rotation)
        self._timelapse.go_to(len(sessions[chosen][1]) - 1)

    def _open_debug_view(self) -> None:
        if self._debug_window is not None and self._debug_window.open:
            self._debug_window.lift()
            return
        self._debug_window = DebugWindow(self.root, self._debug_state, on_ink_cut=self._set_ink_cut)

    def _debug_state(self) -> dict:
        """Read from the diagnostics worker thread: only whole-object reads."""
        frame = self._stream.snapshot()[1] if self._stream is not None else None
        return {
            "frame": frame,
            "pot_roi": self._manual_pot_roi,
            "thermometer_roi": self._temperature_roi,
            "stable": self._measurement,
            "empty_pot": None if self._analyzer is None else self._analyzer.empty_pot,
            "temperature_c": self._temperature_c,
            "profile": None if self._analyzer is None else self._analyzer.profile,
            "outline": self._pot_outline,
            "face_quad": self._temperature_quad,
            "ink_cut": self._ink_cut_of_profile(),
            "surface_bands": self._active_bands(),  # only the bands the reading really uses
            "auto_strips": self._method_flags()[0],
            "only": self._method_flags()[1],
            "ink_control": self._ink_control(),
            "vertical_bounds": None if self._analyzer is None else self._analyzer.effective_bounds(),
        }

    def _reset_tracking(self, baseline: float | None = None) -> None:
        self._phase_tracker.reset(baseline)
        self._forecaster.reset()

    def _set_message(self, message: str) -> None:
        # Two lines is all the message area holds; cut a longer text with an ellipsis.
        font = tkfont.nametofont("TkDefaultFont")
        limit = 2 * max(200, int(float(str(self.message.cget("wraplength")) or 0))) * 0.9
        if font.measure(message) > limit:
            while message and font.measure(message + "…") > limit:
                message = message[:-1]
            message = message.rstrip() + "…"
        self.message.configure(text=message)

    def _mode_hint(self) -> str:
        if self._reposition_mode:
            return "Reposicionar: mova o mouse · roda redimensiona · clique confirma · Esc cancela"
        if self._outline_mode:
            return "Contorno: arraste os cantos ou um lado · Enter confirma · Esc cancela"
        if self._roi_mode:
            if self._roi_target == "bands":
                return "Faixas: arraste para criar · clique apaga · Esc sai"
            return "Marcar região: arraste sobre o vídeo · Esc cancela"
        if self._marker_mode:
            return "Nível inicial: clique no pote para marcar · Esc cancela"
        return "D detecção · R reposicionar · roda: zoom · botão direito: menu do vídeo"

    def _confirm_current_action(self, event: tk.Event | None = None) -> None:
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return  # Enter in a field belongs to the field
        if self._outline_mode:
            self._apply_outline()

    def _refresh(self) -> None:
        started = time.perf_counter()
        hint = self._mode_hint()
        if self.instructions.cget("text") != hint:
            self.instructions.configure(text=hint)
        drawing_pot = self._reposition_mode or (self._outline_mode and self._outline_target == "pot") or (
            self._roi_mode and self._roi_target == "pot"
        )
        if self._stream is not None:
            status = self._stream.status
            profile = self._analyzer.profile.name if self._analyzer is not None else None
            lost = self._analyzer is not None and self._analyzer.alignment_status
            if not lost and time.monotonic() - self._realigned_at < 90:
                lost = "pote reposicionado"
            self.status_value.configure(
                text=(status if profile is None else f"{status} · perfil {profile}")
                + (f" · {lost}" if lost else "")
            )
            if self._analyzer is not None:
                self._analyzer.align_enabled = not (self._roi_mode or self._outline_mode or self._reposition_mode)
                if drawing_pot and self._analyzer.detect_enabled:
                    self._measurement = None  # the automatic search is not shown while the pot is drawn
                    self._last_container_box = None
                self._analyzer.detect_enabled = not drawing_pot
                self._analyzer.join_enabled = self._session_log.active
                self._take_alignment()
                self._take_visor_alignment()
                self._sync_analyzer_regions()
            if status != self._last_stream_status:
                self._last_stream_status = status
                if self._measurement is None:
                    self._set_message(status)
            analysis_changed = False
            if self._analyzer is not None:
                result = self._analyzer.latest_result()
                if drawing_pot:
                    result = None
                if result is not None and self._analyzer.latest_key != self._analyzer.region_key():
                    result = None  # measured with a region you have since replaced: not about the new one
                if result is not None:
                    _, self._measurement, self._temperature_c = result
                    # An unread temperature repeats the last good reading.
                    self._temperature_filled = self._temperature_c is None
                    if self._temperature_c is not None:
                        self._last_known_temperature = self._temperature_c
                    else:
                        self._temperature_c = self._last_known_temperature
                    self._update_temperature_label()
                    self._sync_vessel_bounds()
                    self._handle_container_change(self._measurement)
                    if (
                        self._measurement is not None and not self._measurement.empty
                        and self._measurement.volume_below_profile
                        and (
                            self._manual_marker_y is None
                            or (self._marker_automatic and not self._session_log.active)
                        )
                    ):
                        # The initial mark sits at the surface the detection found, following it until the
                        # session starts; "Marcar início" is for placing it by hand.
                        self._manual_marker_y = int(self._measurement.surface_y)
                        self._marker_automatic = True
                        if self._session_log.active:  # the first reading of the session fixes it
                            self._persist_marker()
                            self._capture_pot_reference()
                    if self._measurement is not None and self._manual_marker_y is not None and not self._baseline_locked:
                        self._measurement = set_initial_marker(
                            self._measurement, self._manual_marker_y,
                            shift=self._analyzer.volume_shift if self._analyzer else 0.0,
                            manual=not self._marker_automatic,
                        )
                    if (
                        self._measurement is not None
                        and self._session_log.active
                    ):
                        if self._session_baseline is None:
                            self._baseline_locked = False
                            self._session_baseline = (
                                self._measurement.initial_volume_fraction
                                if self._measurement.initial_volume_fraction is not None
                                else self._measurement.volume_fraction
                            )
                            self._reset_tracking(self._session_baseline)
                        live = self._measurement.initial_volume_fraction
                        if self._manual_marker_y is not None and live is not None and not self._baseline_locked:
                            # The mark is read in the same frame as the volume. A level stored
                            # earlier (or read back from the CSV) was made with another shift.
                            if abs(live - self._session_baseline) > 1e-6:
                                self._session_baseline = live
                                self._phase_tracker.set_baseline(live)
                        else:
                            self._measurement = set_initial_volume_reference(
                                self._measurement, self._session_baseline
                            )
                    self._update_session(self._measurement)
                    self._update_measurement_labels()
                    analysis_changed = True
                    self._publish_live_state()
            frame_number, frame = self._stream.snapshot()
            if frame is not None and (
                frame_number != self._last_displayed_frame_number or analysis_changed
            ):
                self._last_displayed_frame_number = frame_number
                shown = self._annotate(frame, self._measurement) if self._measurement else frame
                shown = self._apply_region_mask(shown)
                self._show_frame(shown, source_frame=frame)
        # Leave the mouse room: wait at least 40 ms, and longer after a slow pass.
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.root.after(int(min(250, max(40, 1.5 * elapsed_ms))), self._refresh)

    def _update_measurement_labels(self) -> None:
        if self._measurement is None and self._analyzer is not None and self._analyzer.empty_pot is not None:
            self.metric_values["Pote"].configure(text="Pote vazio")
            self.metric_values["Volume relativo"].configure(text="—", foreground="")
            self.metric_values["Estado do levain"].configure(text="Sem levain no pote")
            if not self._marker_mode and not self._roi_mode and not self._outline_mode:
                self._set_message("O contorno do pote está marcado e não há levain dentro dele.")
            return
        if self._measurement is None:
            self.metric_values["Pote"].configure(text="Procurando recipiente…")
            self.metric_values["Volume relativo"].configure(text="—", foreground="")
            self.metric_values["Estado do levain"].configure(text="Aguardando recipiente visível")
            if not (self._roi_mode or self._outline_mode or self._marker_mode):
                self._set_message("A câmera está ativa; não encontrei um contorno de pote confiável neste frame.")
            return

        measurement = self._measurement
        self.metric_values["Pote"].configure(text="Recipiente detectado")
        # The same reading again and again may be a frozen picture or a stuck measurement: say so, in orange.
        repeated = measurement.growth_factor is not None and self._session_log.growth_repeats >= REPEAT_WARNING
        self.metric_values["Volume relativo"].configure(
            text=self._volume_text(
                measurement, self._session_log.last_growth_factor, self._session_log.growth_repeats
            ),
            foreground="#b45a1a" if repeated else "",
        )
        self.metric_values["Estado do levain"].configure(text=self._phase)
        marker_text = "marca ajustada manualmente" if measurement.marker_manual else "faixa inicial detectada"
        if measurement.marker_y is None:
            marker_text = "sem marca inicial detectada"
        if not self._marker_mode and not self._roi_mode:
            self._set_message(f"Recipiente detectado; {marker_text}.")

    def _update_temperature_label(self) -> None:
        if self._temperature_c is None:
            text = "Não detectada" if self._temperature_roi else "Marque a região do visor"
        else:
            text = f"{self._temperature_c:.1f} °C"
            age = None if self._analyzer is None else self._analyzer.temperature_age_seconds()
            if self._temperature_filled or (age is not None and age > 60):
                text += "\núltima leitura"
                if age is not None and age >= 60:
                    text += f" há {int(age // 60)} min"
        self.metric_values["Temperatura"].configure(text=text)

    @staticmethod
    def _volume_text(
        measurement: ContainerMeasurement, previous: float | None = None, repeats: int = 1
    ) -> str:
        capacity = f"≈ {measurement.volume_fraction:.0%} da capacidade"
        if measurement.growth_factor is not None:
            text = f"{capacity}\n≈ {measurement.growth_factor:.2f}× do nível inicial"
            if previous is not None:  # the last row written: "3.02 (5x)" when it came up several times
                text += f"\nanterior {previous:.2f}×" + (f" ({repeats}x)" if repeats > 1 else "")
                if repeats >= REPEAT_WARNING:
                    text += " ⚠ repetida"
            return text
        return capacity

    def _handle_container_change(self, measurement: ContainerMeasurement | None) -> None:
        if measurement is None:
            return
        current = (measurement.left, measurement.top, measurement.right, measurement.bottom)
        previous = self._last_container_box
        if previous is not None and self._source_frame_size is not None:
            frame_width, frame_height = self._source_frame_size
            changed = (
                abs(current[0] - previous[0]) > frame_width * 0.05
                or abs(current[1] - previous[1]) > frame_height * 0.07
                or abs(current[2] - previous[2]) > frame_width * 0.05
                or abs(current[3] - previous[3]) > frame_height * 0.07
            )
            if changed:
                self._manual_marker_y = None
                self._persist_marker()
        self._last_container_box = current

    def _marker_base(self):
        """What the mark is placed on: the reading, or the outline read as empty (the hand knows better)."""
        if self._measurement is not None:
            return self._measurement
        return self._analyzer.empty_pot if self._analyzer is not None else None

    def _toggle_marker_mode(self) -> None:
        if self._marker_mode:
            self._cancel_marker_mode()
            return
        if self._marker_base() is None:
            self._set_message("Aguarde a detecção do pote antes de marcar o nível inicial.")
            return
        if self._roi_mode:
            self._cancel_roi_mode()
        self._marker_mode = True
        self.marker_button.configure(text="Cancelar marcação")
        self.video.configure(cursor="crosshair")
        self._set_message("Passe o mouse pelo pote para pré-visualizar a linha e clique para confirmar.")

    def _cancel_marker_mode(self, _event: tk.Event | None = None) -> None:
        self._marker_mode = False
        self._preview_marker_y = None
        if hasattr(self, "marker_button"):
            self.marker_button.configure(text="Marcar início")
        if hasattr(self, "video"):
            self.video.configure(cursor="")
        self._hide_marker_preview()
        self._cancel_roi_mode()

    def _active_bands(self) -> tuple:
        """The strips the analyzer reads: the drawn ones, when the option is on and there are enough."""
        settings = self._settings
        return settings.surface_bands if settings.reading_method == "faixas" and len(settings.surface_bands) >= MIN_SURFACE_BANDS else ()

    def _method_flags(self) -> tuple[bool, str]:
        """(automatic strips on, the one fixed method or "") for the chosen reading method."""
        method = self._settings.reading_method
        return method in ("automático", "pote todo"), {"pote todo": "pote", "faixas": "faixas", "central": "central"}.get(method, "")

    def _apply_method(self) -> None:
        auto, only = self._method_flags()
        self._analyzer.surface_bands = self._active_bands()
        self._analyzer.auto_strips = auto
        self._analyzer.only_method = only

    def _set_bands(self, bands: tuple, use: bool | None = None) -> None:
        """Store the drawn bands; ``use`` False takes the reading off "faixas" (to automatic), True puts it on."""
        method = self._settings.reading_method
        if use is True:
            method = "faixas"
        elif use is False and method == "faixas":
            method = "automático"
        self._settings = replace(self._settings, surface_bands=bands, reading_method=method).clamped()
        self._method_var.set(self._settings.reading_method)
        if self._analyzer is not None:
            auto, only = self._method_flags()
            self._analyzer.set_surface_bands(self._active_bands(), auto=auto, only=only)
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar as faixas")
        self._redraw_current_frame()

    def _band_span(self) -> tuple[int, int, int, int] | None:
        if not self._pot_outline or self._source_frame_size is None:
            return None
        return inner_span(tuple(tuple(point) for point in self._pot_outline), self._source_frame_size)

    def _band_at(self, x: int) -> int | None:
        """The drawn band under picture column ``x`` (a few pixels of reach), or None."""
        span = self._band_span()
        if span is None:
            return None
        width = span[1] - span[0]
        reach = max(3, round(8 / self._zoom))
        for index, (start, end) in enumerate(self._settings.surface_bands):
            if span[0] + start * width - reach <= x <= span[0] + end * width + reach:
                return index
        return None

    def _moved_bands(self, x: int) -> tuple:
        """The bands with the dragged one shifted by the mouse's travel, kept inside the glass."""
        index, start_x, (start, end) = self._band_drag
        span = self._band_span()
        shift = 0.0 if span is None else (x - start_x) / (span[1] - span[0])
        shift = min(1.0 - end, max(-start, shift))
        bands = list(self._settings.surface_bands)
        bands[index] = (start + shift, end + shift)
        return tuple(bands)

    def _finish_band_move(self, x: int) -> None:
        """Released after dragging a band: a short click removes it, a real drag leaves it where it is."""
        index, start_x, _ = self._band_drag
        moved = self._moved_bands(x)
        self._band_drag = self._band_override = None
        if abs(x - start_x) < max(3, round(4 / self._zoom)):
            bands = tuple(b for i, b in enumerate(self._settings.surface_bands) if i != index)
            self._set_bands(bands)
            self._set_message(
                f"Faixa apagada ({len(bands)} restante(s))."
                + (f" Com menos de {MIN_SURFACE_BANDS} a leitura volta a olhar o pote todo." if len(bands) < MIN_SURFACE_BANDS else "")
            )
            return
        self._set_bands(moved)
        self._set_message("Faixa movida.")

    def _add_band(self, left: int, right: int) -> None:
        span = None if not self._pot_outline or self._source_frame_size is None else inner_span(
            tuple(tuple(point) for point in self._pot_outline), self._source_frame_size
        )
        if span is None:
            self._set_message("Desenhe o contorno do pote antes de marcar as faixas.")
            return
        bands = self._settings.surface_bands
        if len(bands) >= MAX_SURFACE_BANDS:
            self._set_message(f"No máximo {MAX_SURFACE_BANDS} faixas: apague as faixas para refazer.")
            return
        width = span[1] - span[0]
        start, end = (left - span[0]) / width, (right - span[0]) / width
        if min(end, 1.0) - max(start, 0.0) < 0.012:
            self._set_message("A faixa ficou fora do pote ou estreita demais (menos de 4 px); arraste dentro do vidro.")
            return
        self._set_bands((*bands, (start, end)))
        count = len(self._settings.surface_bands)
        self._set_message(
            f"{count} faixa(s). " + (
                "Marque pelo menos mais uma." if count < MIN_SURFACE_BANDS
                else 'Escolha "Só as faixas" no menu para usá-las.' if self._settings.reading_method != "faixas"
                else "A leitura usa só as faixas."
            )
        )

    def _choose_method(self) -> None:
        method = str(self._method_var.get())
        if method == "faixas" and len(self._settings.surface_bands) < MIN_SURFACE_BANDS:
            self._method_var.set(self._settings.reading_method)
            self._set_message(f"Desenhe pelo menos {MIN_SURFACE_BANDS} faixas antes de ler só as faixas.")
            return
        self._settings = replace(self._settings, reading_method=method).clamped()
        if self._analyzer is not None:
            auto, only = self._method_flags()
            self._analyzer.set_surface_bands(self._active_bands(), auto=auto, only=only)
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar o método de leitura")
        self._set_message(f"Método de leitura: {method}.")

    def _toggle_show_bands(self) -> None:
        self._settings = replace(self._settings, show_surface_bands=bool(self._show_bands_var.get())).clamped()
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar a exibição das faixas")
        self._redraw_current_frame()

    def _clear_bands(self) -> None:
        if self._settings.surface_bands and not messagebox.askyesno(
            "Apagar faixas",
            f"Apagar as {len(self._settings.surface_bands)} faixas desenhadas?\nA leitura volta a olhar o pote todo.",
            parent=self.root,
        ):
            return
        self._set_bands((), use=False)
        self._set_message("Faixas apagadas: a leitura volta a olhar o pote todo.")

    def _toggle_roi_mode(self, target: str) -> None:
        self._cancel_outline_mode()
        if self._roi_mode and self._roi_target == target:
            self._cancel_roi_mode()
            return
        if self._marker_mode:
            self._cancel_marker_mode()
        self._roi_mode = True
        self._roi_target = target
        self._roi_drag_start = None
        self.draw_pot_button.configure(
            text="Cancelar" if target == "pot" else "Pote"
        )
        self.draw_thermometer_button.configure(
            text="Cancelar" if target == "thermometer" else "Termômetro"
        )
        self.video.configure(cursor="crosshair")
        if target == "bands":
            self._set_message(
                f"Arraste na horizontal, sobre o pote, uma faixa estreita onde a luz é estável "
                f"({MIN_SURFACE_BANDS} a {MAX_SURFACE_BANDS}). Clique numa faixa para apagá-la, "
                f"arraste uma faixa para movê-la. Esc sai."
            )
            return
        target_text = "pote" if target == "pot" else "visor do termômetro"
        self._set_message(f"Arraste uma área um pouco maior que o {target_text}.")

    def _toggle_outline_mode(self, target: str = "pot") -> None:
        """First press: put four corners on the picture to adjust. Second press: use them.

        ``target`` is the pot's inner contour or the thermometer's LCD face.
        """
        if self._outline_mode and self._outline_target == target:
            self._apply_outline()
            return
        if self._source_frame_size is None:
            self._set_message("Aguarde a imagem da câmera para desenhar o contorno do pote.")
            return
        if self._marker_mode:
            self._cancel_marker_mode()
        self._cancel_roi_mode()
        current = self._pot_outline if target == "pot" else self._temperature_quad
        self._outline_target = target
        self._outline_points = [list(point) for point in (current or self._default_outline(target))]
        self._outline_mode = True
        self._outline_drag = None
        (self.draw_outline_button if target == "pot" else self.draw_face_button).configure(text="Confirmar")
        self.video.configure(cursor="crosshair")
        self._set_message(
            "Arraste os cantos até o contorno interno do pote (a parede do vidro). "
            "Confirmar usa o contorno; Esc cancela." if target == "pot" else
            "Arraste os cantos até a face do visor (o vidro claro com os números, sem a moldura). "
            "Confirmar usa; Esc cancela."
        )

    def _default_outline(self, target: str = "pot") -> tuple[tuple[int, int], ...]:
        """Corners inside the region of the pot (a trapezoid) or of the display (a rectangle), to start from."""
        width, height = self._source_frame_size
        if target == "thermometer":
            if self._temperature_roi is not None:
                left, top, right, bottom = self._temperature_roi
            else:
                left, top, right, bottom = width * 0.1, height * 0.45, width * 0.3, height * 0.75
            span_x, span_y = right - left, bottom - top
            return (
                (int(left + 0.12 * span_x), int(top + 0.15 * span_y)),
                (int(right - 0.12 * span_x), int(top + 0.15 * span_y)),
                (int(right - 0.12 * span_x), int(bottom - 0.15 * span_y)),
                (int(left + 0.12 * span_x), int(bottom - 0.15 * span_y)),
            )
        if self._manual_pot_roi is not None:
            left, top, right, bottom = self._manual_pot_roi
        else:
            left, top, right, bottom = width * 0.3, height * 0.2, width * 0.7, height * 0.9
        span_x, span_y = right - left, bottom - top
        return (
            (int(left + 0.06 * span_x), int(top + 0.08 * span_y)),
            (int(right - 0.06 * span_x), int(top + 0.08 * span_y)),
            (int(right - 0.18 * span_x), int(bottom - 0.06 * span_y)),
            (int(left + 0.18 * span_x), int(bottom - 0.06 * span_y)),
        )

    def _cancel_outline_mode(self) -> None:
        was_active = self._outline_mode
        self._outline_mode = False
        self._outline_drag = None
        self._outline_edge_drag = None
        if hasattr(self, "draw_outline_button"):
            self.draw_outline_button.configure(text="Contorno")
            self.draw_face_button.configure(text="Visor")
        if was_active and hasattr(self, "video"):
            self.video.configure(cursor="")

    def _outline_handle_at(self, point: tuple[int, int]) -> int | None:
        reach = max(8, round(40 / self._zoom))  # picture pixels; the corners are drawn with a 12 px radius at no zoom
        distances = [
            (float(np.hypot(point[0] - x, point[1] - y)), index)
            for index, (x, y) in enumerate(self._outline_points)
        ]
        distance, index = min(distances)
        return index if distance <= reach else None

    def _outline_edge_at(self, point: tuple[int, int]) -> tuple | None:
        """The side of the outline under ``point`` (picture pixels), as the state ``_outline_edge_drag`` keeps."""
        reach = max(6, round(18 / self._zoom))
        count = len(self._outline_points)
        best: tuple[float, int] | None = None
        for first in range(count):
            a = np.asarray(self._outline_points[first], dtype=np.float64)
            b = np.asarray(self._outline_points[(first + 1) % count], dtype=np.float64)
            side = b - a
            length = float(np.hypot(*side))
            if length < 1:
                continue
            along = float(np.clip(np.dot(np.asarray(point) - a, side) / length ** 2, 0.0, 1.0))
            distance = float(np.hypot(*(np.asarray(point) - (a + along * side))))
            if distance <= reach and (best is None or distance < best[0]):
                best = (distance, first)
        if best is None:
            return None
        first, second = best[1], (best[1] + 1) % count
        a = np.asarray(self._outline_points[first], dtype=np.float64)
        b = np.asarray(self._outline_points[second], dtype=np.float64)
        side = b - a
        normal = np.array([-side[1], side[0]]) / float(np.hypot(*side))
        return first, second, (a, b), normal, point

    def _move_outline_edge(self, event: tk.Event) -> None:
        if self._outline_edge_drag is None or self._source_frame_size is None:
            return
        point = self._source_point(event, clamp=True)
        if point is None:
            return
        first, second, (a, b), normal, start = self._outline_edge_drag
        shift = float(np.dot(np.asarray(point, dtype=np.float64) - np.asarray(start, dtype=np.float64), normal))
        width, height = self._source_frame_size
        low, high = (0 - a - 0, np.array([width - 1, height - 1]) - a)  # the room left for corner ``a``
        low_b, high_b = (0 - b, np.array([width - 1, height - 1]) - b)
        for axis in (0, 1):  # keep both corners inside the picture
            if normal[axis] > 1e-9:
                shift = min(shift, high[axis] / normal[axis], high_b[axis] / normal[axis])
                shift = max(shift, low[axis] / normal[axis], low_b[axis] / normal[axis])
            elif normal[axis] < -1e-9:
                shift = max(shift, high[axis] / normal[axis], high_b[axis] / normal[axis])
                shift = min(shift, low[axis] / normal[axis], low_b[axis] / normal[axis])
        self._outline_points[first] = [int(round(a[0] + shift * normal[0])), int(round(a[1] + shift * normal[1]))]
        self._outline_points[second] = [int(round(b[0] + shift * normal[0])), int(round(b[1] + shift * normal[1]))]
        self._redraw_current_frame()

    def _move_outline_handle(self, event: tk.Event) -> None:
        if self._outline_edge_drag is not None:
            self._move_outline_edge(event)
            return
        if self._outline_drag is None or self._source_frame_size is None:
            return
        point = self._source_point(event, clamp=True)
        if point is None:
            return
        width, height = self._source_frame_size
        self._outline_points[self._outline_drag] = [
            int(np.clip(point[0], 0, width - 1)), int(np.clip(point[1], 0, height - 1)),
        ]
        self._redraw_current_frame()

    @staticmethod
    def _outline_is_valid(points: list[list[int]], min_area: float = 80 * 120) -> bool:
        """A simple quadrilateral of a sensible size: no crossed sides, not a sliver."""
        polygon = np.asarray(points, dtype=np.float64)
        if len(polygon) < 3:
            return False
        area = 0.5 * abs(np.dot(polygon[:, 0], np.roll(polygon[:, 1], -1)) - np.dot(polygon[:, 1], np.roll(polygon[:, 0], -1)))
        if area < min_area:
            return False
        signs = []
        for index in range(len(polygon)):
            a, b, c = polygon[index], polygon[(index + 1) % len(polygon)], polygon[(index + 2) % len(polygon)]
            signs.append(np.sign((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])))
        return abs(sum(signs)) == len(signs)  # all turns the same way: convex, not crossed

    @staticmethod
    def _fine_part(rotation: float) -> float:
        """What is left of the angle after the whole quarter turns: what the slider shows."""
        return round(rotation - round(rotation / 90) * 90, 1)

    def _fine_turned(self, value) -> None:
        fine = min(FINE_TURN_DEGREES, max(-FINE_TURN_DEGREES, float(value)))
        quarter = round(self._settings.rotation / 90) * 90
        self._set_rotation(quarter + round(fine, 1))

    def _turn_quarters(self, degrees: int) -> None:
        if degrees == 0 and round(self._settings.rotation / 90) != 0 and not messagebox.askyesno(
            "Imagem sem girar",
            "Tirar a volta de 90° da imagem?\n"
            "O visor acompanha a imagem; o pote fica onde está na tela e talvez precise de ajuste.",
            parent=self.root,
        ):
            return
        self._set_rotation(degrees + self._fine_part(self._settings.rotation))

    def _reset_rotation(self) -> None:
        if self._settings.rotation == 0:
            return
        if messagebox.askyesno(
            "Zerar o ângulo",
            f"Voltar a imagem de {self._settings.rotation:.1f}° para 0°?\n"
            "O visor acompanha a imagem; o pote fica onde está na tela e talvez precise de ajuste.",
            parent=self.root,
        ):
            self._set_rotation(0)

    def _set_rotation(self, value: float, turn_visor: bool = True) -> None:
        degrees = Settings(rotation=value).clamped().rotation
        self._rotation_var.set(self._fine_part(degrees))
        if degrees == self._settings.rotation:
            return
        self._rotation_text.set(f"{degrees:.1f}°")
        if turn_visor:
            self._turn_visor(self._settings.rotation, degrees)
        self._settings = replace(self._settings, rotation=degrees).clamped()
        if self._stream is not None:
            self._stream.rotation = self._settings.rotation
        if self._session_log.active:
            try:
                self._session_log.save_rotation(degrees)
            except OSError:
                logging.exception("Falha ao salvar o ângulo na sessão")
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar a rotação da imagem")

    def _turn_visor(self, old: float, new: float) -> None:
        """The visor area and corners turn with the picture, so they stay on the thermometer."""
        if self._temperature_roi is None or self._source_frame_size is None:
            return
        self._temperature_roi, self._temperature_quad = rotate_visor(
            self._temperature_roi, self._temperature_quad, old, new, self._source_frame_size
        )
        if self._session_log.active:
            try:
                self._session_log.save_regions(
                    self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline,
                    face_quad=self._temperature_quad,
                )
            except OSError:
                logging.exception("Falha ao salvar o visor girado")

    def _choose_mark_shape(self) -> None:
        self._settings = replace(self._settings, mark_shape=self._shape_var.get()).clamped()
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar o formato da marca")
        self._redraw_current_frame()

    def _toggle_mark_alignment(self) -> None:
        enabled = bool(self._mark_var.get())
        self._settings = replace(self._settings, mark_alignment=enabled)
        if self._analyzer is not None:
            self._analyzer.use_mark = enabled
            # References taken without the mark have none to compare with: take them again.
            if enabled and self._analyzer.outline is not None:
                self._capture_pot_reference()
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar a opção da marca")
        self._set_message(
            'Marca "+" ou "X" ligada: o app a procura no pote, de dia.' if enabled else "Marca no pote desligada."
        )

    def _apply_outline(self) -> None:
        pot = self._outline_target == "pot"
        if not self._outline_is_valid(self._outline_points, 80 * 120 if pot else 40 * 20):
            self._set_message("Os cantos ficaram cruzados ou o desenho ficou pequeno demais; ajuste.")
            return
        if not pot:
            self._apply_face_quad()
            return
        outline = tuple((int(x), int(y)) for x, y in self._outline_points)
        width, height = self._source_frame_size
        xs, ys = [x for x, _ in outline], [y for _, y in outline]
        margin = int(0.03 * max(max(xs) - min(xs), max(ys) - min(ys)))
        roi = (max(0, min(xs) - margin), max(0, min(ys) - margin),
               min(width, max(xs) + margin), min(height, max(ys) + margin))
        kept = self._manual_pot_roi
        if kept is not None and kept[0] <= roi[0] and kept[1] <= roi[1] and kept[2] >= roi[2] and kept[3] >= roi[3]:
            roi = tuple(kept)  # the box the user drew around the pot still holds the new contour
        self._cancel_outline_mode()
        self._end_auto_detect()
        self._pot_outline = outline
        self._manual_pot_roi = roi
        if self._analyzer is not None:
            self._analyzer.roi = roi
            self._analyzer.outline = outline
            self._analyzer.vessel_bounds.clear()
        self._manual_marker_y = None
        self._persist_marker()
        self._capture_pot_reference()
        self._last_container_box = None
        self._measurement = None
        self.auto_pot_button.configure(state="normal")
        if self._session_log.active:
            kept_baseline = self._session_baseline
            self._session_baseline = None
            self._reset_tracking()
            self._session_log.save_regions(roi, self._temperature_roi, outline=outline)
            # A new contour reads the same levain at another level: join the series at the last one
            # logged, and keep the session's initial level, so the growth factor goes on from where it was.
            if self._analyzer is not None and self._session_log.last_volume is not None:
                self._analyzer.anchor_volume = self._session_log.last_volume
                self._analyzer.rejoin()
                if kept_baseline is not None:
                    self._session_baseline = kept_baseline
                    self._reset_tracking(kept_baseline)
        self._set_message("Contorno do pote definido. Procurando o nível do levain dentro dele.")

    def _apply_face_quad(self) -> None:
        quad = tuple((int(x), int(y)) for x, y in self._outline_points)
        width, height = self._source_frame_size
        xs, ys = [x for x, _ in quad], [y for _, y in quad]
        margin = int(0.12 * max(max(xs) - min(xs), max(ys) - min(ys)))
        roi = (max(0, min(xs) - margin), max(0, min(ys) - margin),
               min(width, max(xs) + margin), min(height, max(ys) + margin))
        kept = self._temperature_roi
        if kept is not None and kept[0] <= min(xs) and kept[1] <= min(ys) and kept[2] >= max(xs) and kept[3] >= max(ys):
            roi = tuple(kept)  # the box the user drew around the display still holds the new corners
        self._cancel_outline_mode()
        self._temperature_quad = quad
        self._temperature_roi = roi
        if self._analyzer is not None:
            self._analyzer.temperature_roi = roi
            self._analyzer.temperature_quad = quad
        if self._session_log.active:
            self._session_log.save_regions(
                self._manual_pot_roi, roi, outline=self._pot_outline, face_quad=quad
            )
        self._temperature_c = None
        self._update_temperature_label()
        self._set_message("Face do visor definida; procurando a temperatura.")

    def _cancel_roi_mode(self) -> None:
        self._cancel_outline_mode()
        self._roi_mode = False
        self._roi_drag_start = None
        self._band_drag = self._band_override = None
        if hasattr(self, "draw_pot_button"):
            self.draw_pot_button.configure(text="Pote")
        if hasattr(self, "draw_thermometer_button"):
            self.draw_thermometer_button.configure(text="Termômetro")
        if hasattr(self, "video"):
            self.video.configure(cursor="")
        if self._roi_preview_item is not None:
            self.video.itemconfigure(self._roi_preview_item, state="hidden")

    def _start_roi_draw(self, event: tk.Event) -> None:
        if self._outline_mode:
            point = self._source_point(event)
            self._outline_drag = None if point is None else self._outline_handle_at(point)
            self._outline_edge_drag = None
            if point is not None and self._outline_drag is None:
                self._outline_edge_drag = self._outline_edge_at(point)  # not on a corner: the side under the pointer
            return
        if not self._roi_mode:
            if self._zoom > 1.0 and not self._marker_mode and not self._reposition_mode:
                self._pan_drag_start = (event.x, event.y)
                self._pan_start_center = self._zoom_center
                self.video.configure(cursor="fleur")
            return
        point = self._source_point(event)
        if point is None:
            return
        self._roi_drag_start = point
        self._band_drag = None
        if self._roi_target == "bands":
            hit = self._band_at(point[0])
            if hit is not None:
                self._band_drag = (hit, point[0], self._settings.surface_bands[hit])

    def _toggle_reposition(self, event: tk.Event | None = None) -> None:
        """"r": the drawn pot follows the mouse and the wheel resizes it; click to place it, Esc to give up."""
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox)):
            return  # typing an "r" in a field
        if self._reposition_mode:
            self._cancel_reposition()
            return
        if self._pot_outline is None or self._source_frame_size is None:
            self._set_message("Desenhe o contorno do pote antes de reposicioná-lo (botão Contorno).")
            return
        if self._outline_mode or self._roi_mode or self._marker_mode:
            self._set_message("Termine o desenho atual antes de reposicionar (Esc cancela).")
            return
        self._reposition_mode = True
        self._reposition_scale = 1.0
        self._reposition_center = None
        self.video.configure(cursor="fleur")
        self._set_message("Reposicionar: mova o mouse para levar o desenho ao pote, a roda muda o tamanho, clique confirma, Esc cancela.")

    def _cancel_reposition(self, event: tk.Event | None = None) -> None:
        if not self._reposition_mode:
            return
        self._reposition_mode = False
        self._reposition_center = None
        self._reposition_scale = 1.0
        self.video.configure(cursor="")
        if event is not None or self._reposition_mode is False:
            self._set_message("Reposicionamento cancelado.")
        self._redraw_current_frame()

    def _repositioned(self):
        """The contour, its search box and the level mark as they would be placed now, or None."""
        if not self._pot_outline or self._reposition_center is None:
            return None
        points = np.asarray(self._pot_outline, np.float32)
        origin = points.mean(axis=0)
        px, py = self._reposition_center
        scale = self._reposition_scale

        def move(x: float, y: float) -> tuple[int, int]:
            return round(px + scale * (x - origin[0])), round(py + scale * (y - origin[1]))

        outline = tuple(move(x, y) for x, y in points)
        roi = None
        if self._manual_pot_roi is not None and self._source_frame_size is not None:
            width, height = self._source_frame_size
            left, top = move(self._manual_pot_roi[0], self._manual_pot_roi[1])
            right, bottom = move(self._manual_pot_roi[2], self._manual_pot_roi[3])
            roi = (max(0, left), max(0, top), min(width, right), min(height, bottom))
        marker = None if self._manual_marker_y is None else round(py + scale * (self._manual_marker_y - origin[1]))
        return outline, roi, marker

    def _preview_reposition(self, event: tk.Event) -> None:
        if not self._reposition_mode:
            return
        point = self._source_point(event, clamp=True)
        if point is None:
            return
        self._reposition_center = (float(point[0]), float(point[1]))
        self._redraw_reposition()

    def _redraw_reposition(self) -> None:
        now = time.monotonic()
        if now - self._reposition_redrawn >= 0.03:  # the stream redraws too; keep the mouse light
            self._reposition_redrawn = now
            self._redraw_current_frame()

    def _confirm_reposition(self, event: tk.Event) -> None:
        if not self._reposition_mode:
            return
        point = self._source_point(event)
        if point is None:
            return
        self._reposition_center = (float(point[0]), float(point[1]))
        moved = self._repositioned()
        self._reposition_mode = False
        self.video.configure(cursor="")
        if moved is None:
            return
        outline, roi, marker = moved
        scale = self._reposition_scale
        self._reposition_scale = 1.0
        self._reposition_center = None
        self._end_auto_detect()
        self._pot_outline, self._manual_pot_roi = outline, roi
        self._manual_marker_y = marker
        if self._analyzer is not None:
            self._analyzer.roi = roi
            self._analyzer.outline = outline
            self._analyzer.vessel_bounds.clear()
            self._analyzer.marker_y = marker
        self._persist_marker()
        self._capture_pot_reference()
        self._last_container_box = None
        if self._session_log.active:
            self._session_log.save_regions(
                roi, self._temperature_roi, outline=outline, face_quad=self._temperature_quad
            )
            # The same pot in another place or size reads the same fraction; if it reads another,
            # join the series at the last level logged, and keep the session's initial level.
            if self._analyzer is not None and self._session_log.last_volume is not None:
                self._analyzer.anchor_volume = self._session_log.last_volume
                self._analyzer.rejoin()
        self._set_message(f"Pote reposicionado à mão{f' e redimensionado {scale:.2f}×' if abs(scale - 1) >= 0.01 else ''}.")
        self._redraw_current_frame()

    def _preview_roi(self, event: tk.Event) -> None:
        if not self._roi_mode or self._roi_drag_start is None:
            return
        point = self._source_point(event, clamp=True)
        if point is None:
            return
        if self._band_drag is not None:  # a band is being moved: it follows the mouse, no new box
            self._band_override = self._moved_bands(point[0])
            self._redraw_current_frame()
            return
        start_x, start_y = self._roi_drag_start
        end_x, end_y = point
        coords = (*self._canvas_point(min(start_x, end_x), min(start_y, end_y)),
                  *self._canvas_point(max(start_x, end_x), max(start_y, end_y)))
        if self._roi_preview_item is None:
            self._roi_preview_item = self.video.create_rectangle(
                *coords, outline="#ffe16b", width=3, dash=(8, 4)
            )
        else:
            self.video.coords(self._roi_preview_item, *coords)
            self.video.itemconfigure(self._roi_preview_item, state="normal")

    def _finish_roi_draw(self, event: tk.Event) -> None:
        if self._outline_mode:
            self._outline_drag = None
            self._outline_edge_drag = None
            return
        if self._pan_drag_start is not None:
            self._pan_drag_start = None
            self.video.configure(cursor="hand2" if self._zoom > 1 else "")
            return
        if not self._roi_mode or self._roi_drag_start is None:
            return
        point = self._source_point(event, clamp=True)
        start = self._roi_drag_start
        self._roi_drag_start = None
        if point is None:
            self._set_message("Desenhe o retângulo inteiramente sobre a imagem do vídeo.")
            return
        left, right = sorted((start[0], point[0]))
        top, bottom = sorted((start[1], point[1]))
        if self._roi_target == "bands":
            if self._roi_preview_item is not None:
                self.video.itemconfigure(self._roi_preview_item, state="hidden")  # the drag's dashed box is done
            if self._band_drag is not None:
                self._finish_band_move(point[0])
                return
            self._add_band(left, right)
            if len(self._settings.surface_bands) >= MAX_SURFACE_BANDS:
                self._cancel_roi_mode()  # all of them are drawn
            return
        minimum_size = (80, 120) if self._roi_target == "pot" else (40, 20)
        if right - left < minimum_size[0] or bottom - top < minimum_size[1]:
            self._set_message("A área ficou pequena; desenhe um retângulo maior sobre o visor ou o pote.")
            return
        selected_roi = (left, top, right, bottom)
        if self._roi_target == "pot":
            self._end_auto_detect()
            self._manual_pot_roi = selected_roi
            # The same pot in a new search area: the drawn contour (and the mark placed
            # for it) still hold when the contour lies inside the area.
            keep_outline = self._pot_outline is not None and all(
                left <= x <= right and top <= y <= bottom for x, y in self._pot_outline
            )
            if not keep_outline:
                self._pot_outline = None
            if self._analyzer is not None:
                self._analyzer.roi = selected_roi
                if not keep_outline:
                    self._analyzer.outline = None
                    self._analyzer.vessel_bounds.clear()  # a different pot: measure again by day
            if not keep_outline:
                self._manual_marker_y = None
                self._persist_marker()
            self._capture_pot_reference()
            self._last_container_box = None
            self._measurement = None
            if self._session_log.active:
                if not keep_outline:
                    self._session_baseline = None
                    self._reset_tracking()
                self._session_log.save_regions(self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline, face_quad=self._temperature_quad)
            self.auto_pot_button.configure(state="normal")
            message = "Área de busca do pote definida." + (" O contorno foi mantido." if keep_outline else "")
        else:
            self._temperature_roi = selected_roi
            keep_quad = self._temperature_quad is not None and all(
                left <= x <= right and top <= y <= bottom for x, y in self._temperature_quad
            )
            if not keep_quad:
                self._temperature_quad = None
            if self._analyzer is not None:
                self._analyzer.temperature_roi = selected_roi
                if not keep_quad:
                    self._analyzer.temperature_quad = None
            if self._session_log.active:
                self._session_log.save_regions(
                    self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline,
                    face_quad=self._temperature_quad,
                )
            self._temperature_c = None
            self._update_temperature_label()
            message = "Área do visor definida; procurando a temperatura."
        self._cancel_roi_mode()
        self._set_message(message)

    def _toggle_auto_detect(self) -> None:
        """Let the automatic search find the pot while the drawn box and contour are kept aside, untouched;
        unchecking goes back to the drawing. The series is joined at the level it had, not restarted."""
        on = bool(self._auto_detect_var.get())
        if on == self._auto_detect:
            return
        self._auto_detect = on
        if self._analyzer is not None:
            self._analyzer.join_here()
            if on:
                self._auto_bounds_stash = dict(self._analyzer.vessel_bounds)
                self._analyzer.roi = self._analyzer.outline = None
                self._analyzer.vessel_bounds.clear()
            else:
                self._analyzer.roi, self._analyzer.outline = self._manual_pot_roi, self._pot_outline
                self._analyzer.vessel_bounds.clear()
                self._analyzer.vessel_bounds.update(getattr(self, "_auto_bounds_stash", {}))
        self._last_container_box = None
        self._measurement = None
        self._set_message(
            "Detecção automática do pote ligada: o desenho continua guardado; desmarque para voltar a ele."
            if on else "De volta ao pote desenhado."
        )

    def _end_auto_detect(self) -> None:
        """A new drawing (or another session) takes over: the automatic search is over, with nothing to restore."""
        self._auto_detect = False
        self._auto_detect_var.set(False)

    def _source_point(self, event: tk.Event, clamp: bool = False) -> tuple[int, int] | None:
        """The picture's point under the mouse; with ``clamp`` a mouse outside the picture (a drag
        that went past its edge, which Tk still reports) counts as the nearest point on the edge."""
        if self._source_frame_size is None or self._displayed_image_size is None:
            return None
        image_width, image_height = self._display_visible_size or self._displayed_image_size
        offset_x, offset_y = self._display_offsets
        x, y = event.x, event.y
        if clamp:
            x = min(max(x, offset_x), offset_x + image_width - 1)
            y = min(max(y, offset_y), offset_y + image_height - 1)
        if not (offset_x <= x < offset_x + image_width and offset_y <= y < offset_y + image_height):
            return None
        source_width, source_height = self._source_frame_size
        crop_x, crop_y = self._display_crop_origin
        full_width, full_height = self._displayed_image_size
        source_x = min(source_width - 1, int((x - offset_x + crop_x) * source_width / full_width))
        source_y = min(source_height - 1, int((y - offset_y + crop_y) * source_height / full_height))
        return source_x, source_y

    def _preview_marker(self, event: tk.Event) -> None:
        measurement = self._marker_base()
        if not self._marker_mode or measurement is None:
            return
        point = self._source_point(event)
        if point is None:
            self._hide_marker_preview()
            return
        source_x, source_y = point
        if not (
            measurement.left <= source_x <= measurement.right
            and measurement.top <= source_y < measurement.bottom
        ):
            self._hide_marker_preview()
            return

        self._preview_marker_y = source_y
        x1, y = self._canvas_point(measurement.left, source_y)
        x2, _ = self._canvas_point(measurement.right, source_y)
        if self._preview_marker_item is None:
            self._preview_marker_item = self.video.create_line(
                x1, y, x2, y, fill="#ffe16b", width=3, dash=(8, 4)
            )
        else:
            self.video.coords(self._preview_marker_item, x1, y, x2, y)
            self.video.itemconfigure(self._preview_marker_item, state="normal")

    def _hide_marker_preview(self, _event: tk.Event | None = None) -> None:
        if self._preview_marker_item is not None:
            self.video.itemconfigure(self._preview_marker_item, state="hidden")

    def _set_marker_from_click(self, event: tk.Event) -> None:
        if not self._marker_mode:
            return
        measurement = self._marker_base()
        point = self._source_point(event)
        if measurement is None or point is None:
            self._set_message("Aguarde a detecção do pote antes de marcar o nível inicial.")
            return
        source_x, source_y = point
        if not (
            measurement.left <= source_x <= measurement.right
            and measurement.top <= source_y < measurement.bottom
        ):
            self._set_message("Clique na linha pré-visualizada, dentro do recipiente.")
            return
        self._manual_marker_y = source_y
        self._marker_automatic = False
        self._baseline_locked = False
        self._persist_marker()
        self._capture_pot_reference()
        if measurement.empty and measurement.volume_below_profile:  # read at the mark, not as an empty pot
            index = min(max(source_y - measurement.top, 0), len(measurement.volume_below_profile) - 1)
            measurement = replace(measurement, empty=False, surface_y=int(source_y), confidence=0.2,
                                  volume_fraction=measurement.volume_below_profile[index])
        self._measurement = set_initial_marker(
            measurement, source_y, shift=self._analyzer.volume_shift if self._analyzer else 0.0, manual=True
        )
        if self._session_log.active:
            now = time.monotonic()
            baseline = self._measurement.initial_volume_fraction
            self._session_baseline = baseline
            self._reset_tracking(baseline)
            self._phase = self._phase_tracker.update(
                now, self._measurement.volume_fraction, baseline
            )
            self._record_session(now, force=True)
            self._update_double_forecast(now)
        else:
            self._phase_tracker.set_baseline(self._measurement.initial_volume_fraction)
        self._cancel_marker_mode()
        self._update_measurement_labels()
        if self._last_raw_frame is not None:
            shown = self._annotate(self._last_raw_frame, self._measurement)
            self._show_frame(
                self._apply_region_mask(shown),
                source_frame=self._last_raw_frame,
            )

    def _show_frame(self, frame, source_frame=None) -> None:
        panel_width = max(1, self.video.winfo_width())
        panel_height = max(1, self.video.winfo_height())
        self._last_raw_frame = source_frame if source_frame is not None else frame
        self._source_frame_size = (frame.shape[1], frame.shape[0])
        source_height, source_width = frame.shape[:2]
        base_scale = min(panel_width / source_width, panel_height / source_height)
        base_size = (max(1, round(source_width * base_scale)), max(1, round(source_height * base_scale)))
        full_size = (max(1, round(base_size[0] * self._zoom)), max(1, round(base_size[1] * self._zoom)))
        visible = (min(panel_width, full_size[0]), min(panel_height, full_size[1]))
        crop_x = max(0, min(full_size[0] - visible[0], round((full_size[0] - visible[0]) * self._zoom_center[0])))
        crop_y = max(0, min(full_size[1] - visible[1], round((full_size[1] - visible[1]) * self._zoom_center[1])))
        # Resize only what is visible, straight from the source: a 1080p frame
        # is never scaled up and then cropped, which was most of the time spent here.
        scale_x, scale_y = full_size[0] / source_width, full_size[1] / source_height
        x0, y0 = int(crop_x / scale_x), int(crop_y / scale_y)
        x1 = min(source_width, int(np.ceil((crop_x + visible[0]) / scale_x)))
        y1 = min(source_height, int(np.ceil((crop_y + visible[1]) / scale_y)))
        window = frame[y0:y1, x0:x1]
        shrinking = scale_x < 1
        resized = cv2.resize(
            window, visible, interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
        )
        resized = self._draw_recording_state(resized)
        image = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
        self._base_display_size = base_size
        self._displayed_image_size = full_size
        self._display_crop_origin = (crop_x, crop_y)
        self._display_visible_size = visible
        self._display_offsets = ((panel_width - visible[0]) // 2, (panel_height - visible[1]) // 2)
        self._image = ImageTk.PhotoImage(image)
        if self._video_image_item is None:
            self._video_image_item = self.video.create_image(
                self._display_offsets[0] + visible[0] // 2,
                self._display_offsets[1] + visible[1] // 2,
                image=self._image,
                anchor="center",
            )
            self.video.itemconfigure(self._placeholder_item, state="hidden")
        else:
            self.video.coords(
                self._video_image_item,
                self._display_offsets[0] + visible[0] // 2,
                self._display_offsets[1] + visible[1] // 2,
            )
            self.video.itemconfigure(self._video_image_item, image=self._image)

    def _draw_recording_state(self, image):
        """A translucent "Registrando", "Pausado" or "Parado" in the top right corner of what is shown."""
        if not self._session_log.active:
            text, color = "Parado", (200, 200, 200)
        elif self._paused:
            text, color = "Pausado", (60, 60, 255)
        else:
            text, color = "Registrando", (90, 200, 90)
        height, width = image.shape[:2]
        scale = max(0.5, min(1.1, width / 900))
        thickness = max(1, round(scale * 2))
        (text_width, text_height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        x_top, x_bottom = _lowercase_rows(scale, thickness)
        dot = max(5, round((x_bottom - x_top) * 0.6))
        gap = max(8, round(scale * 12))
        x_end = width - gap
        x_text = x_end - text_width
        y_text = gap + text_height
        layer = image.copy()
        cv2.rectangle(
            layer, (x_text - 2 * dot - gap, gap // 2), (x_end + gap // 2, y_text + baseline + gap // 2),
            (0, 0, 0), -1,
        )
        shown = cv2.addWeighted(layer, 0.35, image, 0.65, 0)
        layer = shown.copy()
        cv2.circle(layer, (x_text - dot - gap // 2, y_text - (x_top + x_bottom) // 2), dot, color, -1, cv2.LINE_AA)
        cv2.putText(layer, text, (x_text, y_text), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)
        return cv2.addWeighted(layer, 0.8, shown, 0.2, 0)

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        delta = getattr(event, "delta", 0)
        direction = 1 if delta > 0 or getattr(event, "num", None) == 4 else -1
        if self._reposition_mode:
            self._reposition_scale = min(2.0, max(0.5, self._reposition_scale * (1.02 if direction > 0 else 1 / 1.02)))
            self._redraw_reposition()
            return
        old_zoom = self._zoom
        new_zoom = min(MAX_ZOOM, max(1.0, old_zoom * (1.25 if direction > 0 else 0.8)))
        if new_zoom == old_zoom:
            return
        self._zoom_around_pointer(event, new_zoom)
        self._redraw_current_frame()

    def _zoom_around_pointer(self, event: tk.Event, new_zoom: float) -> None:
        """Zoom so that the point under the mouse stays under the mouse."""
        if new_zoom == 1.0:
            self._zoom_center = (0.5, 0.5)
            self._zoom = new_zoom
            return
        if self._displayed_image_size is None or self._base_display_size is None:
            self._zoom = new_zoom
            return
        full_width, full_height = self._displayed_image_size
        visible_width, visible_height = self._display_visible_size or self._displayed_image_size
        offset_x, offset_y = self._display_offsets
        crop_x, crop_y = self._display_crop_origin
        # Where the pointer is on the picture, as a fraction of it (kept inside the picture).
        fraction_x = min(1.0, max(0.0, (event.x - offset_x + crop_x) / max(1, full_width)))
        fraction_y = min(1.0, max(0.0, (event.y - offset_y + crop_y) / max(1, full_height)))
        panel_width, panel_height = max(1, self.video.winfo_width()), max(1, self.video.winfo_height())
        new_full = (max(1, round(self._base_display_size[0] * new_zoom)), max(1, round(self._base_display_size[1] * new_zoom)))
        new_visible = (min(panel_width, new_full[0]), min(panel_height, new_full[1]))
        new_offset = ((panel_width - new_visible[0]) // 2, (panel_height - new_visible[1]) // 2)
        pointer_x = min(max(event.x, new_offset[0]), new_offset[0] + new_visible[0])
        pointer_y = min(max(event.y, new_offset[1]), new_offset[1] + new_visible[1])
        wanted_x = fraction_x * new_full[0] - (pointer_x - new_offset[0])
        wanted_y = fraction_y * new_full[1] - (pointer_y - new_offset[1])
        overflow_x, overflow_y = new_full[0] - new_visible[0], new_full[1] - new_visible[1]
        self._zoom = new_zoom
        self._zoom_center = (
            min(1.0, max(0.0, wanted_x / overflow_x)) if overflow_x > 0 else 0.5,
            min(1.0, max(0.0, wanted_y / overflow_y)) if overflow_y > 0 else 0.5,
        )

    def _drag_video(self, event: tk.Event) -> None:
        """Mouse moved with the left button down.

        Tk runs only the most specific binding, so <B1-Motion> hides <Motion> while
        a button is held. Both the region preview and the pan have to be driven
        from here, or the preview rectangle never shows while the region is dragged.
        """
        if self._outline_mode:
            self._move_outline_handle(event)
            return
        self._preview_roi(event)
        self._pan_video(event)

    def _pan_video(self, event: tk.Event) -> None:
        if self._pan_drag_start is None or self._displayed_image_size is None:
            return
        visible_width, visible_height = self._display_visible_size or (0, 0)
        overflow_x = max(0, self._displayed_image_size[0] - visible_width)
        overflow_y = max(0, self._displayed_image_size[1] - visible_height)
        start_x, start_y = self._pan_drag_start
        start_center_x, start_center_y = self._pan_start_center
        center_x = start_center_x - (event.x - start_x) / overflow_x if overflow_x else 0.5
        center_y = start_center_y - (event.y - start_y) / overflow_y if overflow_y else 0.5
        self._zoom_center = (min(1.0, max(0.0, center_x)), min(1.0, max(0.0, center_y)))
        self._redraw_current_frame()

    def _redraw_current_frame(self) -> None:
        if self._last_raw_frame is None:
            return
        overlay = (
            self._annotate(self._last_raw_frame, self._measurement)
            if self._measurement else self._last_raw_frame
        )
        self._show_frame(self._apply_region_mask(overlay), source_frame=self._last_raw_frame)

    def _canvas_point(self, x: int, y: int) -> tuple[float, float]:
        if self._displayed_image_size is None or self._source_frame_size is None:
            return 0.0, 0.0
        sw, sh = self._source_frame_size
        dw, dh = self._displayed_image_size
        ox, oy = self._display_offsets
        cx, cy = self._display_crop_origin
        return ox + x * dw / sw - cx, oy + y * dh / sh - cy

    def _apply_region_mask(self, frame):
        regions = [region for region in (self._manual_pot_roi, self._temperature_roi) if region]
        if not regions and not self._outline_mode and not self._reposition_mode:
            return frame
        height, width = frame.shape[:2]
        drawn = {"pot": self._pot_outline, "thermometer": self._temperature_quad}
        # The marks keep the size they have with no zoom: they are drawn on the picture, which is scaled up.
        k = 1.0 / self._zoom

        def thick(pixels: float) -> int:
            return max(1, round(pixels * k))

        if self._roi_mode or self._outline_mode or self._reposition_mode:
            # Drawing a new region: the thing to find may have moved out of the old
            # ones, so keep the whole picture visible and only outline them.
            result = frame.copy()
        else:
            # Dim everything (by the brightness set in Configurações), then put the
            # outer boxes (drawn, or the box around a drawn contour) back as they were.
            result = cv2.convertScaleAbs(frame, alpha=self._settings.overlay_brightness / 100)
            for left, top, right, bottom in regions:
                top, bottom = max(0, top), min(height, bottom)
                left, right = max(0, left), min(width, right)
                result[top:bottom, left:right] = frame[top:bottom, left:right]
        for region, color, label, shape in (
            (self._manual_pot_roi, (50, 220, 130), "POTE", drawn["pot"]),
            (self._temperature_roi, (40, 190, 245), "TERMÔMETRO", drawn["thermometer"]),
        ):
            if not region:
                continue
            left, top, right, bottom = region
            if shape:
                # The contour is the outline; the search area around it is only a faint hint.
                overlay = result.copy()
                cv2.rectangle(overlay, (left, top), (right, bottom), (190, 200, 195), thick(1), cv2.LINE_AA)
                cv2.addWeighted(overlay, 0.35, result, 0.65, 0, result)
                label_x, label_y = min(x for x, _ in shape), min(y for _, y in shape)
            else:
                cv2.rectangle(result, (left, top), (right, bottom), color, thick(2))
                label_x, label_y = left, top
            if shape and label == "POTE":
                continue  # the measurement label ("Pote | volume ...") already names it
            cv2.putText(result, label, (label_x, max(round(22 * k), label_y - round(8 * k))),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65 * k, color, thick(2), cv2.LINE_AA)
        for target, saved, base_color in (
            ("pot", self._pot_outline, (50, 220, 130)),
            ("thermometer", self._temperature_quad, (40, 190, 245)),
        ):
            editing = self._outline_mode and self._outline_target == target
            outline = self._outline_points if editing else saved
            if not outline:
                continue
            color = (0, 235, 255) if editing else base_color
            cv2.polylines(result, [np.asarray(outline, np.int32)], True, color, thick(2), cv2.LINE_AA)
            if editing:
                radius = max(3, round(12 * k))
                for x, y in outline:
                    cv2.circle(result, (int(x), int(y)), radius, color, -1, cv2.LINE_AA)
                    cv2.circle(result, (int(x), int(y)), radius, (0, 0, 0), thick(2), cv2.LINE_AA)
        moved = self._repositioned() if self._reposition_mode else None
        if moved is not None:
            outline, _roi, marker_y = moved
            cv2.polylines(result, [np.asarray(outline, np.int32)], True, (0, 235, 255), thick(3), cv2.LINE_AA)
            center = np.mean(np.asarray(outline, np.float32), axis=0)
            cx, cy = int(center[0]), int(center[1])
            arm = max(3, round(14 * k))
            cv2.line(result, (cx - arm, cy), (cx + arm, cy), (0, 235, 255), thick(2), cv2.LINE_AA)
            cv2.line(result, (cx, cy - arm), (cx, cy + arm), (0, 235, 255), thick(2), cv2.LINE_AA)
            if marker_y is not None:
                xs = [x for x, _ in outline]
                cv2.line(result, (min(xs), marker_y), (max(xs), marker_y), (0, 235, 255), thick(2), cv2.LINE_AA)
        return result

    def _center_video_items(self, event: tk.Event) -> None:
        center = event.width // 2, event.height // 2
        self.video.coords(self._placeholder_item, *center)
        if self._video_image_item is not None:
            visible = self._display_visible_size or (0, 0)
            offset = self._display_offsets
            self.video.coords(
                self._video_image_item,
                offset[0] + visible[0] // 2,
                offset[1] + visible[1] // 2,
            )

    def close(self) -> None:
        if self._phone_pusher is not None:
            self._phone_pusher.stop(wait=False)
        if self._debug_window is not None and self._debug_window.open:
            self._debug_window.close()
        if self._session_log.active:
            self._session_log.stop()
        if self._analyzer is not None:
            self._analyzer.stop()
        if self._stream is not None:
            self._stream.stop()
        self.root.destroy()

    def _toggle_session(self, confirm: bool = True) -> None:
        if self._session_log.active:
            if confirm and not messagebox.askyesno(
                "Encerrar sessão",
                f"Encerrar a sessão ‘{self._session_name or 'sem nome'}’?\n"
                "Os registros ficam salvos e ela pode ser retomada depois.",
                parent=self.root,
            ):
                return
            path = self._session_log.stop()
            self._set_paused(False)
            self.pause_button.configure(state="disabled")
            self.session_button.configure(text="Iniciar sessão")
            self.metric_values["Sessão"].configure(text="Sessão encerrada")
            self._phase = "Aguardando sessão"
            self.metric_values["Estado do levain"].configure(text=self._phase)
            self._set_message(f"Registro salvo em {path}.")
            self._forget_session_data()
            return
        if self._measurement is None:
            empty = self._analyzer is not None and self._analyzer.empty_pot is not None
            self._set_message(
                "O pote está vazio; coloque o levain para iniciar a sessão." if empty
                else "Aguarde a detecção do pote para iniciar uma sessão."
            )
            return
        try:
            if self._mix_time is None:  # always asked: it never carries over from a session before
                self._mix_time = self._ask_mix_time(datetime.now().astimezone())
                if self._mix_time is None:
                    return
            suggested_name = f"Levain {self._mix_time.strftime('%d/%m %H:%M')}"
            name = simpledialog.askstring(
                "Nome da sessão", "Dê um nome para esta sessão:",
                initialvalue=self._session_name or suggested_name, parent=self.root,
            )
            if name is None:
                return
            self._session_name = name.strip() or suggested_name
            asked = simpledialog.askstring(
                "Proporção", "Proporção isca : água : farinha:",
                initialvalue="{:g}:{:g}:{:g}".format(*(parse_ratio(self._session_name) or (1, 1, 1))),
                parent=self.root,
            )
            ratio = parse_ratio(asked or "") or parse_ratio(self._session_name) or (1.0, 1.0, 1.0)
            session_id = self._session_log.start(self._mix_time, self._session_name)
            self._session_log.save_ratio(ratio)
            self._session_log.save_snapshot_seconds(self._settings.snapshot_minutes * 60.0)
            self._session_log.save_phone(
                self._settings.phone_enabled, self._settings.phone_interval_seconds, self._settings.phone_snapshot
            )
            self._session_log.save_rotation(self._settings.rotation)
            self._session_log.volume_offset = 0.0
            if self._analyzer is not None:
                self._analyzer.reset_join()
                self._analyzer.reference_base = self._session_log.path.with_suffix("")
                self._capture_pot_reference()
            self._session_log.save_regions(self._manual_pot_roi, self._temperature_roi, outline=self._pot_outline, face_quad=self._temperature_quad)
            self._persist_marker()
        except OSError as exc:
            logging.exception("Não foi possível iniciar o registro da sessão")
            self._set_message(f"Não foi possível criar o CSV da sessão: {exc}")
            return
        baseline = (
            self._measurement.initial_volume_fraction
            if self._measurement.initial_volume_fraction is not None
            else self._measurement.volume_fraction
        )
        self._session_baseline = baseline
        self._measurement = set_initial_volume_reference(self._measurement, baseline)
        self._reset_tracking(baseline)
        self._phase = PhaseTracker.WAITING
        self.session_button.configure(text="Encerrar sessão")
        self.pause_button.configure(state="normal")
        self.mix_time_button.configure(
            text=f"Mistura: {self._mix_time.strftime('%d/%m %H:%M')}"
        )
        self._update_ratio_label()
        self._follow_graph_to(self._session_log.path)
        self.metric_values["Sessão"].configure(
            text=self._session_summary()
        )
        self.metric_values["Estado do levain"].configure(text=self._phase)
        self._update_double_forecast(time.monotonic())
        self._record_session(time.monotonic(), force=True)
        self._set_message(f"Sessão ‘{self._session_name}’ ativa. Dados registrados em {self._session_log.path}.")

    def _forget_session_data(self) -> None:
        """After a session ends, what was typed or measured for it does not carry to the next.

        Kept: the pot, the thermometer and its digits (the camera's view). Gone: the mix time, the name,
        the ratio, the mark, the level it started from, the forecast and the last temperature."""
        self._mix_time = None
        self._session_name = ""
        self._session_baseline = None
        self._manual_marker_y = None
        self._marker_automatic = True
        self._baseline_locked = False
        self._last_known_temperature = None
        self._forecast_saved_at = 0.0
        self._forecast_prediction = None
        self._reset_tracking()
        if self._analyzer is not None:
            self._analyzer.forget_session()
        self.mix_time_button.configure(text="Hora da mistura")
        self._update_ratio_label()
        self._update_temperature_label()
        self._update_double_forecast(time.monotonic())

    def _update_ratio_label(self) -> None:
        ratio = self._session_log.ratio if self._session_log.active else None
        self.ratio_button.configure(
            text="Proporção" if ratio is None else "Proporção: " + ":".join(f"{part:g}" for part in ratio)
        )

    def _set_ratio(self) -> None:
        if not self._session_log.active:
            self._set_message("A proporção é pedida ao iniciar a sessão; depois dá para mudar aqui.")
            return
        current = self._session_log.ratio or (1.0, 1.0, 1.0)
        entered = simpledialog.askstring(
            "Proporção", "Proporção isca : água : farinha:",
            initialvalue="{:g}:{:g}:{:g}".format(*current), parent=self.root,
        )
        if entered is None:
            return
        ratio = parse_ratio(entered)
        if ratio is None:
            self._set_message("Proporção inválida. Use o formato 1:2:3.")
            return
        try:
            self._session_log.save_ratio(ratio)
        except OSError:
            logging.exception("Falha ao salvar a proporção na sessão")
            self._set_message("Não foi possível salvar a proporção.")
            return
        self._update_ratio_label()
        self._update_double_forecast(time.monotonic())
        self._set_message("Proporção alterada para " + ":".join(f"{part:g}" for part in ratio) + ".")

    def _open_session_csv(self) -> None:
        path = self._session_log.path
        if path is None or not path.exists():
            selected = filedialog.askopenfilename(
                title="Abrir CSV de sessão", initialdir="data/sessions",
                filetypes=(("CSV", "*.csv"), ("Todos os arquivos", "*")), parent=self.root,
            )
            if not selected:
                return
            path = Path(selected)
        webbrowser.open(path.resolve().as_uri())

    def _show_session_graph(self, choose: bool = False) -> None:
        """The chart of the current session, or (``choose``) of any saved one."""
        path = self._session_log.path
        if choose or path is None or not path.exists():
            selected = filedialog.askopenfilename(
                title="Escolher sessão para o gráfico",
                initialdir="data/sessions",
                filetypes=(("CSV", "*.csv"),),
                parent=self.root,
            )
            if not selected:
                return
            path = Path(selected)

        existing = getattr(self, "_graph_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            self._graph_path = path
            existing.title(f"Evolução da sessão · {path.stem}")
            self._refresh_session_graph()
            return

        self._graph_path = path
        self._graph_window = tk.Toplevel(self.root, class_="Pythontkfloating")
        self._graph_window.title(f"Evolução da sessão · {path.stem}")
        self._graph_window.geometry("640x400")
        self._graph_window.minsize(640, 400)
        self._graph_window.configure(background="#c9c3b4")
        self._graph_canvas = tk.Canvas(
            self._graph_window, background="#fffefa", highlightthickness=0, cursor="fleur"
        )
        self._graph_canvas.pack(fill="both", expand=True, padx=GRAPH_BORDER, pady=GRAPH_BORDER)
        self._graph_controls = FloatingWindowControls(
            self._graph_window, self._graph_canvas, GRAPH_BORDER,
            on_close=self._close_session_graph,
        )
        self._graph_canvas.bind("<Configure>", self._on_graph_configure, add="+")
        self._graph_canvas.bind("<Motion>", self._graph_hover_at, add="+")
        self._graph_canvas.bind("<Leave>", lambda _e: self._graph_canvas.delete("hover"), add="+")
        self._refresh_session_graph()
        self._graph_window.after_idle(self._refresh_session_graph)

        def update_graph() -> None:
            if not self._graph_window.winfo_exists():
                return
            self._refresh_session_graph()
            self._graph_window.after(10_000, update_graph)

        self._graph_window.after(10_000, update_graph)

    def _follow_graph_to(self, path) -> None:
        """An open chart that was showing the previous current session moves to the new one."""
        previous, self._graph_followed = self._graph_followed, path
        window = getattr(self, "_graph_window", None)
        if window is None or not window.winfo_exists() or path is None:
            return
        if previous is not None and getattr(self, "_graph_path", None) != previous:
            return  # the chart shows a saved session the user picked: leave it
        self._graph_path = path
        window.title(f"Evolução da sessão · {path.stem}")
        self._refresh_session_graph()

    def _cycle_graph_scale(self, series: str) -> str:
        """Next scale mode for the volume or the temperature, saved and redrawn at once."""
        if series == "volume":
            modes, current = VOLUME_SCALES, self._settings.graph_volume_scale
            field = "graph_volume_scale"
        else:
            modes, current = tuple(TEMPERATURE_SCALES), self._settings.graph_temperature_scale
            field = "graph_temperature_scale"
        chosen = modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0]
        self._settings = replace(self._settings, **{field: chosen}).clamped()
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar a escala do gráfico")
        self._refresh_session_graph()
        return "break"

    def _toggle_graph_option(self, field: str) -> str:
        """Turn the stretch before the monitoring, or the forecast curve, on or off; saved and redrawn."""
        self._settings = replace(self._settings, **{field: not getattr(self._settings, field)}).clamped()
        try:
            save_settings(self._settings)
        except OSError:
            logging.exception("Falha ao salvar a opção do gráfico")
        self._refresh_session_graph()
        return "break"

    def _graph_forecast_time(self, path: Path) -> datetime | None:
        """When the doubling was forecast for the session of this graph: the live forecast of the session being
        watched, else the last one kept in its JSON (so the curve stays after the doubling, to compare)."""
        if path == self._session_log.path and self._forecast_prediction is not None:
            return self._forecast_prediction
        try:
            known = json.loads(path.with_suffix(".json").read_text(encoding="utf-8")).get("last_forecast")
            return datetime.fromisoformat(known["at"]) if known else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _refresh_session_graph(self) -> None:
        if getattr(self, "_graph_canvas", None) is None:
            return
        self._draw_session_graph()

    def _close_session_graph(self, _event: tk.Event | None = None) -> str:
        graph_window = getattr(self, "_graph_window", None)
        if graph_window is not None and graph_window.winfo_exists():
            graph_window.destroy()
        return "break"

    def _connect_camera(self, config: CameraConfig) -> None:
        """Start reading the camera; on a later call the old stream is replaced, keeping the drawings."""
        stream = RtspStream(config)
        stream.rotation = self._settings.rotation
        old = self._stream
        self._stream = stream
        stream.start()
        if self._analyzer is not None:
            self._analyzer.stream = stream
        else:
            self._analyzer = ContainerAnalyzer(stream)
            self._analyzer.ink_cut_infrared = self._settings.ink_cut_infrared
            self._analyzer.ink_cut_day = self._settings.ink_cut_day
            self._analyzer.use_mark = self._settings.mark_alignment
            self._apply_method()
            self._analyzer.lamp_hold_seconds = self._settings.lamp_minutes * 60.0
            self._analyzer.start()
        if old is not None:
            old.stop()
        self._set_message("Iniciando conexão com a câmera…")

    def _save_camera(self) -> None:
        config = CameraConfig(
            url=self._camera_url_var.get().strip(),
            username=self._camera_user_var.get().strip(),
            password=self._camera_password_var.get(),
            transport=self._camera_transport_var.get(),
            read_timeout_seconds=self._camera_timeout,
        )
        try:
            save_camera_config(config)
        except (ValueError, OSError) as exc:
            self._camera_note.configure(text=str(exc), foreground="#a43d32")
            return
        self._connect_camera(config)
        self.status_value.configure(foreground="")
        self._camera_note.configure(text="Salvo em camera.ini; reconectando.", foreground="#59645e")

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        try:
            camera_now = load_camera_config()
        except (FileNotFoundError, ValueError):
            camera_now = CameraConfig(url="")
        try:
            phone_now = load_phone_config()
        except (FileNotFoundError, ValueError):
            phone_now = PhoneConfig(url="", token="")
        self._camera_timeout = camera_now.read_timeout_seconds
        for column in (0, 1):
            parent.columnconfigure(column, weight=1, uniform="settings")
        muted = "#59645e"

        columns = []
        for column in (0, 1):  # each column stacks on its own, so a tall box on one side leaves no gap on the other
            holder = ttk.Frame(parent)
            holder.grid(row=0, column=column, sticky="new", padx=(0, 6) if column == 0 else (6, 0))
            columns.append(holder)

        def frame(title: str, row: int, column: int) -> ttk.LabelFrame:
            box = ttk.LabelFrame(columns[column], text=title, padding=(8, 4))
            box.pack(fill="x", pady=(0, 6))
            box.columnconfigure(1, weight=1)
            return box

        def entry(box, row: int, label: str, variable, hint: str = "", width: int = 12) -> ttk.Entry:
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=1)
            field = ttk.Entry(box, textvariable=variable, width=width)
            field.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=1)
            if hint:
                _Tooltip(field, hint)
            return field

        def spin(box, row: int, label: str, variable, limits, unit: str, hint: str = "", step: int = 1) -> None:
            name = ttk.Label(box, text=label)
            name.grid(row=row, column=0, sticky="w", pady=1)
            field = ttk.Spinbox(box, from_=limits[0], to=limits[1], increment=step, width=5,
                                textvariable=variable, command=self._apply_settings)
            field.grid(row=row, column=1, sticky="w", padx=6, pady=1)
            field.bind("<Return>", lambda _e: self._apply_settings(), add="+")
            field.bind("<FocusOut>", lambda _e: self._apply_settings(), add="+")
            ttk.Label(box, text=unit).grid(row=row, column=2, sticky="w")
            if hint:
                _Tooltip(name, hint)

        # --- left column: camera, record, video
        camera = frame("Câmera", 0, 0)
        self._camera_url_var = tk.StringVar(value=camera_now.url)
        self._camera_user_var = tk.StringVar(value=camera_now.username)
        self._camera_password_var = tk.StringVar(value=camera_now.password)
        self._camera_transport_var = tk.StringVar(
            value=camera_now.transport if camera_now.transport in ("tcp", "udp") else "tcp")
        entry(camera, 0, "URL", self._camera_url_var, "rtsp://…  (se a câmera só abre com udp, troque o transporte)")
        entry(camera, 1, "Usuário", self._camera_user_var)
        entry(camera, 2, "Senha", self._camera_password_var)
        ttk.Label(camera, text="Transporte").grid(row=3, column=0, sticky="w", pady=1)
        row = ttk.Frame(camera)
        row.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=1)
        ttk.Combobox(row, textvariable=self._camera_transport_var, values=("tcp", "udp"), width=5,
                     state="readonly").pack(side="left")
        ttk.Button(row, text="Salvar e reconectar", command=self._save_camera).pack(side="right")
        self._camera_note = ttk.Label(camera, text="", foreground=muted, wraplength=300, justify="left")
        self._camera_note.grid(row=4, column=0, columnspan=3, sticky="w")

        record = frame("Registro da sessão", 1, 0)
        self._interval_var = tk.StringVar(value=str(self._settings.record_interval_seconds))
        self._snapshot_var = tk.StringVar(value=str(self._settings.snapshot_minutes))
        spin(record, 0, "Medição no CSV a cada", self._interval_var, (MIN_RECORD_SECONDS, MAX_RECORD_SECONDS),
             "s", f"Entre {MIN_RECORD_SECONDS} e {MAX_RECORD_SECONDS} s. Vale já na sessão ativa.", step=5)
        spin(record, 1, "Foto da câmera a cada", self._snapshot_var, SNAPSHOT_MINUTES, "min")
        self._settings_note = ttk.Label(record, text="")  # kept: other code may write to it

        video = frame("Vídeo", 2, 0)
        ttk.Label(video, text="Brilho fora das regiões").grid(row=0, column=0, sticky="w")
        self._overlay_var = tk.IntVar(value=self._settings.overlay_brightness)
        self._overlay_value = ttk.Label(video, text=f"{self._settings.overlay_brightness}%", width=5)
        slider = ttk.Scale(
            video, from_=MIN_OVERLAY_BRIGHTNESS, to=MAX_OVERLAY_BRIGHTNESS, orient="horizontal",
            variable=self._overlay_var,
            command=lambda value: self._overlay_value.configure(text=f"{int(float(value))}%"),
        )
        slider.grid(row=0, column=1, sticky="ew", padx=6)
        slider.bind("<ButtonRelease-1>", lambda _e: self._apply_settings(), add="+")
        slider.bind("<KeyRelease>", lambda _e: self._apply_settings(), add="+")
        self._overlay_value.grid(row=0, column=2, sticky="w")
        _Tooltip(slider, f"{MAX_OVERLAY_BRIGHTNESS}% não escurece nada; só age depois de marcar o pote ou o visor.")

        # --- right column: phases, phone
        phases = frame("Fases do levain", 0, 1)
        self._peak_var = tk.StringVar(value=str(self._settings.peak_minutes))
        self._artifact_var = tk.StringVar(value=str(self._settings.artifact_minutes))
        self._lamp_var = tk.StringVar(value=str(self._settings.lamp_minutes))
        spin(phases, 0, "Pico: estável por", self._peak_var, PEAK_MINUTES, "min",
             "Quanto tempo o volume fica parado no topo até o app dizer \"Em pico\".")
        spin(phases, 1, "Artefato: volta em", self._artifact_var, ARTIFACT_MINUTES, "min",
             "Uma mudança que volta ao nível anterior dentro desse tempo (lâmpada, pote retirado) é descartada; "
             "uma queda que não volta passa a valer depois dele.")
        spin(phases, 2, "Lâmpada: segurar por", self._lamp_var, LAMP_MINUTES, "min",
             "Quando o brilho muda de repente (a lâmpada liga ou desliga), o volume fica no nível de antes por "
             "esse tempo. 0 desliga.")

        phone = frame("Celular (Google Sheets)", 1, 1)
        self._phone_enabled_var = tk.BooleanVar(value=self._settings.phone_enabled)
        self._phone_snapshot_var = tk.BooleanVar(value=self._settings.phone_snapshot)
        self._phone_interval_var = tk.StringVar(value=str(self._settings.phone_interval_seconds))
        self._phone_url_var = tk.StringVar(value=phone_now.url)
        self._phone_token_var = tk.StringVar(value=phone_now.token)
        self._phone_panel_var = tk.StringVar(value=phone_now.panel_url)
        checks = ttk.Frame(phone)
        checks.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(checks, text="Enviar para o celular", variable=self._phone_enabled_var,
                        command=self._apply_settings).pack(side="left")
        ttk.Checkbutton(checks, text="com imagem", variable=self._phone_snapshot_var,
                        command=self._apply_settings).pack(side="left", padx=(10, 0))
        spin(phone, 1, "Pacote a cada", self._phone_interval_var, (MIN_PHONE_SECONDS, MAX_PHONE_SECONDS), "s", step=30)
        entry(phone, 2, "URL (/exec)", self._phone_url_var, "URL do app da web do Google Apps Script (https://…/exec)")
        entry(phone, 3, "Token", self._phone_token_var, "Mesmo valor da propriedade TOKEN do script (16+ caracteres)")
        entry(phone, 4, "Painel (opcional)", self._phone_panel_var, "Onde o painel está publicado, por exemplo no GitHub Pages")
        row = ttk.Frame(phone)
        row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        ttk.Button(row, text="Salvar celular", command=self._save_phone).pack(side="left")
        ttk.Button(row, text="Link local", width=9, command=self._copy_local_link).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Link online", width=10, command=self._copy_online_link).pack(side="left", padx=(6, 0))
        self._phone_status = ttk.Label(phone, text="", foreground=muted, wraplength=300, justify="left")
        self._phone_status.grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Button(phone, text="Limpar planilha", command=self._clear_phone_data).grid(row=6, column=2, sticky="e")

    def _save_phone(self) -> None:
        config = PhoneConfig(
            url=self._phone_url_var.get(), token=self._phone_token_var.get(), panel_url=self._phone_panel_var.get(),
        )
        try:
            save_phone_config(config)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Celular", str(exc))
            return
        self._phone_applied = None  # the sender starts again with the new address and token
        self._apply_phone_settings()

    def _apply_settings(self) -> None:
        try:
            seconds = int(float(self._interval_var.get()))
        except ValueError:
            seconds = self._settings.record_interval_seconds
        try:
            phone_seconds = int(float(self._phone_interval_var.get()))
        except ValueError:
            phone_seconds = self._settings.phone_interval_seconds
        try:
            peak_minutes = int(float(self._peak_var.get()))
        except ValueError:
            peak_minutes = self._settings.peak_minutes
        try:
            artifact_minutes = int(float(self._artifact_var.get()))
        except ValueError:
            artifact_minutes = self._settings.artifact_minutes
        try:
            lamp_minutes = int(float(self._lamp_var.get()))
        except ValueError:
            lamp_minutes = self._settings.lamp_minutes
        try:
            snapshot_minutes = int(float(self._snapshot_var.get()))
        except ValueError:
            snapshot_minutes = self._settings.snapshot_minutes
        # replace(), not a new Settings: the options this tab does not show (graph scales,
        # the display's black/white cuts) must survive a change here.
        self._settings = replace(
            self._settings,
            record_interval_seconds=seconds,
            phone_enabled=self._phone_enabled_var.get(),
            phone_interval_seconds=phone_seconds,
            phone_snapshot=self._phone_snapshot_var.get(),
            overlay_brightness=int(float(self._overlay_var.get())),
            peak_minutes=peak_minutes,
            artifact_minutes=artifact_minutes,
            lamp_minutes=lamp_minutes,
            snapshot_minutes=snapshot_minutes,
        ).clamped()
        self._overlay_var.set(self._settings.overlay_brightness)
        self._interval_var.set(str(self._settings.record_interval_seconds))
        self._phone_interval_var.set(str(self._settings.phone_interval_seconds))
        self._peak_var.set(str(self._settings.peak_minutes))
        self._artifact_var.set(str(self._settings.artifact_minutes))
        self._lamp_var.set(str(self._settings.lamp_minutes))
        self._snapshot_var.set(str(self._settings.snapshot_minutes))
        snapshot_seconds = self._settings.snapshot_minutes * 60.0
        if snapshot_seconds != self._session_log.snapshot_seconds:
            try:
                self._session_log.save_snapshot_seconds(snapshot_seconds)
            except OSError:
                logging.exception("Falha ao salvar o intervalo das fotos na sessão")
        if self._session_log.active:
            phone = (self._settings.phone_enabled, self._settings.phone_interval_seconds, self._settings.phone_snapshot)
            known = self._session_log.phone
            if known is None or phone != (known["enabled"], known["interval_seconds"], known["snapshot"]):
                try:
                    self._session_log.save_phone(*phone)
                except OSError:
                    logging.exception("Falha ao salvar o envio para o celular na sessão")
        if self._analyzer is not None:
            self._analyzer.lamp_hold_seconds = self._settings.lamp_minutes * 60.0
        self._phase_tracker.plateau_seconds = self._settings.peak_minutes * 60
        self._phase_tracker.artifact_seconds = self._settings.artifact_minutes * 60
        self._session_log.sample_seconds = self._settings.record_interval_seconds
        try:
            save_settings(self._settings)
        except OSError as exc:
            self._set_message(f"Não foi possível salvar as configurações: {exc}")
        self._apply_phone_settings()

    # ------------------------------------------------------------ celular

    def _apply_phone_settings(self) -> None:
        """Start, restart or stop the sender when its settings change."""
        settings = self._settings
        wanted = (settings.phone_enabled, settings.phone_interval_seconds, settings.phone_snapshot)
        if wanted == self._phone_applied:
            return
        self._phone_applied = wanted
        if self._phone_pusher is not None:
            self._phone_pusher.stop(wait=False)
            self._phone_pusher = None
        self._phone_status_text = "Desligado."
        if settings.phone_enabled:
            try:
                config = load_phone_config()
            except (FileNotFoundError, ValueError) as exc:
                self._phone_status_text = f"Não foi possível ligar: {exc}"
            else:
                self._phone_pusher = PhonePusher(
                    config, self._phone_live, self._phone_csv,
                    self._phone_snapshot if settings.phone_snapshot else None,
                    interval_seconds=settings.phone_interval_seconds,
                    get_chart_options=self._phone_chart_options,
                )
                self._phone_pusher.start()
        self._render_phone_status()

    def _clear_phone_data(self) -> None:
        pusher = self._phone_pusher
        if pusher is None:
            messagebox.showinfo("Celular", "Ligue o envio para o celular antes de limpar a planilha.")
            return
        if not messagebox.askyesno(
            "Limpar dados do celular",
            "Apagar todos os dados da planilha do Google (série, gráfico, imagem e estado ao vivo)?\n\n"
            "A sessão atual é enviada de novo em seguida. Os arquivos de sessão deste computador não são tocados.",
        ):
            return
        self._phone_status.configure(text="Limpando a planilha…")

        def work() -> None:
            done, message = pusher.clear_remote()
            text = f"Planilha limpa ({message})." if done else f"Não foi possível limpar: {message}"
            self.root.after(0, lambda: self._phone_status.configure(text=text))

        threading.Thread(target=work, name="phone-clear", daemon=True).start()

    def _render_phone_status(self) -> None:
        pusher = self._phone_pusher
        if pusher is None:
            text = self._phone_status_text
        else:
            status = pusher.status
            parts = ["Ligado"]
            if status.last_ok is None:
                parts.append("aguardando o primeiro envio")
            else:
                parts.append(f"último envio há {self._elapsed_text(time.time() - status.last_ok)}")
            if status.last_error:
                parts.append(f"falha: {status.last_error}")
            text = " · ".join(parts)
        if hasattr(self, "_phone_status"):
            self._phone_status.configure(text=text)

    def _tick_phone_status(self) -> None:
        self._render_phone_status()
        self.root.after(5000, self._tick_phone_status)

    @staticmethod
    def _elapsed_text(seconds: float) -> str:
        if seconds < 60:
            return "alguns segundos"
        if seconds < 90 * 60:
            return f"{round(seconds / 60)} min"
        return f"{seconds / 3600:.1f} h".replace(".", ",")

    def _publish_live_state(self) -> None:
        """What the phone panel shows as 'now'; the sender thread only reads it."""
        if self._phone_pusher is None:
            return
        measurement = self._measurement
        age = None if self._analyzer is None else self._analyzer.temperature_age_seconds()
        note = ""
        if self._temperature_c is not None and (self._temperature_filled or (age is not None and age > 60)):
            note = "última leitura" + (f" há {self._elapsed_text(age)}" if age is not None and age >= 60 else "")
        self._live_state = {
            "sessao": self._session_name or "",
            "volume_pct": None if measurement is None else round(measurement.volume_fraction * 100, 1),
            "crescimento": None if measurement is None or measurement.growth_factor is None
            else round(measurement.growth_factor, 2),
            "fase": self._phase,
            "temperatura_c": self._temperature_c,
            "temperatura_nota": note,
            "taxa": self._growth_rate_text(),
            "previsao": self.metric_values["Previsão do dobro"].cget("text"),
            "perfil": None if self._analyzer is None else self._analyzer.profile.name,
            "status": None if self._stream is None else self._stream.status,
            "intervalo_s": self._settings.phone_interval_seconds,
        }

    def _growth_rate_text(self) -> str | None:
        """The growth per hour and the arrow of its trend, worked out at most every 30 s."""
        now = time.time()
        if self._rate_text_at is None or now - self._rate_text_at >= 30.0 or not self._session_log.active:
            rate = growth_rate(self._session_log.recent, now) if self._session_log.active else None
            self._rate_text, self._rate_text_at = (None if rate is None else rate.text()), now
        return self._rate_text

    def _phone_live(self) -> dict:
        return dict(self._live_state)

    def _phone_csv(self) -> Path | None:
        return self._session_log.path if self._session_log.active else None

    def _phone_chart_options(self) -> dict:
        """The scales chosen on the graph, so the phone shows the same axes."""
        return {
            "temperature_scale": self._settings.graph_temperature_scale,
            "volume_mode": self._settings.graph_volume_scale,
            "peak_seconds": self._settings.peak_minutes * 60,
            "artifact_seconds": self._settings.artifact_minutes * 60,
        }

    def _phone_snapshot(self) -> dict | None:
        stream, measurement = self._stream, self._measurement
        if stream is None or measurement is None or self._paused:  # paused: no new frames for the sheet's ring
            return None
        return make_snapshot(stream.snapshot()[1], measurement, crop=self._manual_pot_roi)

    def _copy_local_link(self) -> None:
        self._copy_link(
            local_link,
            "Link do painel local copiado. Ele só abre com o servidor rodando "
            "(cd painel && python -m http.server 8000). Carrega o token: não compartilhe.",
        )

    def _copy_online_link(self) -> None:
        self._copy_link(
            online_link,
            "Link do painel online copiado. Carrega o token: não compartilhe.",
            missing="Defina panel_url no phone.ini (por exemplo https://lexrupy.github.io/levainpeak/).",
        )

    def _copy_link(self, make_link, done: str, missing: str = "") -> None:
        try:
            link = make_link(load_phone_config())
        except (FileNotFoundError, ValueError) as exc:
            self._set_message(str(exc))
            return
        if link is None:
            self._set_message(missing)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(link)
        self._set_message(done)

    def _graph_data(self, path: Path) -> dict:
        """The graph's series, rebuilt only when the CSV file changes."""
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, self._settings.peak_minutes, self._settings.artifact_minutes)
        cached = getattr(self, "_graph_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        data = prepare_graph_data(
            path, peak_seconds=self._settings.peak_minutes * 60, artifact_seconds=self._settings.artifact_minutes * 60
        )
        self._graph_cache = (key, data)
        return data

    def _on_graph_configure(self, event: tk.Event) -> None:
        # Moving the window also sends <Configure>; only a new size needs a redraw,
        # and a drag-resize sends many in a row, so they are merged.
        if (event.width, event.height) == getattr(self, "_graph_drawn_size", None):
            return
        job = getattr(self, "_graph_redraw_job", None)
        if job is not None:
            self.root.after_cancel(job)
        self._graph_redraw_job = self.root.after(80, self._draw_session_graph)

    def _draw_session_graph(self, _event: tk.Event | None = None) -> None:
        self._graph_redraw_job = None
        canvas = getattr(self, "_graph_canvas", None)
        path = getattr(self, "_graph_path", None)
        if canvas is None or path is None or not canvas.winfo_exists():
            return
        try:
            data = self._graph_data(path)
        except (OSError, KeyError, ValueError, csv.Error) as exc:
            canvas.delete("all")
            canvas.create_text(
                24, 24, anchor="nw", text=f"Não foi possível ler o CSV: {exc}",
                fill="#8f3028", font=("TkDefaultFont", 11),
            )
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        self._graph_drawn_size = (width, height)
        canvas.create_text(
            width / 2, 18, text=f"Evolução · {data['session_name']}",
            fill="#202723", font=("TkDefaultFont", 13, "bold"),
        )
        if data["too_few"]:
            canvas.create_text(
                width / 2, height / 2,
                text="O gráfico aparecerá após algumas medições da sessão.",
                fill="#59645e", font=("TkDefaultFont", 11),
            )
            return
        samples, phases, baseline = data["samples"], data["phases"], data["baseline"]

        left, right, top, bottom = 72, width - 76, 82, height - 58
        # The growth rate gets its own strip under the plot, on the same time axis; the axis labels go under it.
        axis_bottom = bottom
        strip = None
        if self._settings.graph_show_rate and bottom - top >= RATE_STRIP_MIN_HEIGHT:
            strip = (bottom - (bottom - top) * RATE_STRIP_SHARE + 26, bottom)
            bottom = strip[0] - 26
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        # The graph starts at the mix (the zero of the session), even when the monitoring began later.
        monitoring_start = samples[0][0]
        mix_time = data.get("mix_time")
        if mix_time is not None and monitoring_start.tzinfo is not None and mix_time.tzinfo is None:
            mix_time = mix_time.astimezone()
        show_start = self._settings.graph_show_start
        first_time = mix_time if show_start and mix_time is not None and mix_time < monitoring_start else monitoring_start
        # The doubling forecast is drawn from the start, over the measured curve, to compare; the axis
        # goes on to its time while that is still to come.
        predicted = self._graph_forecast_time(path) if self._settings.graph_show_forecast else None
        if predicted is not None:
            predicted = predicted.astimezone(first_time.tzinfo) if first_time.tzinfo else predicted.replace(tzinfo=None)
            if predicted <= first_time:
                predicted = None
        end_time = max(predicted, samples[-1][0]) if predicted is not None else samples[-1][0]
        duration = max(1.0, (end_time - first_time).total_seconds())
        x_of = lambda stamp: left + (stamp - first_time).total_seconds() / duration * plot_width
        initial_volume = baseline  # the growth factor in the hover text counts from here
        # 0% is the initial volume; see volume_scale for what 100% is.
        baseline, full_scale, scale_label = volume_scale(
            baseline, [v for _, v, _ in samples], self._settings.graph_volume_scale
        )
        span = max(1e-6, full_scale - baseline)
        # Leave room under 0% so the latency stretch is not glued to the axis,
        # and above 100% so the peak does not touch the top edge (the phase
        # names sit there). The room has no ticks or labels, so nothing
        # outside 0-100% is ever shown.
        floor = bottom - plot_height * 0.12
        ceiling = top + plot_height * 0.09
        scale_height = floor - ceiling
        y_volume = lambda value: floor - min(1.0, max(0.0, (value - baseline) / span)) * scale_height

        temperatures = [temp for _, _, temp in samples if temp is not None]
        if temperatures:
            low, high = temperature_axis(temperatures, self._settings.graph_temperature_scale)
            y_temperature = lambda value: floor - (value - low) / (high - low) * scale_height
        else:
            low, high = 0.0, 1.0
            y_temperature = lambda value: floor

        for tick in range(5):
            fraction = tick / 4
            y = floor - fraction * scale_height
            canvas.create_line(left, y, right, y, fill="#e4e6df")
            canvas.create_text(left - 10, y, text=f"{fraction:.0%}", anchor="e", fill="#397a9b")
            if temperatures:
                value = low + fraction * (high - low)
                text = f"{value:.0f}°" if abs(value - round(value)) < 1e-6 else f"{value:.1f}°"
                canvas.create_text(right + 10, y, text=text, anchor="w", fill="#c46b35")
        canvas.create_line(left, top, left, bottom, fill="#397a9b", width=2)
        canvas.create_line(right, top, right, bottom, fill="#c46b35", width=2)
        canvas.create_line(left, bottom, right, bottom, fill="#59645e", width=2)
        # The axis titles are buttons: a click cycles the scale of that series.
        volume_title = canvas.create_text(
            left, top - 8, text=f"Volume (% do {scale_label}; 0% = início) ▾", anchor="sw", fill="#397a9b"
        )
        temperature_title = canvas.create_text(
            right, top - 8, text=f"Temperatura (°C) · {self._settings.graph_temperature_scale} ▾",
            anchor="se", fill="#c46b35",
        )
        for item, series in ((volume_title, "volume"), (temperature_title, "temperature")):
            canvas.tag_bind(item, "<Button-1>", lambda _e, series=series: self._cycle_graph_scale(series))
            canvas.tag_bind(item, "<Enter>", lambda _e: canvas.configure(cursor="hand2"))
            canvas.tag_bind(item, "<Leave>", lambda _e: canvas.configure(cursor="fleur"))

        for tick in range(5):
            fraction = tick / 4
            stamp = first_time + (end_time - first_time) * fraction
            x = left + fraction * plot_width
            canvas.create_line(x, axis_bottom, x, axis_bottom + 5, fill="#59645e")
            if strip is not None:
                canvas.create_line(x, top, x, axis_bottom, fill="#eef0ea")
            canvas.create_text(
                x, axis_bottom + 9, text=stamp.strftime("%d/%m\n%H:%M"),
                anchor="n", fill="#59645e", font=("TkDefaultFont", 8),
            )

        self._draw_phase_markers(canvas, samples, phases, x_of, top, bottom, plot_width, duration)

        if show_start and first_time < monitoring_start and (monitoring_start - first_time).total_seconds() > 0.02 * duration:
            # No readings between the mix and the start of the monitoring: join the zero to the first reading
            # with a pale line, and mark where the monitoring began.
            canvas.create_line(
                x_of(first_time), y_volume(initial_volume or baseline), x_of(monitoring_start), y_volume(samples[0][1]),
                fill="#a9c6d6", width=2,
            )
            start_x = x_of(monitoring_start)
            canvas.create_line(start_x, top, start_x, bottom, fill="#a9b0aa", dash=(2, 4))
            # At the foot of the plot, under the 0% line: the phase names own the top.
            canvas.create_text(
                start_x + 4, bottom - 4, text="início do monitoramento", anchor="sw", fill="#7d8a83",
                font=("TkDefaultFont", 8),
            )

        step = max(1, len(samples) // 300)
        volume_points = []
        for stamp, volume, _ in samples[::step]:
            volume_points.extend((x_of(stamp), y_volume(volume)))
        if len(volume_points) >= 4:
            canvas.create_line(*volume_points, fill="#397a9b", width=3, smooth=True)

        if initial_volume and initial_volume > 0:
            # Where the levain first reached twice its initial volume: a dot on the curve with the time since the mix.
            doubled = next((item for item in samples if item[1] >= 2 * initial_volume), None)
            if doubled is not None:
                stamp, volume, _ = doubled
                dot_x, dot_y = x_of(stamp), y_volume(volume)
                canvas.create_oval(dot_x - 5, dot_y - 5, dot_x + 5, dot_y + 5, fill="#2f7d4f", outline="#ffffff", width=2)
                try:
                    since = stamp - (mix_time if mix_time is not None else samples[0][0])
                except TypeError:  # one with a time zone and one without
                    since = stamp - samples[0][0]
                minutes = max(0, int(since.total_seconds() // 60))
                canvas.create_text(
                    dot_x - 8, dot_y - 4, text=f"✓ dobro · {minutes // 60}h{minutes % 60:02d}",
                    anchor="se", fill="#2f7d4f", font=("TkDefaultFont", 9, "bold"),
                )

        if predicted is not None and initial_volume:
            # An exponential from the initial volume at the start to twice that at the forecast time:
            # dotted and pale, so it reads as what was expected and not as what was measured.
            points = []
            for step in range(61):
                fraction = step / 60
                stamp = first_time + (predicted - first_time) * fraction
                points.extend((x_of(stamp), y_volume(initial_volume * 2 ** fraction)))
            canvas.create_line(*points, fill="#a9c6d6", width=2, dash=(2, 5))
            canvas.create_text(
                points[-2], points[-1] - 10, text=f"dobro {predicted:%H:%M}",
                anchor="s", fill="#7fa6bb", font=("TkDefaultFont", 9),
            )

        rates: list[tuple[float, float]] = []
        if strip is not None and len(samples) >= 6:
            stamps = [stamp.timestamp() for stamp, _, _ in samples]
            series = rate_series([(t, volume) for t, (_s, volume, _c) in zip(stamps, samples)])
            origin = first_time.timestamp()
            strip_top, strip_bottom = strip
            top_value = max([10.0] + [value for _t, value in series])
            high_rate = float(min(40, 5 * math.ceil(min(top_value, 40.0) * 1.1 / 5)))
            low_rate = float(max(-20, min(0.0, 5 * math.floor(min([0.0] + [value for _t, value in series]) / 5))))
            y_rate = lambda value: strip_bottom - (min(high_rate, max(low_rate, value)) - low_rate) / (high_rate - low_rate) * (strip_bottom - strip_top)
            canvas.create_text(
                left, strip_top - 8, text="Taxa de crescimento (%/h, últimos 15 min)", anchor="sw", fill="#2f8a5f",
            )
            for value in sorted({low_rate, 0.0, high_rate, (low_rate + high_rate) / 2}):
                y = y_rate(value)
                canvas.create_line(left, y, right, y, fill="#e4e6df")
                canvas.create_text(left - 10, y, text=f"{value:.0f}", anchor="e", fill="#2f8a5f")
            canvas.create_line(left, strip_top, left, strip_bottom, fill="#2f8a5f", width=2)
            canvas.create_line(left, strip_bottom, right, strip_bottom, fill="#59645e", width=2)
            zero_y = y_rate(0.0)
            canvas.create_line(left, zero_y, right, zero_y, fill="#59645e", dash=(3, 3))
            rates = [(left + (t - origin) / duration * plot_width, value) for t, value in series]
            if len(rates) >= 2:
                canvas.create_line(*[c for x, value in rates for c in (x, y_rate(value))], fill="#2f8a5f", width=2)

        previous = None
        for stamp, _, temperature in samples:
            if temperature is None:
                previous = None
                continue
            point = (x_of(stamp), y_temperature(temperature))
            if previous is not None:
                canvas.create_line(*previous, *point, fill="#c46b35", width=2)
            previous = point

        self._graph_hover = {
            "samples": samples, "xs": [x_of(stamp) for stamp, _, _ in samples], "initial": initial_volume,
            "y_volume": y_volume, "y_temperature": y_temperature, "bounds": (left, right, top, axis_bottom),
            "rates": rates, "mix_time": mix_time,
        }

        legend_y = 42
        canvas.create_line(left + 8, legend_y, left + 32, legend_y, fill="#397a9b", width=3)
        canvas.create_text(
            left + 38, legend_y, text="Volume estabilizado",
            anchor="w", fill="#202723",
        )
        # Two switches in the legend: the forecast curve and the stretch before the monitoring.
        for x, field, label in (
            (left + 190, "graph_show_forecast", "Previsão"), (left + 290, "graph_show_start", "Faixa inicial"),
            (left + 400, "graph_show_rate", "Taxa"),
        ):
            on = getattr(self._settings, field)
            item = canvas.create_text(
                x, legend_y, text=f"{'☑' if on else '☐'} {label}", anchor="w", fill="#202723" if on else "#8a948e",
            )
            canvas.tag_bind(item, "<Button-1>", lambda _e, field=field: self._toggle_graph_option(field))
            canvas.tag_bind(item, "<Enter>", lambda _e: canvas.configure(cursor="hand2"))
            canvas.tag_bind(item, "<Leave>", lambda _e: canvas.configure(cursor="fleur"))
        canvas.create_line(right - 190, legend_y, right - 166, legend_y, fill="#c46b35", width=3)
        canvas.create_text(
            right - 160, legend_y,
            text="Temperatura" if temperatures else "Temperatura indisponível",
            anchor="w", fill="#202723",
        )

    def _graph_hover_at(self, event: tk.Event) -> None:
        """Mark the reading nearest the pointer and say its time, growth factor and temperature."""
        canvas = getattr(self, "_graph_canvas", None)
        hover = getattr(self, "_graph_hover", None)
        if canvas is None or not hover:
            return
        canvas.delete("hover")
        left, right, top, bottom = hover["bounds"]
        if not left <= event.x <= right or not top <= event.y <= bottom:
            return
        xs, samples = hover["xs"], hover["samples"]
        index = min(range(len(xs)), key=lambda i: abs(xs[i] - event.x))
        stamp, volume, temperature = samples[index]
        x = xs[index]
        factor = volume / hover["initial"] if hover["initial"] > 0 else 0.0
        canvas.create_line(x, top, x, bottom, fill="#8a948e", dash=(3, 3), tags="hover")
        y = hover["y_volume"](volume)
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#397a9b", outline="#ffffff", width=2, tags="hover")
        lines = [stamp.strftime("%d/%m %H:%M"), f"{factor:.2f}x"]
        mix = hover.get("mix_time")
        if mix is not None:
            if mix.tzinfo is None and stamp.tzinfo is not None:
                mix = mix.astimezone()
            minutes = int((stamp - mix).total_seconds() // 60)
            if minutes >= 0:
                days, rest = divmod(minutes, 24 * 60)
                lines.insert(1, (f"{days}d " if days else "") + f"{rest // 60}h{rest % 60:02d} após mistura")
        rates = hover.get("rates") or []
        if rates:
            near = min(rates, key=lambda item: abs(item[0] - x))
            if abs(near[0] - x) <= 0.05 * (right - left):
                lines.append(f"{near[1]:+.1f}".replace(".", ",") + "%/h")
        if temperature is not None:
            y = hover["y_temperature"](temperature)
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#c46b35", outline="#ffffff", width=2, tags="hover")
            lines.append(f"{temperature:.1f} °C")
        text = canvas.create_text(0, 0, text="\n".join(lines), anchor="nw", fill="#202723", tags="hover")
        x0, y0, x1, y1 = canvas.bbox(text)
        box_width, box_height = x1 - x0 + 12, y1 - y0 + 8
        box_left = x + 12 if x + 12 + box_width <= right else x - 12 - box_width
        box_top = min(max(top, event.y - box_height // 2), bottom - box_height)
        canvas.coords(text, box_left + 6, box_top + 4)
        box = canvas.create_rectangle(
            box_left, box_top, box_left + box_width, box_top + box_height,
            fill="#fffefa", outline="#8a948e", tags="hover",
        )
        canvas.tag_lower(box, text)

    _GRAPH_PHASE_FILL = {
        LATENCY: "#f2f0e8",
        EXPONENTIAL: "#e6f0e8",
        STATIONARY: "#f7efd9",
        DECLINE: "#f3e3de",
    }

    def _draw_phase_markers(
        self, canvas, samples, phases, x_of, top, bottom, plot_width, duration
    ) -> None:
        """Shaded bands and dashed dividers for the growth-curve phases."""
        if len(phases) != len(samples) or not samples:
            return
        stamps = [sample[0] for sample in samples]
        segments = graph_phase_segments(stamps, phases)
        for index, (phase, start, end) in enumerate(segments):
            left_x = x_of(start) if index else x_of(stamps[0])
            right_x = x_of(segments[index + 1][1]) if index + 1 < len(segments) else x_of(end)
            band = canvas.create_rectangle(
                left_x, top, right_x, bottom, fill=self._GRAPH_PHASE_FILL[phase], outline=""
            )
            canvas.tag_lower(band)
            if index:
                canvas.create_line(left_x, top, left_x, bottom, fill="#a9b0aa", dash=(5, 4))
            if right_x - left_x >= 64:
                canvas.create_text(
                    (left_x + right_x) / 2, top + 10, text=phase, fill="#59645e",
                    font=("TkDefaultFont", 9, "italic"),
                )

    def _fill_resume_menu(self) -> None:
        """The saved sessions not yet ended, newest first, each with when it ran; a click asks before resuming it."""
        menu = self._resume_menu
        menu.delete(0, "end")
        entries = [entry for entry in list_sessions(self._session_log.directory) if not entry.ended]
        for entry in entries:
            menu.add_command(label=entry.label, command=lambda path=entry.path: self._confirm_resume(path))
        if not entries:
            menu.add_command(label="Nenhuma sessão em aberto", state="disabled")
        menu.add_separator()
        menu.add_command(label="Abrir um arquivo…", command=self._resume_from_file)

    def _confirm_resume(self, path: Path) -> None:
        if self._session_log.active:
            self._set_message("Encerre a sessão ativa antes de retomar outra.")
            return
        entry = describe_session(path)
        gap = "" if entry.last is None else f"\nÚltima leitura: {entry.last:%d/%m às %H:%M} (há {self._elapsed_text((datetime.now().astimezone() - entry.last).total_seconds())})."
        if not messagebox.askyesno(
            "Retomar sessão",
            f"Retomar ‘{entry.name}’?{gap}\n\n"
            "As novas leituras serão acrescentadas ao mesmo arquivo (o intervalo parado aparece como uma lacuna).",
            parent=self.root,
        ):
            return
        self._resume_from(path)

    def _resume_from_file(self) -> None:
        if self._session_log.active:
            self._set_message("Encerre a sessão ativa antes de retomar outra.")
            return
        selected = filedialog.askopenfilename(
            title="Retomar sessão", initialdir="data/sessions",
            filetypes=(("CSV", "*.csv"),), parent=self.root,
        )
        if selected:
            self._confirm_resume(Path(selected))

    def _resume_latest_session(self, name: str | None = None) -> None:
        if name:
            latest = find_session_csv(name, self._session_log.directory)
            if latest is None:
                self._set_message(f"Nenhuma sessão chamada ‘{name}’ para retomar.")
                return
        else:
            latest = latest_session_csv(self._session_log.directory)
        if latest is None:
            self._set_message("Nenhuma sessão anterior para retomar.")
            return
        self._resume_from(latest)

    def _resume_from(self, selected: Path) -> None:
        if self._session_log.active:
            self._set_message("Encerre a sessão ativa antes de retomar outra.")
            return
        try:
            self._session_log.snapshot_seconds = self._settings.snapshot_minutes * 60.0
            session_id, regions = self._session_log.resume(selected)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._set_message(f"Não foi possível retomar o CSV: {exc}")
            return
        self._session_name = self._session_log.session_name
        self._snapshot_var.set(str(round(self._session_log.snapshot_seconds / 60)))
        if self._session_log.ratio is None:  # a session from before the ratio was kept: read it from its name
            named = parse_ratio(self._session_log.session_name)
            if named is not None:
                try:
                    self._session_log.save_ratio(named)
                except OSError:
                    logging.exception("Falha ao salvar a proporção na sessão")
        if self._session_log.rotation is not None:  # the angle its pot, visor and mark were drawn at
            self._set_rotation(self._session_log.rotation, turn_visor=False)  # its regions are loaded below
        else:  # a session from before the angle was kept: it is taken to be at the one in use now
            try:
                self._session_log.save_rotation(self._settings.rotation)
            except OSError:
                logging.exception("Falha ao salvar o ângulo na sessão")
        known_phone = self._session_log.phone
        if known_phone is not None:  # what this session was sent with, not the last global choice
            self._settings = replace(
                self._settings,
                phone_enabled=bool(known_phone["enabled"]),
                phone_interval_seconds=int(known_phone.get("interval_seconds", self._settings.phone_interval_seconds)),
                phone_snapshot=bool(known_phone.get("snapshot", self._settings.phone_snapshot)),
            ).clamped()
            self._phone_enabled_var.set(self._settings.phone_enabled)
            self._phone_interval_var.set(str(self._settings.phone_interval_seconds))
            self._phone_snapshot_var.set(self._settings.phone_snapshot)
            self._apply_phone_settings()
        self._end_auto_detect()
        self._manual_pot_roi = regions.get("pot")
        self._temperature_roi = regions.get("thermometer")
        logging.info("Sessão retomada: pote %s, termômetro %s", self._manual_pot_roi, self._temperature_roi)
        self._pot_outline = self._session_log.pot_outline
        self._temperature_quad = self._session_log.face_quad
        if self._analyzer is not None:
            self._analyzer.temperature_quad = self._temperature_quad
            self._analyzer.outline = self._pot_outline
            self._analyzer.roi = self._manual_pot_roi
            self._analyzer.temperature_roi = self._temperature_roi
            self._analyzer.vessel_bounds = dict(self._session_log.vessel_bounds)
            self._analyzer.volume_offset = self._session_log.volume_offset
            self._analyzer.marker_y = self._session_log.manual_marker_y
            self._analyzer.reference_base = selected.with_suffix("")
            self._analyzer.load_references()
        self._manual_marker_y = self._session_log.manual_marker_y
        self._mix_time = self._session_log.mix_time
        self._reset_tracking()
        self._session_baseline = None
        try:
            with selected.open("r", newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            # Each row lands on the monotonic clock where it really happened, so the hours
            # the app was closed stay a gap and the live readings come after the history.
            now_wall, now_clock = datetime.now().astimezone(), time.monotonic()
            last_temperature = None
            for row in rows:
                stamp = datetime.fromisoformat(row["timestamp"])
                volume = float(row["volume_fraction"])
                baseline = float(row["initial_volume_fraction"]) if row.get("initial_volume_fraction") else None
                if self._session_baseline is None:
                    self._session_baseline = baseline if baseline is not None else volume
                elif baseline is not None and abs(baseline - self._session_baseline) > 1e-9:
                    # The initial level was marked again here: the latest mark wins,
                    # and the trend starts over, exactly as it did live.
                    self._session_baseline = baseline
                    self._reset_tracking(baseline)
                elapsed = now_clock - (now_wall - stamp).total_seconds()
                self._phase = self._phase_tracker.update(elapsed, volume, baseline)
                temperature = row.get("temperature_c")
                if temperature:
                    last_temperature = float(temperature)
                self._forecaster.add(elapsed, volume, last_temperature)
            first_levels = [float(row["initial_volume_fraction"]) for row in rows if row.get("initial_volume_fraction")]
            volumes = [float(row["volume_fraction"]) for row in rows[:90]]
            if first_levels and len(volumes) >= 9:
                # The level the session began at, as the graph takes it: the first stored one or the level the
                # readings settled at, whichever is lower. Recomputing it from the mark at each resume drifted.
                settled = min(float(np.median(volumes[i:i + 9])) for i in range(len(volumes) - 8))
                self._session_baseline = min(first_levels[0], settled)
                self._phase_tracker.set_baseline(self._session_baseline)
                self._baseline_locked = True
            if last_temperature is not None:
                self._last_known_temperature = last_temperature
                if self._analyzer is not None:
                    self._analyzer.anchor_temperature = last_temperature
            if self._analyzer is not None and rows:
                self._analyzer.anchor_volume = float(rows[-1]["volume_fraction"])
        except (KeyError, ValueError, IndexError):
            self._reset_tracking()
            self._session_baseline = None
        if self._session_log.geometry != GEOMETRY_VERSION:
            # The volume is measured more accurately now (the jar's real base, not the
            # steel bowl under it), so the initial level saved with the old measurement
            # no longer matches. Take it again from the mark, once.
            self._session_baseline = None
            try:
                self._session_log.save_geometry(GEOMETRY_VERSION)
            except OSError:
                logging.exception("Falha ao salvar a versão da medição")
        self.session_button.configure(text="Encerrar sessão")
        self.pause_button.configure(state="normal")
        self.metric_values["Sessão"].configure(
            text=self._session_summary(resumed=True)
        )
        self.metric_values["Estado do levain"].configure(text=self._phase)
        self.mix_time_button.configure(
            text="Hora da mistura" if self._mix_time is None else f"Mistura: {self._mix_time.strftime('%d/%m %H:%M')}"
        )
        self._update_ratio_label()
        self._follow_graph_to(self._session_log.path)
        self.auto_pot_button.configure(state="normal" if self._manual_pot_roi else "disabled")
        self._set_message(f"Sessão retomada; as próximas medições serão acrescentadas em {self._session_log.path}.")

    def _clear_session(self) -> None:
        """Close the session (saving it) and leave the app as if it had been opened without --resume."""
        if not messagebox.askyesno(
            "Limpar",
            "Encerrar a sessão e limpar a tela?\n"
            "A sessão fica salva e pode ser retomada. O pote, o contorno, o visor e a marca saem da tela.",
            parent=self.root,
        ):
            return
        self._end_auto_detect()
        if self._session_log.active:
            self._toggle_session(confirm=False)
        self._session_log = SessionLog(sample_seconds=self._settings.record_interval_seconds)
        self._settings = load_settings()  # the global choices, not what the closed session carried
        self._phone_enabled_var.set(self._settings.phone_enabled)
        self._phone_interval_var.set(str(self._settings.phone_interval_seconds))
        self._phone_snapshot_var.set(self._settings.phone_snapshot)
        self._snapshot_var.set(str(self._settings.snapshot_minutes))
        self._apply_phone_settings()
        self._manual_pot_roi = self._pot_outline = None
        self._temperature_roi = self._temperature_quad = None
        self._manual_marker_y = None
        self._mix_time = None
        self._session_name = ""
        self._measurement = None
        self._session_baseline = None
        self._last_container_box = None
        self._temperature_c = None
        self._last_known_temperature = None
        self._forecast_saved_at = 0.0
        self._forecast_prediction = None
        self._reset_tracking()
        self._set_paused(False)
        if self._analyzer is not None:
            self._analyzer.roi = self._analyzer.outline = None
            self._analyzer.temperature_roi = self._analyzer.temperature_quad = None
            self._analyzer.vessel_bounds.clear()
            self._analyzer.marker_y = None
            self._analyzer.anchor_temperature = None
            self._analyzer.reset_join()
            self._analyzer.reference_base = None
            self._analyzer.load_references()
        self._phase = "Aguardando sessão"
        self.metric_values["Sessão"].configure(text="Nenhuma sessão iniciada")
        self.metric_values["Pote"].configure(text="Aguardando calibração")
        self.metric_values["Volume relativo"].configure(text="—", foreground="")
        self.metric_values["Estado do levain"].configure(text="Aguardando análise")
        self._update_temperature_label()
        self._update_double_forecast(time.monotonic())
        self.draw_pot_button.configure(text="Pote")
        self.auto_pot_button.configure(state="disabled")
        self.mix_time_button.configure(text="Hora da mistura")
        self._update_ratio_label()
        self._set_message("Sessão fechada; a tela está limpa. Marque o pote e inicie outra, ou retome uma salva.")

    def _toggle_pause(self) -> None:
        if not self._session_log.active:
            return
        self._set_paused(not self._paused)
        if self._paused:
            self._set_message("Sessão pausada: nada é gravado até continuar. Pode mexer na fermentadora.")
            return
        # The pot may have been moved and put back: carry the series on from the last logged level.
        if self._analyzer is not None and self._session_log.last_volume is not None:
            self._analyzer.anchor_volume = self._session_log.last_volume
            self._analyzer.rejoin()
        self._set_message("Sessão retomada.")

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.configure(text="Continuar" if paused else "Pausar")
        if self._session_log.active:
            self.metric_values["Estado do levain"].configure(text="Pausada" if paused else self._phase)

    def _update_session(self, measurement: ContainerMeasurement | None) -> None:
        if not self._session_log.active or self._paused:
            return
        try:
            self._session_log.snapshot(time.monotonic(), self._last_raw_frame)
        except OSError:
            logging.exception("Falha ao gravar o snapshot da sessão")
        if measurement is None:
            return
        if self._analyzer is not None and self._analyzer.alignment_status:
            return  # the pot is out of its place (lifted, covered): what is read now is not the levain
        now = time.monotonic()
        self._phase = self._phase_tracker.update(
            now, measurement.volume_fraction, measurement.initial_volume_fraction
        )
        self._forecaster.add(now, measurement.volume_fraction, self._temperature_c)
        self.metric_values["Estado do levain"].configure(text=self._phase)
        self._update_double_forecast(now)
        self._record_session(now)

    def _update_double_forecast(self, monotonic_time: float) -> None:
        label = self.metric_values["Previsão do dobro"]
        self._forecast_prediction = None
        if not self._session_log.active:
            label.configure(text="Inicie uma sessão para estimar")
            return
        if not self._phase_tracker.risen:
            # No rise yet: whatever slope the readings have is noise. The forecast comes from the table
            # (time to double at 24 °C for this ratio, scaled by the temperatures since the mix), and a
            # forecast made from that noise before is not shown nor kept.
            try:
                self._session_log.clear_forecast()
            except OSError:
                logging.exception("Falha ao apagar a previsão da sessão")
            mix = self._mix_time or datetime.now().astimezone()
            ratio = self._session_log.ratio
            table = self._forecaster.table_forecast(
                (datetime.now().astimezone() - mix).total_seconds(), monotonic_time,
                self._settings.base_doubling_hours, ratio,
            )
            if table is None:
                label.configure(text="Aguardando a temperatura para estimar pela tabela")
                return
            basis = "tabela" + ("" if ratio is None else " · " + ":".join(f"{part:g}" for part in ratio))
            if table.minutes <= 0:
                label.configure(text=f"Pela tabela já devia ter dobrado\n{basis}")
                return
            self._forecast_prediction = datetime.now().astimezone() + timedelta(minutes=table.minutes)
            label.configure(text=self._forecast_text(self._forecast_prediction, basis, "antes do crescimento"))
            return
        if self._forecaster.rate_per_thermal_minute() is None:
            known = self._session_log.last_forecast
            if known:
                try:
                    self._forecast_prediction = datetime.fromisoformat(known["at"])
                    label.configure(text=self._forecast_text(self._forecast_prediction, known["basis"], "última conhecida"))
                    return
                except (ValueError, KeyError):
                    self._forecast_prediction = None
            label.configure(text="Calculando tendência; aguarde ao menos 2 minutos")
            return
        if self._measurement is None:
            label.configure(text="Aguardando medição do pote")
            return
        now = datetime.now().astimezone()
        current = self._measurement.volume_fraction
        mix_volume = self._measurement.initial_volume_fraction or self._session_baseline
        if mix_volume is None or mix_volume <= 0.01:
            label.configure(text="Sem nível inicial para estimar o dobro")
            return
        basis = (
            "início marcado"
            if self._measurement.marker_y is not None
            else "1ª leitura"
        )
        target = mix_volume * 2
        if target > 1.0:
            label.configure(text="O dobro estimado excede a capacidade do pote")
            return
        already = self._session_log.reached_at(2.0)
        if current >= target or already is not None:
            # It came true (or did once, and the level has since slipped under the target): no new forecast.
            self._session_log.clear_forecast()
            reached = already or now
            label.configure(
                text=self._reached_text(reached, self._session_log.reached_before_monitoring(2.0))
            )
            return
        forecast = self._forecaster.minutes_until(current, target)
        if forecast is None:
            known = self._session_log.last_forecast
            if known:
                try:
                    self._forecast_prediction = datetime.fromisoformat(known["at"])
                    label.configure(text=self._forecast_text(self._forecast_prediction, known["basis"], "última conhecida"))
                    return
                except (ValueError, KeyError):
                    self._forecast_prediction = None
            label.configure(text="Sem crescimento sustentado para estimar")
            return
        minutes = forecast.minutes
        if forecast.uses_temperature:
            basis += f" + temp. ({forecast.model})"
        if minutes > 10080:
            label.configure(text="Projeção muito distante; aguardando mais crescimento")
            return
        predicted_at = now + timedelta(minutes=minutes)
        if minutes > 600:  # leave a trace of what produced an unusually long forecast
            logging.warning(
                "Previsão longa do dobro: %.0f min (%s) · volume %.3f, alvo %.3f, taxa %s por min térmico",
                minutes, basis, current, target, self._forecaster.rate_per_thermal_minute(),
            )
        known = self._session_log.last_forecast
        if known is None or monotonic_time - self._forecast_saved_at >= 60:
            try:
                self._session_log.save_forecast(predicted_at, basis)
                self._forecast_saved_at = monotonic_time
            except OSError:
                logging.exception("Falha ao salvar a previsão do dobro")
        self._forecast_prediction = predicted_at
        label.configure(text=self._forecast_text(predicted_at, basis))

    def _since_mix_text(self, moment: datetime) -> str:
        """'2h47 após mistura' for a moment, or '' when the mix time is unknown."""
        if self._mix_time is None:
            return ""
        minutes = max(0, int((moment - self._mix_time).total_seconds() // 60))
        days, rest = divmod(minutes, 24 * 60)
        hours, mins = divmod(rest, 60)
        return f"{days}d {hours}h após mistura" if days else f"{hours}h{mins:02d} após mistura"

    def _peak_line(self) -> str:
        """The peak's time and how long after the mix, once the levain is at or past it."""
        if self._phase not in (PhaseTracker.PEAK, PhaseTracker.FALL_CHECK, PhaseTracker.FALLING):
            return ""
        peak = self._session_log.peak_at()
        if peak is None:
            return ""
        local = peak.astimezone()
        since = self._since_mix_text(peak)
        return f"\nPico às {local:%H:%M}" + (f" · {since}" if since else "")

    def _reached_text(self, reached: datetime, before_monitoring: bool) -> str:
        """When the doubling happened; if it was already there at the first reading, only that is known."""
        local = reached.astimezone()
        if before_monitoring:
            return (
                "O dobro do volume inicial já tinha sido alcançado quando o monitoramento começou "
                f"({local:%H:%M} de {local:%d/%m})"
            )
        since = self._since_mix_text(reached)
        return (f"O dobro do volume inicial já foi alcançado às {local:%H:%M} de {local:%d/%m}"
                + (f"\n{since}" if since else "") + self._peak_line())

    def _forecast_text(self, predicted_at: datetime, basis: str, note: str = "") -> str:
        mix_elapsed = predicted_at - (self._mix_time or predicted_at)
        total_minutes = max(0, int(mix_elapsed.total_seconds() // 60))
        elapsed_days, remainder = divmod(total_minutes, 24 * 60)
        elapsed_hours, elapsed_minutes = divmod(remainder, 60)
        if elapsed_days:
            elapsed_text = f"{elapsed_days}d {elapsed_hours}h após mistura"
        else:
            elapsed_text = f"{elapsed_hours}h{elapsed_minutes:02d} após mistura"
        # Three short lines: when, how long after mixing, and what it rests on.
        # The third line is kept short so it does not wrap and change the tile's height.
        for long, short in (("início marcado", "marca"), ("1ª leitura", "1ª leit."), (" + temp. ", " +temp ")):
            basis = basis.replace(long, short)
        note = {"última conhecida": "salva", "antes do crescimento": "pré-subida"}.get(note, note)
        return f"{predicted_at.strftime('%d/%m às %H:%M')}\n{elapsed_text}\n{basis}" + (f" · {note}" if note else "")

    def _ask_mix_time(self, default_time: datetime) -> datetime | None:
        """Ask for the mix time; None when cancelled or not valid (the reason is shown)."""
        entered = simpledialog.askstring(
            "Hora da mistura",
            "Informe data e hora da mistura (DD/MM/AAAA HH:MM):",
            initialvalue=default_time.strftime("%d/%m/%Y %H:%M"),
            parent=self.root,
        )
        if entered is None:
            return None
        try:
            mix_time = datetime.strptime(entered.strip(), "%d/%m/%Y %H:%M").astimezone()
        except ValueError:
            self._set_message("Formato inválido. Use DD/MM/AAAA HH:MM, por exemplo 30/10/2026 08:15.")
            return None
        if mix_time > datetime.now().astimezone():
            self._set_message("A hora da mistura não pode estar no futuro.")
            return None
        return mix_time

    def _set_mix_time(self) -> None:
        mix_time = self._ask_mix_time(self._mix_time or datetime.now().astimezone())
        if mix_time is None:
            return
        self._mix_time = mix_time
        if self._session_log.active:
            self._session_log.set_mix_time(mix_time)
            self._record_session(time.monotonic(), force=True)
            self.metric_values["Sessão"].configure(text=self._session_summary())
        self.mix_time_button.configure(text=f"Mistura: {mix_time.strftime('%d/%m %H:%M')}")
        self._update_double_forecast(time.monotonic())
        self._set_message(f"Hora da mistura definida para {mix_time.strftime('%d/%m/%Y às %H:%M')}.")

    def _record_session(self, monotonic_time: float, *, force: bool = False) -> None:
        if self._measurement is None:
            return
        try:
            self._session_log.record(
                monotonic_time,
                self._measurement,
                self._phase,
                temperature_c=self._temperature_c,
                force=force,
            )
        except OSError as exc:
            logging.exception("Falha ao gravar a sessão")
            self._set_message(f"Falha ao gravar CSV da sessão: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="levainpeak")
    parser.add_argument(
        "--resume", "--retomar", dest="resume_last", nargs="?", const=True, default=False, metavar="NOME",
        help="retoma a sessão gravada mais recentemente em data/sessions, ou a de NOME (nome da sessão ou do arquivo)",
    )
    arguments = parser.parse_args()
    root = tk.Tk()
    LevainPeakApp(root, resume_last=arguments.resume_last)
    root.mainloop()


if __name__ == "__main__":
    main()
