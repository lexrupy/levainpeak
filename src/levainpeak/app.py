"""Tk screen for viewing the live camera stream."""

from __future__ import annotations

import argparse
import tkinter as tk
import logging
import csv
import json
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from tkinter import filedialog, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from levainpeak.analysis import ContainerAnalyzer
from levainpeak.config import load_camera_config
from levainpeak.filters import despike, valid_after_recovery
from levainpeak.forecast import DoublingForecaster
from levainpeak.stream import RtspStream
from levainpeak.sessions import PhaseTracker, SessionLog, latest_session_csv
from levainpeak.settings import (
    MAX_RECORD_SECONDS,
    MIN_RECORD_SECONDS,
    Settings,
    load_settings,
    save_settings,
)
from levainpeak.vision import (
    ContainerMeasurement,
    draw_measurement,
    set_initial_marker,
    set_initial_volume_reference,
)

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
        self._marker_mode = False
        self._roi_mode = False
        self._roi_target = "pot"
        self._roi_drag_start: tuple[int, int] | None = None
        self._roi_preview_item: int | None = None
        self._manual_pot_roi: tuple[int, int, int, int] | None = None
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
        self._phase_tracker = PhaseTracker()
        self._forecaster = DoublingForecaster()
        self._phase = "Aguardando sessão"
        self._mix_time: datetime | None = None
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        try:
            self._stream = RtspStream(load_camera_config())
            self._stream.start()
            self._analyzer = ContainerAnalyzer(self._stream)
            self._analyzer.start()
            self._set_message("Iniciando conexão com a câmera…")
        except (FileNotFoundError, ValueError) as exc:
            self._set_message(str(exc))
            self.status_value.configure(text="Configuração necessária", foreground="#a43d32")

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
        self.video.bind("<B1-Motion>", self._pan_video, add="+")
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
            tile.bind(
                "<Configure>",
                lambda event, target=label: target.configure(
                    wraplength=max(120, event.width - 28)
                ),
                add="+",
            )
            self._metric_tiles.append(tile)
            self.metric_values[title] = label
        self._metrics_container = metrics
        for column in range(3):
            self._metrics_container.columnconfigure(column, weight=1, uniform="metrics")
        for index, tile in enumerate(self._metric_tiles):
            tile.grid(row=index // 3, column=index % 3, sticky="nsew", padx=4, pady=4)

        footer = ttk.Frame(monitor, padding=(20, 0, 20, 14))
        footer.pack(fill="x")
        self.instructions = ttk.Label(
            footer,
            text="Ative ‘Marcar início’, passe o mouse para pré-visualizar e clique para confirmar.",
            anchor="w", justify="left",
        )
        self.instructions.pack(fill="x", pady=(0, 2))
        self.message = ttk.Label(footer, text="", anchor="w", justify="left")
        self.message.pack(fill="x", pady=(0, 6))
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

        regions = group("Regiões", 0, 0)
        level = group("Nível inicial", 0, 1)
        session = group("Sessão", 1, 0)
        files = group("Arquivos", 1, 1)
        self.draw_pot_button = button(
            regions, "Marcar pote", lambda: self._toggle_roi_mode("pot"), width=13
        )
        self.draw_thermometer_button = button(
            regions, "Marcar termômetro", lambda: self._toggle_roi_mode("thermometer"), width=18
        )
        self.auto_pot_button = button(
            regions, "Detecção automática", self._use_automatic_detection, width=19,
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
        self.resume_button = button(files, "Retomar sessão", self._resume_session, width=14)
        self.open_csv_button = button(files, "Abrir CSV", self._open_session_csv, width=10)
        self.graph_button = button(files, "Gráfico", self._show_session_graph, width=9)

    def _wrap_footer_labels(self, event: tk.Event) -> None:
        wrap = max(200, event.width - 8)
        self.instructions.configure(wraplength=wrap)
        self.message.configure(wraplength=wrap)

    def _annotate(self, frame, measurement):
        return draw_measurement(frame, measurement, detail=self._show_detail)

    def _toggle_detail(self, event: tk.Event | None = None) -> None:
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox)):
            return  # typing a "d" in a field must not toggle the overlay
        self._show_detail = not self._show_detail
        self.detail_button.configure(
            text="Ocultar detecção" if self._show_detail else "Ver detecção"
        )
        self._redraw_current_frame()

    def _reset_tracking(self, baseline: float | None = None) -> None:
        self._phase_tracker.reset(baseline)
        self._forecaster.reset()

    def _set_message(self, message: str) -> None:
        self.message.configure(text=message)

    def _refresh(self) -> None:
        if self._stream is not None:
            status = self._stream.status
            self.status_value.configure(text=status)
            if status != self._last_stream_status:
                self._last_stream_status = status
                if self._measurement is None:
                    self._set_message(status)
            analysis_changed = False
            if self._analyzer is not None:
                result = self._analyzer.latest_result()
                if result is not None:
                    _, self._measurement, self._temperature_c = result
                    # An unread temperature repeats the last good reading.
                    self._temperature_filled = self._temperature_c is None
                    if self._temperature_c is not None:
                        self._last_known_temperature = self._temperature_c
                    else:
                        self._temperature_c = self._last_known_temperature
                    self._update_temperature_label()
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
            frame_number, frame = self._stream.snapshot()
            if frame is not None and (
                frame_number != self._last_displayed_frame_number or analysis_changed
            ):
                self._last_displayed_frame_number = frame_number
                shown = self._annotate(frame, self._measurement) if self._measurement else frame
                shown = self._apply_region_mask(shown)
                self._show_frame(shown, source_frame=frame)
        self.root.after(40, self._refresh)

    def _update_measurement_labels(self) -> None:
        if self._measurement is None:
            self.metric_values["Pote"].configure(text="Procurando recipiente…")
            self.metric_values["Volume relativo"].configure(text="—")
            self.metric_values["Estado do levain"].configure(text="Aguardando recipiente visível")
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
        if self._roi_mode and self._roi_target == target:
            self._cancel_roi_mode()
            return
        if self._marker_mode:
            self._cancel_marker_mode()
        self._roi_mode = True
        self._roi_target = target
        self._roi_drag_start = None
        self.draw_pot_button.configure(
            text="Cancelar desenho" if target == "pot" else "Marcar pote"
        )
        self.draw_thermometer_button.configure(
            text="Cancelar desenho" if target == "thermometer" else "Marcar termômetro"
        )
        self.video.configure(cursor="crosshair")
        target_text = "pote" if target == "pot" else "visor do termômetro"
        self._set_message(f"Arraste uma área um pouco maior que o {target_text}.")

    def _cancel_roi_mode(self) -> None:
        self._roi_mode = False
        self._roi_drag_start = None
        if hasattr(self, "draw_pot_button"):
            self.draw_pot_button.configure(text="Marcar pote")
        if hasattr(self, "draw_thermometer_button"):
            self.draw_thermometer_button.configure(text="Marcar termômetro")
        if hasattr(self, "video"):
            self.video.configure(cursor="")
        if self._roi_preview_item is not None:
            self.video.itemconfigure(self._roi_preview_item, state="hidden")

    def _start_roi_draw(self, event: tk.Event) -> None:
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
            if self._analyzer is not None:
                self._analyzer.roi = selected_roi
            self._manual_marker_y = None
            self._last_container_box = None
            self._measurement = None
            if self._session_log.active:
                self._session_baseline = None
                self._reset_tracking()
                self._session_log.save_regions(self._manual_pot_roi, self._temperature_roi)
            self.auto_pot_button.configure(state="normal")
            message = "Área de busca do pote definida."
        else:
            self._temperature_roi = selected_roi
            if self._analyzer is not None:
                self._analyzer.temperature_roi = selected_roi
            if self._session_log.active:
                self._session_log.save_regions(self._manual_pot_roi, self._temperature_roi)
            self._temperature_c = None
            self._update_temperature_label()
            message = "Área do visor definida; procurando a temperatura."
        self._cancel_roi_mode()
        self._set_message(message)

    def _use_automatic_detection(self) -> None:
        self._manual_pot_roi = None
        if self._analyzer is not None:
            self._analyzer.roi = None
        self._manual_marker_y = None
        self._last_container_box = None
        self._measurement = None
        self.auto_pot_button.configure(state="disabled")
        self.draw_pot_button.configure(text="Marcar pote")
        if self._session_log.active:
            self._session_baseline = None
            self._reset_tracking()
            self._session_log.save_regions(None, self._temperature_roi)
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
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        image.thumbnail((panel_width, panel_height), Image.Resampling.LANCZOS)
        base_size = image.size
        full_size = (max(1, round(image.width * self._zoom)), max(1, round(image.height * self._zoom)))
        if self._zoom != 1:
            image = image.resize(full_size, Image.Resampling.LANCZOS)
        visible = (min(panel_width, image.width), min(panel_height, image.height))
        crop_x = max(0, min(image.width - visible[0], round((image.width - visible[0]) * self._zoom_center[0])))
        crop_y = max(0, min(image.height - visible[1], round((image.height - visible[1]) * self._zoom_center[1])))
        image = image.crop((crop_x, crop_y, crop_x + visible[0], crop_y + visible[1]))
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
        if not regions:
            return frame
        height, width = frame.shape[:2]
        mask = np.zeros((height, width), dtype=np.uint8)
        for left, top, right, bottom in regions:
            mask[max(0, top):min(height, bottom), max(0, left):min(width, right)] = 255
        dim = (frame.astype(np.float32) * 0.24).astype(np.uint8)
        result = np.where(mask[:, :, None] > 0, frame, dim)
        for region, color, label in (
            (self._manual_pot_roi, (50, 220, 130), "POTE"),
            (self._temperature_roi, (40, 190, 245), "TERMÔMETRO"),
        ):
            if region:
                left, top, right, bottom = region
                cv2.rectangle(result, (left, top), (right, bottom), color, 2)
                cv2.putText(result, label, (left, max(22, top - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA)
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
            self._set_message("Aguarde a detecção do pote para iniciar uma sessão.")
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
            self._session_log.save_regions(self._manual_pot_roi, self._temperature_roi)
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
            text=f"{self._session_name}\nAtiva desde {session_id}\nMistura: {self._mix_time.strftime('%d/%m %H:%M')}"
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

    def _show_session_graph(self) -> None:
        path = self._session_log.path
        if path is None or not path.exists():
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
            self._refresh_session_graph()
            return

        self._graph_path = path
        self._graph_window = tk.Toplevel(self.root, class_="Pythontkfloating")
        self._graph_window.title(f"Evolução da sessão · {path.stem}")
        self._graph_window.geometry("900x520")
        self._graph_window.minsize(640, 400)
        self._graph_window.bind("<Escape>", self._close_session_graph)
        self._graph_window.configure(background="#c9c3b4")
        self._graph_canvas = tk.Canvas(
            self._graph_window, background="#fffefa", highlightthickness=0, cursor="fleur"
        )
        self._graph_canvas.pack(fill="both", expand=True, padx=GRAPH_BORDER, pady=GRAPH_BORDER)
        self._graph_canvas.bind("<ButtonPress-1>", self._graph_drag_start, add="+")
        self._graph_canvas.bind("<B1-Motion>", self._graph_drag_move, add="+")
        self._graph_window.bind("<Motion>", self._graph_border_hover, add="+")
        self._graph_window.bind("<ButtonPress-1>", self._graph_resize_start, add="+")
        self._graph_window.bind("<B1-Motion>", self._graph_resize_move, add="+")
        self._graph_canvas.bind("<Configure>", self._draw_session_graph, add="+")
        self._refresh_session_graph()
        self._graph_window.after_idle(self._refresh_session_graph)

        def update_graph() -> None:
            if not self._graph_window.winfo_exists():
                return
            self._refresh_session_graph()
            self._graph_window.after(10_000, update_graph)

        self._graph_window.after(10_000, update_graph)

    def _graph_drag_start(self, event: tk.Event) -> None:
        window = self._graph_window
        self._graph_drag_origin = (
            event.x_root - window.winfo_x(), event.y_root - window.winfo_y()
        )

    def _graph_drag_move(self, event: tk.Event) -> None:
        offset_x, offset_y = self._graph_drag_origin
        self._graph_window.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _graph_edges(self, event: tk.Event) -> str:
        """Edges ("n", "s", "w", "e" combined) under the pointer in the border."""
        window = self._graph_window
        edges = ""
        if event.y < GRAPH_BORDER:
            edges += "n"
        elif event.y > window.winfo_height() - GRAPH_BORDER:
            edges += "s"
        if event.x < GRAPH_BORDER:
            edges += "w"
        elif event.x > window.winfo_width() - GRAPH_BORDER:
            edges += "e"
        return edges

    def _graph_border_hover(self, event: tk.Event) -> None:
        if event.widget is not self._graph_window:
            return
        cursors = {
            "n": "top_side", "s": "bottom_side", "w": "left_side", "e": "right_side",
            "nw": "top_left_corner", "ne": "top_right_corner",
            "sw": "bottom_left_corner", "se": "bottom_right_corner",
        }
        self._graph_window.configure(cursor=cursors.get(self._graph_edges(event), ""))

    def _graph_resize_start(self, event: tk.Event) -> None:
        if event.widget is not self._graph_window:
            return
        window = self._graph_window
        self._graph_resize = (
            self._graph_edges(event), event.x_root, event.y_root,
            window.winfo_x(), window.winfo_y(), window.winfo_width(), window.winfo_height(),
        )

    def _graph_resize_move(self, event: tk.Event) -> None:
        resize = getattr(self, "_graph_resize", None)
        if event.widget is not self._graph_window or not resize or not resize[0]:
            return
        edges, start_x, start_y, x, y, width, height = resize
        min_width, min_height = self._graph_window.minsize()
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
        self._graph_window.geometry(f"{width}x{height}+{x}+{y}")

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

    def _apply_settings(self) -> None:
        try:
            seconds = int(float(self._interval_var.get()))
        except ValueError:
            seconds = self._settings.record_interval_seconds
        self._settings = Settings(record_interval_seconds=seconds).clamped()
        self._interval_var.set(str(self._settings.record_interval_seconds))
        self._session_log.sample_seconds = self._settings.record_interval_seconds
        try:
            save_settings(self._settings)
        except OSError as exc:
            self._set_message(f"Não foi possível salvar as configurações: {exc}")

    def _draw_session_graph(self, _event: tk.Event | None = None) -> None:
        canvas = getattr(self, "_graph_canvas", None)
        path = getattr(self, "_graph_path", None)
        if canvas is None or path is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        samples = []
        phases: list[str] = []
        last_temperature: float | None = None
        baseline = None
        session_name = path.stem
        try:
            with path.open("r", newline="", encoding="utf-8") as source:
                for row in csv.DictReader(source):
                    timestamp = datetime.fromisoformat(row["timestamp"])
                    volume = float(row["volume_fraction"])
                    if row.get("initial_volume_fraction"):
                        baseline = float(row["initial_volume_fraction"])
                    raw_temperature = row.get("temperature_c", "")
                    # An unread temperature repeats the last good reading.
                    temperature = float(raw_temperature) if raw_temperature else last_temperature
                    last_temperature = temperature
                    samples.append((timestamp, volume, temperature))
                    phases.append(row.get("phase", ""))
                    if row.get("session_name"):
                        session_name = row["session_name"]
        except (OSError, KeyError, ValueError, csv.Error) as exc:
            canvas.create_text(
                24, 24, anchor="nw", text=f"Não foi possível ler o CSV: {exc}",
                fill="#8f3028", font=("TkDefaultFont", 11),
            )
            return

        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        canvas.create_text(
            width / 2, 18, text=f"Evolução · {session_name}",
            fill="#202723", font=("TkDefaultFont", 13, "bold"),
        )
        if len(samples) < 2:
            canvas.create_text(
                width / 2, height / 2,
                text="O gráfico aparecerá após algumas medições da sessão.",
                fill="#59645e", font=("TkDefaultFont", 11),
            )
            return

        # Drop dips and peaks that leave the trend and come back (for example the
        # surface being read at the dark band for a few minutes).
        cleaned = despike([sample[1] for sample in samples])
        # A later reading that rises above an earlier level proves the readings
        # under that level in between were wrong: rebuild them from the valid
        # neighbours.
        valid = valid_after_recovery(cleaned)
        good = [index for index, ok in enumerate(valid) if ok]
        if len(good) >= 2:
            cleaned = [
                value if ok else float(np.interp(index, good, [cleaned[i] for i in good]))
                for index, (value, ok) in enumerate(zip(cleaned, valid, strict=True))
            ]
        # Phases are recomputed from the cleaned readings, so the shading does
        # not inherit labels that were produced while a wrong reading was live.
        graph_tracker = PhaseTracker()
        graph_tracker.reset(baseline if baseline and baseline > 0 else cleaned[0])
        origin = samples[0][0]
        phases = [
            graph_tracker.update((stamp - origin).total_seconds(), volume)
            for (stamp, _, _), volume in zip(samples, cleaned, strict=True)
        ]
        raw_samples = [
            (stamp, volume, temperature)
            for (stamp, _, temperature), volume in zip(samples, cleaned, strict=True)
        ]
        samples = []
        smoothed_volume = None
        smoothed_temperature = None
        previous_timestamp = None
        for index, (stamp, _volume, _temperature) in enumerate(raw_samples):
            window = raw_samples[max(0, index - 4):min(len(raw_samples), index + 5)]
            median_volume = float(median(sample[1] for sample in window))
            nearby_temperatures = [sample[2] for sample in window if sample[2] is not None]
            median_temperature = float(median(nearby_temperatures)) if nearby_temperatures else None
            if smoothed_volume is None:
                smoothed_volume = median_volume
            else:
                elapsed_minutes = max(
                    1 / 60,
                    (stamp - previous_timestamp).total_seconds() / 60,
                )
                # A fixed pot cannot gain or lose a large fraction of its
                # capacity in seconds; cap graph-only spikes at 1.5%/minute.
                maximum_step = 0.015 * elapsed_minutes
                smoothed_volume += min(
                    maximum_step, max(-maximum_step, median_volume - smoothed_volume)
                )
            if median_temperature is None:
                smoothed_temperature = None
            elif smoothed_temperature is None:
                smoothed_temperature = median_temperature
            else:
                smoothed_temperature += 0.3 * (median_temperature - smoothed_temperature)
            samples.append((stamp, smoothed_volume, smoothed_temperature))
            previous_timestamp = stamp

        left, right, top, bottom = 72, width - 76, 82, height - 58
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        first_time = samples[0][0]
        duration = max(1.0, (samples[-1][0] - first_time).total_seconds())
        x_of = lambda stamp: left + (stamp - first_time).total_seconds() / duration * plot_width
        # 0% is the initial volume. 100% is the doubling of it until that is
        # reached; once the volume goes past it, 100% is the session's peak.
        baseline = baseline if baseline and baseline > 0 else samples[0][1]
        peak = max(volume for _, volume, _ in samples)
        double_target = 2 * baseline
        if double_target > 1.0:
            full_scale, scale_label = 1.0, "capacidade"
        elif peak >= double_target:
            full_scale, scale_label = peak, "máximo atingido"
        else:
            full_scale, scale_label = double_target, "dobro"
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
            low = min(temperatures)
            high = max(temperatures)
            if high - low < 1.0:
                center = (high + low) / 2
                low, high = center - 0.5, center + 0.5
            else:
                margin = (high - low) * 0.08
                low, high = low - margin, high + margin
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
                canvas.create_text(right + 10, y, text=f"{value:.1f}°", anchor="w", fill="#c46b35")
        canvas.create_line(left, top, left, bottom, fill="#397a9b", width=2)
        canvas.create_line(right, top, right, bottom, fill="#c46b35", width=2)
        canvas.create_line(left, bottom, right, bottom, fill="#59645e", width=2)
        canvas.create_text(left, top - 8, text=f"Volume (% do {scale_label}; 0% = início)", anchor="sw", fill="#397a9b")
        canvas.create_text(right, top - 8, text="Temperatura (°C)", anchor="se", fill="#c46b35")

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

    # The graph names the classic growth-curve phases; the CSV and the Monitor
    # keep the finer tracker states (for example "Pico provável").
    _GRAPH_PHASES = {
        PhaseTracker.WAITING: "Latência",
        PhaseTracker.RISING: "Exponencial",
        PhaseTracker.PLATEAU: "Estacionária",
        PhaseTracker.PEAK: "Estacionária",
        PhaseTracker.FALL_CHECK: "Estacionária",
        PhaseTracker.FALLING: "Decaimento",
    }
    _GRAPH_PHASE_FILL = {
        "Latência": "#f2f0e8",
        "Exponencial": "#e6f0e8",
        "Estacionária": "#f7efd9",
        "Decaimento": "#f3e3de",
    }

    def _draw_phase_markers(
        self, canvas, samples, phases, x_of, top, bottom, plot_width, duration
    ) -> None:
        """Shaded bands and dashed dividers for the growth-curve phases."""
        if len(phases) != len(samples):
            return
        minimum_seconds = duration * 0.03  # ignore short flickers between phases
        segments: list[list] = []  # [phase, start_stamp, end_stamp]
        for stamp, raw in zip((sample[0] for sample in samples), phases, strict=True):
            phase = self._GRAPH_PHASES.get(raw)
            if phase is None:
                continue
            if segments and segments[-1][0] == phase:
                segments[-1][2] = stamp
            elif segments and (segments[-1][2] - segments[-1][1]).total_seconds() < minimum_seconds:
                segments[-1][0], segments[-1][2] = phase, stamp
            else:
                segments.append([phase, stamp, stamp])
        for index, (phase, start, end) in enumerate(segments):
            left_x = x_of(start) if index else x_of(samples[0][0])
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
        self._resume_from(latest)

    def _resume_from(self, selected: Path) -> None:
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
        if self._analyzer is not None:
            self._analyzer.roi = self._manual_pot_roi
            self._analyzer.temperature_roi = self._temperature_roi
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
                elapsed = clock_origin + (stamp - first_time).total_seconds()
                self._phase = self._phase_tracker.update(elapsed, volume, baseline)
                temperature = row.get("temperature_c")
                if temperature:
                    last_temperature = float(temperature)
                self._forecaster.add(elapsed, volume, last_temperature)
            if last_temperature is not None:
                self._last_known_temperature = last_temperature
        except (KeyError, ValueError, IndexError):
            self._reset_tracking()
            self._session_baseline = None
        self.session_button.configure(text="Encerrar sessão")
        self.metric_values["Sessão"].configure(
            text=f"{self._session_name or session_id}\nRetomada: {session_id}"
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
            "nível inicial marcado"
            if self._measurement.marker_y is not None
            else "primeira leitura"
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
            basis += f" + temperatura ({forecast.model})"
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
        label.configure(
            text=(
                f"{predicted_at.strftime('%d/%m às %H:%M')} · {basis}"
                f" ({elapsed_text})"
            )
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
            self.metric_values["Sessão"].configure(
                text=f"Ativa desde {self._session_log.session_id}\nMistura: {mix_time.strftime('%d/%m %H:%M')}"
            )
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
