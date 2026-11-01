"""Tk screen for viewing the live camera stream."""

from __future__ import annotations

import argparse
import csv
import json
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
from levainpeak.config import load_camera_config
from levainpeak.debug_view import DebugWindow
from levainpeak.floating import FloatingWindowControls
from levainpeak.forecast import DoublingForecaster
from levainpeak.graph_data import (TEMPERATURE_SCALES, VOLUME_SCALES, prepare_graph_data,
                                   temperature_axis, volume_scale)
from levainpeak.graph_phases import (DECLINE, EXPONENTIAL, LATENCY, STATIONARY,
                                     graph_phase_segments)
from levainpeak.phone_config import load_phone_config, local_link, online_link
from levainpeak.phone_push import PhonePusher, make_snapshot
from levainpeak.sessions import (GEOMETRY_VERSION, PhaseTracker, SessionLog,
                                 latest_session_csv)
from levainpeak.regions_store import load_regions_file, save_regions_file
from levainpeak.settings import (DEFAULT_INK_CUT_DAY, DEFAULT_INK_CUT_INFRARED, INK_CUT_DAY,
                                 INK_CUT_INFRARED, MAX_OVERLAY_BRIGHTNESS,
                                 MAX_PHONE_SECONDS, MAX_RECORD_SECONDS, MIN_OVERLAY_BRIGHTNESS,
                                 MIN_PHONE_SECONDS, MIN_RECORD_SECONDS,
                                 Settings, load_settings, save_settings)
from levainpeak.stream import RtspStream
from levainpeak.vision import (ContainerMeasurement, draw_measurement,
                               set_initial_marker,
                               set_initial_volume_reference)

GRAPH_BORDER = 10


class LevainPeakApp:
    def __init__(self, root: tk.Tk, *, resume_last: bool = False) -> None:
        self.root = root
        self.root.title("LevainPeak")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1180, max(760, screen_width - 80))
        height = min(760, max(560, screen_height - 80))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(760, 560)
        self.root.configure(background="#f4f1e8")

        self._image: ImageTk.PhotoImage | None = None
        self._video_image_item: int | None = None
        self._stream: RtspStream | None = None
        self._analyzer: ContainerAnalyzer | None = None
        self._measurement: ContainerMeasurement | None = None
        self._last_displayed_frame_number = 0
        self._last_stream_status: str | None = None
        self._manual_marker_y: int | None = None
        self._realigned_at = float("-inf")  # when the contour last followed the pot by itself
        self._marker_mode = False
        self._roi_mode = False
        self._roi_target = "pot"
        self._roi_drag_start: tuple[int, int] | None = None
        self._roi_preview_item: int | None = None
        self._manual_pot_roi: tuple[int, int, int, int] | None = None
        self._pot_outline: tuple[tuple[int, int], ...] | None = None  # pot contour drawn by the user
        self._temperature_quad: tuple[tuple[int, int], ...] | None = None  # LCD face corners drawn by the user
        self._outline_target = "pot"
        self._outline_mode = False
        self._outline_points: list[list[int]] = []
        self._outline_drag: int | None = None
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
        self._phone_pusher: PhonePusher | None = None
        self._phone_applied: tuple | None = None
        self._phone_status_text = ""
        self._live_state: dict = {}
        self._phase_tracker = PhaseTracker()
        self._forecaster = DoublingForecaster()
        self._phase = "Aguardando sessão"
        self._mix_time: datetime | None = None
        self._line_height = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        try:
            self._stream = RtspStream(load_camera_config())
            self._stream.start()
            self._analyzer = ContainerAnalyzer(self._stream)
            self._analyzer.ink_cut_infrared = self._settings.ink_cut_infrared
            self._analyzer.ink_cut_day = self._settings.ink_cut_day
            self._load_saved_regions()
            self._analyzer.start()
            self._set_message("Iniciando conexão com a câmera…")
        except (FileNotFoundError, ValueError) as exc:
            self._set_message(str(exc))
            self.status_value.configure(text="Configuração necessária", foreground="#a43d32")

        self._apply_phone_settings()
        self._tick_phone_status()
        self.root.after(40, self._refresh)
        if resume_last:
            self.root.after(200, self._resume_latest_session)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding=(20, 16))
        header.pack(fill="x")
        ttk.Label(header, text="LevainPeak", font=("TkDefaultFont", 20, "bold")).pack(side="left")
        self.status_value = ttk.Label(header, text="Aguardando conexão", font=("TkDefaultFont", 10, "bold"))
        self.status_value.pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        monitor = ttk.Frame(self.notebook)
        settings_tab = ttk.Frame(self.notebook, padding=20)
        self.notebook.add(monitor, text="Monitor")
        self.notebook.add(settings_tab, text="Configurações")
        self._build_settings_tab(settings_tab)

        body = ttk.Frame(monitor, padding=(20, 0, 20, 16))
        body.pack(fill="both", expand=True)

        video_panel = ttk.LabelFrame(body, text="Câmera", padding=8)
        video_panel.pack(side="left", fill="both", expand=True)
        self.video = tk.Canvas(
            video_panel,
            background="#202723",
            highlightthickness=0,
            width=480,
            height=270,
        )
        self.video.pack(fill="both", expand=True)
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
        self.root.bind("<d>", self._toggle_detail, add="+")

        metrics = ttk.Frame(monitor, padding=(20, 0, 20, 12))
        metrics.pack(fill="x")
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
            tile = ttk.LabelFrame(metrics, text=title, padding=(10, 7))
            label = ttk.Label(tile, text=value, anchor="nw", justify="left", wraplength=240)
            label.pack(fill="x", anchor="w")
            label.bind("<Configure>", self._fit_wraplength, add="+")
            self._metric_tiles.append(tile)
            self.metric_values[title] = label
        self._metrics_container = metrics
        for column in range(3):
            self._metrics_container.columnconfigure(column, weight=1, uniform="metrics")
        for index, tile in enumerate(self._metric_tiles):
            tile.grid(row=index // 3, column=index % 3, sticky="nsew", padx=4, pady=4)
        self._reserve_metric_rows(lines=3)

        footer = ttk.Frame(monitor, padding=(20, 0, 20, 14))
        footer.pack(fill="x")
        self.instructions = ttk.Label(
            footer,
            text="Marcar início: passe o mouse sobre o pote e clique para confirmar.",
            anchor="w", justify="left",
        )
        self.instructions.pack(fill="x", pady=(0, 2))
        # The message area always holds two lines, so a longer message never
        # pushes the rest of the window around.
        message_area = ttk.Frame(footer, height=2 * self._line_height + 2)
        message_area.pack(fill="x", pady=(0, 6))
        message_area.pack_propagate(False)
        self.message = ttk.Label(message_area, text="", anchor="nw", justify="left")
        self.message.pack(fill="both", expand=True)
        footer.bind("<Configure>", self._wrap_footer_labels, add="+")

        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")

        def group(title: str, row: int, column: int, columnspan: int = 1) -> ttk.LabelFrame:
            frame = ttk.LabelFrame(buttons, text=title, padding=(8, 4))
            frame.grid(
                row=row, column=column, columnspan=columnspan,
                sticky="w", padx=(0, 8), pady=(0, 4),
            )
            return frame

        def button(frame, text, command, width=None, state="normal"):
            widget = ttk.Button(frame, text=text, command=command, width=width, state=state)
            widget.pack(side="left", padx=(0, 6))
            return widget

        regions = group("Marcar Regiões", 0, 0)
        level = group("Nível inicial", 0, 1)
        files = group("Arquivos", 1, 0)
        session = group("Sessão", 1, 1)
        self.draw_pot_button = button(
            regions, "Pote", lambda: self._toggle_roi_mode("pot"), width=8
        )
        self.draw_outline_button = button(
            regions, "Contorno", lambda: self._toggle_outline_mode("pot"), width=9
        )
        self.draw_thermometer_button = button(
            regions, "Termômetro", lambda: self._toggle_roi_mode("thermometer"), width=11
        )
        self.draw_face_button = button(
            regions, "Visor", lambda: self._toggle_outline_mode("thermometer"), width=8
        )
        self.auto_pot_button = button(
            regions, "Automático", self._use_automatic_detection, width=10,
            state="disabled",
        )
        self.marker_button = button(
            level, "Marcar início", self._toggle_marker_mode, width=13
        )
        self.detail_button = button(level, "Ver detecção", self._toggle_detail, width=15)
        self.mix_time_button = button(
            session, "Hora da mistura", self._set_mix_time, width=21
        )
        self.session_button = button(
            session, "Iniciar sessão", self._toggle_session, width=15
        )
        self.resume_button = button(files, "Retomar", self._resume_session, width=9)
        self.open_csv_button = button(files, "CSV", self._open_session_csv, width=6)
        self.graph_button = ttk.Menubutton(files, text="Gráfico ▾", width=9, direction="below")
        graph_menu = tk.Menu(self.graph_button, tearoff=False)
        graph_menu.add_command(label="Sessão atual", command=self._show_session_graph)
        graph_menu.add_command(
            label="Escolher uma sessão salva…", command=lambda: self._show_session_graph(choose=True)
        )
        self.graph_button.configure(menu=graph_menu)
        self.graph_button.pack(side="left", padx=(0, 6))
        self.debug_button = button(files, "Diagnóstico", self._open_debug_view, width=11)

    def _wrap_footer_labels(self, event: tk.Event) -> None:
        wrap = max(200, event.width - 48)  # the footer has 20 px of padding per side
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
        needed = probe.master.winfo_reqheight() + 8  # the grid pads each tile by 4 px
        probe.configure(text=original, wraplength=wrap)
        for row in range(2):
            self._metrics_container.rowconfigure(row, minsize=needed)

    def _annotate(self, frame, measurement):
        return draw_measurement(
            frame, measurement, detail=self._show_detail, faint_box=self._pot_outline is not None
        )

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

    def _persist_regions(self) -> None:
        """Write the marked regions (and the mark) to disk now, session or not."""
        try:
            save_regions_file(
                pot=self._manual_pot_roi, outline=self._pot_outline, thermometer=self._temperature_roi,
                face_quad=self._temperature_quad, marker_y=self._manual_marker_y,
            )
        except OSError:
            logging.exception("Falha ao salvar as regiões marcadas")

    def _load_saved_regions(self) -> None:
        """The regions marked in an earlier run; a resumed session replaces them with its own."""
        saved = load_regions_file()
        if not saved:
            return
        self._manual_pot_roi = saved["pot"]
        self._pot_outline = saved["outline"]
        self._temperature_roi = saved["thermometer"]
        self._temperature_quad = saved["face_quad"]
        self._manual_marker_y = saved["marker_y"]
        if self._analyzer is not None:
            self._analyzer.roi = self._manual_pot_roi
            self._analyzer.outline = self._pot_outline
            self._analyzer.temperature_roi = self._temperature_roi
            self._analyzer.temperature_quad = self._temperature_quad
            self._analyzer.marker_y = self._manual_marker_y
            self._analyzer.load_references()
        if hasattr(self, "auto_pot_button") and self._manual_pot_roi:
            self.auto_pot_button.configure(state="normal")

    def _persist_marker(self) -> None:
        """Keep the hand-placed initial-level mark in the session file."""
        self._persist_regions()
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
            text = (
                f"Pote reposicionado: {dx:+d} px na horizontal, {dy:+d} px na vertical"
                f"{f', tamanho {size:+.0f}%' if abs(size) >= 1 else ''}. O contorno e a marca acompanharam."
            )
        logging.info(text)
        self._realigned_at = time.monotonic()
        self._set_message(text)

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

    def _refresh(self) -> None:
        started = time.perf_counter()
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
                self._analyzer.align_enabled = not (self._roi_mode or self._outline_mode)
                self._take_alignment()
            if status != self._last_stream_status:
                self._last_stream_status = status
                if self._measurement is None:
                    self._set_message(status)
            analysis_changed = False
            if self._analyzer is not None:
                result = self._analyzer.latest_result()
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
                    if self._measurement is not None and self._manual_marker_y is not None:
                        self._measurement = set_initial_marker(
                            self._measurement, self._manual_marker_y
                        )
                    if (
                        self._measurement is not None
                        and self._session_log.active
                    ):
                        if self._session_baseline is None:
                            self._session_baseline = (
                                self._measurement.initial_volume_fraction
                                if self._measurement.initial_volume_fraction is not None
                                else self._measurement.volume_fraction
                            )
                            self._reset_tracking(self._session_baseline)
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
            self.metric_values["Volume relativo"].configure(text="—")
            self.metric_values["Estado do levain"].configure(text="Sem levain no pote")
            if not self._marker_mode and not self._roi_mode and not self._outline_mode:
                self._set_message("O contorno do pote está marcado e não há levain dentro dele.")
            return
        if self._measurement is None:
            self.metric_values["Pote"].configure(text="Procurando recipiente…")
            self.metric_values["Volume relativo"].configure(text="—")
            self.metric_values["Estado do levain"].configure(text="Aguardando recipiente visível")
            if not (self._roi_mode or self._outline_mode or self._marker_mode):
                self._set_message("A câmera está ativa; não encontrei um contorno de pote confiável neste frame.")
            return

        measurement = self._measurement
        self.metric_values["Pote"].configure(text="Recipiente detectado")
        self.metric_values["Volume relativo"].configure(
            text=self._volume_text(measurement)
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
    def _volume_text(measurement: ContainerMeasurement) -> str:
        capacity = f"≈ {measurement.volume_fraction:.0%} da capacidade"
        if measurement.growth_factor is not None:
            return f"{capacity}\n≈ {measurement.growth_factor:.1f}× do nível inicial"
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

    def _toggle_marker_mode(self) -> None:
        if self._marker_mode:
            self._cancel_marker_mode()
            return
        if self._measurement is None:
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
        if hasattr(self, "draw_outline_button"):
            self.draw_outline_button.configure(text="Contorno")
            self.draw_face_button.configure(text="Visor")
        if was_active and hasattr(self, "video"):
            self.video.configure(cursor="")

    def _outline_handle_at(self, point: tuple[int, int]) -> int | None:
        reach = 40  # picture pixels; the corners are drawn with a 12 px radius
        distances = [
            (float(np.hypot(point[0] - x, point[1] - y)), index)
            for index, (x, y) in enumerate(self._outline_points)
        ]
        distance, index = min(distances)
        return index if distance <= reach else None

    def _move_outline_handle(self, event: tk.Event) -> None:
        if self._outline_drag is None or self._source_frame_size is None:
            return
        point = self._source_point(event)
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
        self._cancel_outline_mode()
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
            self._session_baseline = None
            self._reset_tracking()
            self._session_log.save_regions(roi, self._temperature_roi, outline=outline)
        self._set_message("Contorno do pote definido. Procurando o nível do levain dentro dele.")

    def _apply_face_quad(self) -> None:
        quad = tuple((int(x), int(y)) for x, y in self._outline_points)
        width, height = self._source_frame_size
        xs, ys = [x for x, _ in quad], [y for _, y in quad]
        margin = int(0.12 * max(max(xs) - min(xs), max(ys) - min(ys)))
        roi = (max(0, min(xs) - margin), max(0, min(ys) - margin),
               min(width, max(xs) + margin), min(height, max(ys) + margin))
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
        self._persist_regions()
        self._temperature_c = None
        self._update_temperature_label()
        self._set_message("Face do visor definida; procurando a temperatura.")

    def _cancel_roi_mode(self) -> None:
        self._cancel_outline_mode()
        self._roi_mode = False
        self._roi_drag_start = None
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
            return
        if not self._roi_mode:
            if self._zoom > 1.0 and not self._marker_mode:
                self._pan_drag_start = (event.x, event.y)
                self._pan_start_center = self._zoom_center
                self.video.configure(cursor="fleur")
            return
        point = self._source_point(event)
        if point is None:
            return
        self._roi_drag_start = point

    def _preview_roi(self, event: tk.Event) -> None:
        if not self._roi_mode or self._roi_drag_start is None:
            return
        point = self._source_point(event)
        if point is None:
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
            return
        if self._pan_drag_start is not None:
            self._pan_drag_start = None
            self.video.configure(cursor="hand2" if self._zoom > 1 else "")
            return
        if not self._roi_mode or self._roi_drag_start is None:
            return
        point = self._source_point(event)
        start = self._roi_drag_start
        self._roi_drag_start = None
        if point is None:
            self._set_message("Desenhe o retângulo inteiramente sobre a imagem do vídeo.")
            return
        left, right = sorted((start[0], point[0]))
        top, bottom = sorted((start[1], point[1]))
        minimum_size = (80, 120) if self._roi_target == "pot" else (40, 20)
        if right - left < minimum_size[0] or bottom - top < minimum_size[1]:
            self._set_message("A área ficou pequena; desenhe um retângulo maior sobre o visor ou o pote.")
            return
        selected_roi = (left, top, right, bottom)
        if self._roi_target == "pot":
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
            self._persist_regions()
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
        self._persist_regions()
        self._cancel_roi_mode()
        self._set_message(message)

    def _use_automatic_detection(self) -> None:
        self._manual_pot_roi = None
        self._pot_outline = None
        if self._analyzer is not None:
            self._analyzer.roi = None
            self._analyzer.outline = None
            self._analyzer.vessel_bounds.clear()
        self._manual_marker_y = None
        self._persist_marker()
        self._last_container_box = None
        self._measurement = None
        self.auto_pot_button.configure(state="disabled")
        self.draw_pot_button.configure(text="Pote")
        if self._session_log.active:
            self._session_baseline = None
            self._reset_tracking()
            self._session_log.save_regions(None, self._temperature_roi, outline=None)
        self._set_message("Detecção automática ativada.")

    def _source_point(self, event: tk.Event) -> tuple[int, int] | None:
        if self._source_frame_size is None or self._displayed_image_size is None:
            return None
        image_width, image_height = self._display_visible_size or self._displayed_image_size
        offset_x, offset_y = self._display_offsets
        if not (offset_x <= event.x < offset_x + image_width and offset_y <= event.y < offset_y + image_height):
            return None
        source_width, source_height = self._source_frame_size
        crop_x, crop_y = self._display_crop_origin
        full_width, full_height = self._displayed_image_size
        source_x = int((event.x - offset_x + crop_x) * source_width / full_width)
        source_y = int((event.y - offset_y + crop_y) * source_height / full_height)
        return source_x, source_y

    def _preview_marker(self, event: tk.Event) -> None:
        if not self._marker_mode or self._measurement is None:
            return
        point = self._source_point(event)
        measurement = self._measurement
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
        measurement = self._measurement
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
        self._persist_marker()
        self._capture_pot_reference()
        self._measurement = set_initial_marker(measurement, source_y)
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

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        delta = getattr(event, "delta", 0)
        direction = 1 if delta > 0 or getattr(event, "num", None) == 4 else -1
        self._zoom = min(4.0, max(1.0, self._zoom * (1.25 if direction > 0 else 0.8)))
        if self._zoom == 1:
            self._zoom_center = (0.5, 0.5)
        self._redraw_current_frame()

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
        if not regions and not self._outline_mode:
            return frame
        height, width = frame.shape[:2]
        drawn = {"pot": self._pot_outline, "thermometer": self._temperature_quad}
        if self._roi_mode or self._outline_mode:
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
                cv2.rectangle(overlay, (left, top), (right, bottom), (190, 200, 195), 1, cv2.LINE_AA)
                cv2.addWeighted(overlay, 0.35, result, 0.65, 0, result)
                label_x, label_y = min(x for x, _ in shape), min(y for _, y in shape)
            else:
                cv2.rectangle(result, (left, top), (right, bottom), color, 2)
                label_x, label_y = left, top
            if shape and label == "POTE":
                continue  # the measurement label ("Pote | volume ...") already names it
            cv2.putText(result, label, (label_x, max(22, label_y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA)
        for target, saved, base_color in (
            ("pot", self._pot_outline, (50, 220, 130)),
            ("thermometer", self._temperature_quad, (40, 190, 245)),
        ):
            editing = self._outline_mode and self._outline_target == target
            outline = self._outline_points if editing else saved
            if not outline:
                continue
            color = (0, 235, 255) if editing else base_color
            cv2.polylines(result, [np.asarray(outline, np.int32)], True, color, 2, cv2.LINE_AA)
            if editing:
                for x, y in outline:
                    cv2.circle(result, (int(x), int(y)), 12, color, -1, cv2.LINE_AA)
                    cv2.circle(result, (int(x), int(y)), 12, (0, 0, 0), 2, cv2.LINE_AA)
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

    def _toggle_session(self) -> None:
        if self._session_log.active:
            path = self._session_log.stop()
            self.session_button.configure(text="Iniciar sessão")
            self.metric_values["Sessão"].configure(text="Sessão encerrada")
            self._phase = "Aguardando sessão"
            self.metric_values["Estado do levain"].configure(text=self._phase)
            self._set_message(f"Registro salvo em {path}.")
            self._session_baseline = None
            return
        if self._measurement is None:
            empty = self._analyzer is not None and self._analyzer.empty_pot is not None
            self._set_message(
                "O pote está vazio; coloque o levain para iniciar a sessão." if empty
                else "Aguarde a detecção do pote para iniciar uma sessão."
            )
            return
        try:
            if self._mix_time is None:
                self._mix_time = datetime.now().astimezone()
            suggested_name = f"Levain {self._mix_time.strftime('%d/%m %H:%M')}"
            name = simpledialog.askstring(
                "Nome da sessão", "Dê um nome para esta sessão:",
                initialvalue=self._session_name or suggested_name, parent=self.root,
            )
            if name is None:
                return
            self._session_name = name.strip() or suggested_name
            session_id = self._session_log.start(self._mix_time, self._session_name)
            self._session_log.volume_offset = 0.0
            if self._analyzer is not None:
                self._analyzer.volume_offset = 0.0
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
        self.mix_time_button.configure(
            text=f"Mistura: {self._mix_time.strftime('%d/%m %H:%M')}"
        )
        self.metric_values["Sessão"].configure(
            text=self._session_summary()
        )
        self.metric_values["Estado do levain"].configure(text=self._phase)
        self._update_double_forecast(time.monotonic())
        self._record_session(time.monotonic(), force=True)
        self._set_message(f"Sessão ‘{self._session_name}’ ativa. Dados registrados em {self._session_log.path}.")

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
        self._refresh_session_graph()
        self._graph_window.after_idle(self._refresh_session_graph)

        def update_graph() -> None:
            if not self._graph_window.winfo_exists():
                return
            self._refresh_session_graph()
            self._graph_window.after(10_000, update_graph)

        self._graph_window.after(10_000, update_graph)

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

    def _refresh_session_graph(self) -> None:
        if getattr(self, "_graph_canvas", None) is None:
            return
        self._draw_session_graph()

    def _close_session_graph(self, _event: tk.Event | None = None) -> str:
        graph_window = getattr(self, "_graph_window", None)
        if graph_window is not None and graph_window.winfo_exists():
            graph_window.destroy()
        return "break"

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Registro da sessão", padding=12)
        box.pack(fill="x")
        ttk.Label(box, text="Gravar uma medição no CSV a cada").grid(row=0, column=0, sticky="w")
        self._interval_var = tk.StringVar(value=str(self._settings.record_interval_seconds))
        spinbox = ttk.Spinbox(
            box, from_=MIN_RECORD_SECONDS, to=MAX_RECORD_SECONDS, increment=5, width=5,
            textvariable=self._interval_var, command=self._apply_settings,
        )
        spinbox.grid(row=0, column=1, padx=6)
        spinbox.bind("<Return>", lambda _e: self._apply_settings(), add="+")
        spinbox.bind("<FocusOut>", lambda _e: self._apply_settings(), add="+")
        ttk.Label(box, text="segundos").grid(row=0, column=2, sticky="w")
        self._settings_note = ttk.Label(
            box, text=f"Entre {MIN_RECORD_SECONDS} e {MAX_RECORD_SECONDS} s. Vale já na sessão ativa.",
            foreground="#59645e",
        )
        self._settings_note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        video = ttk.LabelFrame(parent, text="Vídeo", padding=12)
        video.pack(fill="x", pady=(12, 0))
        ttk.Label(video, text="Brilho fora das regiões marcadas").grid(row=0, column=0, sticky="w")
        self._overlay_var = tk.IntVar(value=self._settings.overlay_brightness)
        self._overlay_value = ttk.Label(video, text=f"{self._settings.overlay_brightness}%", width=5)
        slider = ttk.Scale(
            video, from_=MIN_OVERLAY_BRIGHTNESS, to=MAX_OVERLAY_BRIGHTNESS, orient="horizontal", length=220,
            variable=self._overlay_var,
            command=lambda value: self._overlay_value.configure(text=f"{int(float(value))}%"),
        )
        slider.grid(row=0, column=1, padx=8)
        slider.bind("<ButtonRelease-1>", lambda _e: self._apply_settings(), add="+")
        slider.bind("<KeyRelease>", lambda _e: self._apply_settings(), add="+")
        self._overlay_value.grid(row=0, column=2, sticky="w")
        ttk.Label(
            video, foreground="#59645e",
            text=f"{MAX_OVERLAY_BRIGHTNESS}% não escurece nada. O escurecimento só aparece depois de marcar o pote ou o visor.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        phone = ttk.LabelFrame(parent, text="Celular (Google Sheets)", padding=12)
        phone.pack(fill="x", pady=(12, 0))
        self._phone_enabled_var = tk.BooleanVar(value=self._settings.phone_enabled)
        ttk.Checkbutton(
            phone, text="Enviar os dados para o celular",
            variable=self._phone_enabled_var, command=self._apply_settings,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(phone, text="Enviar um pacote a cada").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._phone_interval_var = tk.StringVar(value=str(self._settings.phone_interval_seconds))
        phone_spinbox = ttk.Spinbox(
            phone, from_=MIN_PHONE_SECONDS, to=MAX_PHONE_SECONDS, increment=30, width=5,
            textvariable=self._phone_interval_var, command=self._apply_settings,
        )
        phone_spinbox.grid(row=1, column=1, padx=6, pady=(8, 0))
        phone_spinbox.bind("<Return>", lambda _e: self._apply_settings(), add="+")
        phone_spinbox.bind("<FocusOut>", lambda _e: self._apply_settings(), add="+")
        ttk.Label(phone, text="segundos").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self._phone_snapshot_var = tk.BooleanVar(value=self._settings.phone_snapshot)
        ttk.Checkbutton(
            phone, text="Incluir uma imagem do pote em cada pacote",
            variable=self._phone_snapshot_var, command=self._apply_settings,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._phone_status = ttk.Label(phone, text="", foreground="#59645e", wraplength=560, justify="left")
        self._phone_status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(phone)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Copiar link do painel local", command=self._copy_local_link).pack(side="left")
        ttk.Button(buttons, text="Copiar link do painel online", command=self._copy_online_link).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="Limpar dados do celular", command=self._clear_phone_data).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            phone, foreground="#59645e", wraplength=560, justify="left",
            text="Precisa do phone.ini (modelo: phone.ini.example) e do script no Google Sheets. "
                 "O painel roda com: cd painel && python -m http.server 8000",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _apply_settings(self) -> None:
        try:
            seconds = int(float(self._interval_var.get()))
        except ValueError:
            seconds = self._settings.record_interval_seconds
        try:
            phone_seconds = int(float(self._phone_interval_var.get()))
        except ValueError:
            phone_seconds = self._settings.phone_interval_seconds
        # replace(), not a new Settings: the options this tab does not show (graph scales,
        # the display's black/white cuts) must survive a change here.
        self._settings = replace(
            self._settings,
            record_interval_seconds=seconds,
            phone_enabled=self._phone_enabled_var.get(),
            phone_interval_seconds=phone_seconds,
            phone_snapshot=self._phone_snapshot_var.get(),
            overlay_brightness=int(float(self._overlay_var.get())),
        ).clamped()
        self._overlay_var.set(self._settings.overlay_brightness)
        self._interval_var.set(str(self._settings.record_interval_seconds))
        self._phone_interval_var.set(str(self._settings.phone_interval_seconds))
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
            "previsao": self.metric_values["Previsão do dobro"].cget("text"),
            "perfil": None if self._analyzer is None else self._analyzer.profile.name,
            "status": None if self._stream is None else self._stream.status,
            "intervalo_s": self._settings.phone_interval_seconds,
        }

    def _phone_live(self) -> dict:
        return dict(self._live_state)

    def _phone_csv(self) -> Path | None:
        return self._session_log.path if self._session_log.active else None

    def _phone_chart_options(self) -> dict:
        """The scales chosen on the graph, so the phone shows the same axes."""
        return {
            "temperature_scale": self._settings.graph_temperature_scale,
            "volume_mode": self._settings.graph_volume_scale,
        }

    def _phone_snapshot(self) -> dict | None:
        stream, measurement = self._stream, self._measurement
        if stream is None or measurement is None:
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
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = getattr(self, "_graph_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        data = prepare_graph_data(path)
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
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        first_time = samples[0][0]
        duration = max(1.0, (samples[-1][0] - first_time).total_seconds())
        x_of = lambda stamp: left + (stamp - first_time).total_seconds() / duration * plot_width
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
            stamp = first_time + (samples[-1][0] - first_time) * fraction
            x = left + fraction * plot_width
            canvas.create_line(x, bottom, x, bottom + 5, fill="#59645e")
            canvas.create_text(
                x, bottom + 9, text=stamp.strftime("%d/%m\n%H:%M"),
                anchor="n", fill="#59645e", font=("TkDefaultFont", 8),
            )

        self._draw_phase_markers(canvas, samples, phases, x_of, top, bottom, plot_width, duration)

        step = max(1, len(samples) // 300)
        volume_points = []
        for stamp, volume, _ in samples[::step]:
            volume_points.extend((x_of(stamp), y_volume(volume)))
        if len(volume_points) >= 4:
            canvas.create_line(*volume_points, fill="#397a9b", width=3, smooth=True)

        previous = None
        for stamp, _, temperature in samples:
            if temperature is None:
                previous = None
                continue
            point = (x_of(stamp), y_temperature(temperature))
            if previous is not None:
                canvas.create_line(*previous, *point, fill="#c46b35", width=2)
            previous = point

        legend_y = 42
        canvas.create_line(left + 8, legend_y, left + 32, legend_y, fill="#397a9b", width=3)
        canvas.create_text(
            left + 38, legend_y, text="Volume estabilizado",
            anchor="w", fill="#202723",
        )
        canvas.create_line(right - 190, legend_y, right - 166, legend_y, fill="#c46b35", width=3)
        canvas.create_text(
            right - 160, legend_y,
            text="Temperatura" if temperatures else "Temperatura indisponível",
            anchor="w", fill="#202723",
        )

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

    def _resume_session(self) -> None:
        if self._session_log.active:
            self._set_message("Encerre a sessão ativa antes de retomar outra.")
            return
        selected = filedialog.askopenfilename(
            title="Retomar sessão", initialdir="data/sessions",
            filetypes=(("CSV", "*.csv"),), parent=self.root,
        )
        if selected:
            self._resume_from(Path(selected))

    def _resume_latest_session(self) -> None:
        latest = latest_session_csv(self._session_log.directory)
        if latest is None:
            self._set_message("Nenhuma sessão anterior para retomar.")
            return
        self._resume_from(latest, prefer_saved_regions=True)

    def _resume_from(self, selected: Path, prefer_saved_regions: bool = False) -> None:
        if self._session_log.active:
            self._set_message("Encerre a sessão ativa antes de retomar outra.")
            return
        try:
            session_id, regions = self._session_log.resume(selected)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._set_message(f"Não foi possível retomar o CSV: {exc}")
            return
        self._session_name = self._session_log.session_name
        self._manual_pot_roi = regions.get("pot")
        self._temperature_roi = regions.get("thermometer")
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
            self._analyzer.load_references()
        self._manual_marker_y = self._session_log.manual_marker_y
        # Resuming by itself at start-up (--resume): regions marked since that session was
        # last saved are newer than the ones in its file, so they win. Picking a session
        # by hand gives that session's own regions back.
        saved_file = Path("data/regions.json")
        session_file = selected.with_suffix(".json")
        newer_saved = (
            prefer_saved_regions and saved_file.exists() and session_file.exists()
            and saved_file.stat().st_mtime > session_file.stat().st_mtime
        )
        if newer_saved:
            self._load_saved_regions()
        else:
            self._persist_regions()
        self._mix_time = self._session_log.mix_time
        self._reset_tracking()
        self._session_baseline = None
        try:
            with selected.open("r", newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            first_time = datetime.fromisoformat(rows[0]["timestamp"])
            clock_origin = time.monotonic()
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
                elapsed = clock_origin + (stamp - first_time).total_seconds()
                self._phase = self._phase_tracker.update(elapsed, volume, baseline)
                temperature = row.get("temperature_c")
                if temperature:
                    last_temperature = float(temperature)
                self._forecaster.add(elapsed, volume, last_temperature)
            if last_temperature is not None:
                self._last_known_temperature = last_temperature
                if self._analyzer is not None:
                    self._analyzer.anchor_temperature = last_temperature
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
        self.metric_values["Sessão"].configure(
            text=self._session_summary(resumed=True)
        )
        self.metric_values["Estado do levain"].configure(text=self._phase)
        self.mix_time_button.configure(
            text="Hora da mistura" if self._mix_time is None else f"Mistura: {self._mix_time.strftime('%d/%m %H:%M')}"
        )
        self.auto_pot_button.configure(state="normal" if self._manual_pot_roi else "disabled")
        self._set_message(f"Sessão retomada; as próximas medições serão acrescentadas em {self._session_log.path}.")

    def _update_session(self, measurement: ContainerMeasurement | None) -> None:
        if not self._session_log.active or measurement is None:
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
        if not self._session_log.active:
            label.configure(text="Inicie uma sessão para estimar")
            return
        if self._forecaster.rate_per_thermal_minute() is None:
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
        if current >= target:
            label.configure(text="O dobro do volume inicial da mistura já foi alcançado")
            return
        forecast = self._forecaster.minutes_until(current, target)
        if forecast is None:
            label.configure(text="Sem crescimento sustentado para estimar")
            return
        minutes = forecast.minutes
        if forecast.uses_temperature:
            basis += f" + temp. ({forecast.model})"
        if minutes > 10080:
            label.configure(text="Projeção muito distante; aguardando mais crescimento")
            return
        predicted_at = now + timedelta(minutes=minutes)
        mix_elapsed = predicted_at - (self._mix_time or predicted_at)
        total_minutes = max(0, int(mix_elapsed.total_seconds() // 60))
        elapsed_days, remainder = divmod(total_minutes, 24 * 60)
        elapsed_hours, elapsed_minutes = divmod(remainder, 60)
        if elapsed_days:
            elapsed_text = f"{elapsed_days}d {elapsed_hours}h após mistura"
        else:
            elapsed_text = f"{elapsed_hours}h{elapsed_minutes:02d} após mistura"
        # Three short lines: when, how long after mixing, and what it rests on.
        label.configure(
            text=f"{predicted_at.strftime('%d/%m às %H:%M')}\n{elapsed_text}\n{basis}"
        )

    def _set_mix_time(self) -> None:
        default_time = self._mix_time or datetime.now().astimezone()
        entered = simpledialog.askstring(
            "Hora da mistura",
            "Informe data e hora da mistura (DD/MM/AAAA HH:MM):",
            initialvalue=default_time.strftime("%d/%m/%Y %H:%M"),
            parent=self.root,
        )
        if entered is None:
            return
        try:
            mix_time = datetime.strptime(entered.strip(), "%d/%m/%Y %H:%M").astimezone()
        except ValueError:
            self._set_message("Formato inválido. Use DD/MM/AAAA HH:MM, por exemplo 30/10/2026 08:15.")
            return
        if mix_time > datetime.now().astimezone():
            self._set_message("A hora da mistura não pode estar no futuro.")
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
        "--resume", "--retomar", dest="resume_last", action="store_true",
        help="retoma a sessão gravada mais recentemente em data/sessions",
    )
    arguments = parser.parse_args()
    root = tk.Tk()
    LevainPeakApp(root, resume_last=arguments.resume_last)
    root.mainloop()


if __name__ == "__main__":
    main()
