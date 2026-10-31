"""Live diagnostics: one mosaic with the stages of every detection."""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from functools import lru_cache
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

from levainpeak.floating import BORDER, FloatingWindowControls
from levainpeak.temperature import inspect_temperature
from levainpeak.vision import ContainerMeasurement, column_scan, measure_container

PANEL_W, PANEL_H, TITLE_H, GAP = 230, 380, 22, 8
SECOND_ROW_H, INFO_H = 240, 114
WIDTH = 4 * PANEL_W + 3 * GAP
HEIGHT = TITLE_H + PANEL_H + GAP + TITLE_H + SECOND_ROW_H + GAP + INFO_H

BACKGROUND = (38, 41, 40)
TITLE_BAR = (62, 68, 66)
GRID = (74, 80, 78)
YELLOW, ORANGE, CYAN, GREEN, BLUE, RED = (40, 210, 255), (60, 140, 255), (255, 220, 60), (110, 200, 90), (255, 160, 50), (70, 70, 235)
WHITE, MUTED = (240, 240, 235), (170, 176, 172)
AUTO_MARKER = (150, 110, 70)  # the dark band the detector found, when the mark was set by hand
_FONT_PATHS = ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

Text = tuple[int, int, str, tuple[int, int, int], int]  # x, y, text, BGR colour, size


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def marker_rows(raw: ContainerMeasurement, stable: ContainerMeasurement | None) -> tuple[int | None, int | None, bool]:
    """(row the app uses, row the detector found, whether the used one was set by hand).

    The raw per-frame detection always sits on the dark band; the mark the user
    placed lives on the measurement the app settled on, and is the one that counts.
    """
    if stable is not None and stable.marker_y is not None:
        return stable.marker_y, raw.marker_y, bool(stable.marker_manual)
    return raw.marker_y, raw.marker_y, False


def draw_markers(view: "_View", raw: ContainerMeasurement, stable: ContainerMeasurement | None) -> None:
    used, found, manual = marker_rows(raw, stable)
    if manual and found is not None and used is not None and abs(found - used) > 1:
        view.hline(found, AUTO_MARKER, dashed=True)  # where the detector would have put it
    if used is not None:
        view.hline(used, BLUE, dashed=True, thickness=2 if manual else 1)


class _View:
    """A panel canvas plus the mapping from frame pixels to panel pixels."""

    def __init__(
        self,
        image: np.ndarray | None,
        origin: tuple[int, int],
        width: int,
        height: int,
        background: tuple[int, int, int] = BACKGROUND,
    ):
        self.canvas = np.full((height, width, 3), background, np.uint8)
        self.x0, self.y0 = origin
        self.scale, self.ox, self.oy = 1.0, 0, 0
        if image is not None and image.size:
            source_h, source_w = image.shape[:2]
            self.scale = min(width / source_w, height / source_h)
            new_w, new_h = max(1, int(source_w * self.scale)), max(1, int(source_h * self.scale))
            resized = cv2.resize(
                image, (new_w, new_h),
                interpolation=cv2.INTER_AREA if self.scale < 1 else cv2.INTER_NEAREST,
            )
            if resized.ndim == 2:
                resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
            self.ox, self.oy = (width - new_w) // 2, (height - new_h) // 2
            self.canvas[self.oy:self.oy + new_h, self.ox:self.ox + new_w] = resized

    def pt(self, x: float, y: float) -> tuple[int, int]:
        return (
            int(round(self.ox + (x - self.x0) * self.scale)),
            int(round(self.oy + (y - self.y0) * self.scale)),
        )

    def hline(self, y: float, colour, *, dashed: bool = False, thickness: int = 1) -> None:
        row = self.pt(0, y)[1]
        if not 0 <= row < self.canvas.shape[0]:
            return
        step = 10 if dashed else self.canvas.shape[1]
        for x in range(0, self.canvas.shape[1], step):
            end = min(x + (5 if dashed else step), self.canvas.shape[1] - 1)
            cv2.line(self.canvas, (x, row), (end, row), colour, thickness)

    def polyline(self, points: list[tuple[int, int]], colour, thickness: int = 1) -> None:
        if len(points) >= 2:
            cv2.polylines(self.canvas, [np.asarray(points, np.int32).reshape(-1, 1, 2)], False, colour, thickness, cv2.LINE_AA)


def _crop_box(frame: np.ndarray, raw: ContainerMeasurement | None, pot_roi) -> tuple[int, int, int, int]:
    height, width = frame.shape[:2]
    if raw is not None:
        left, top, right, bottom = raw.left - 30, raw.top - 20, raw.right + 30, raw.bottom + 40
    elif pot_roi is not None:
        left, top, right, bottom = pot_roi
    else:
        left, top, right, bottom = 0, 0, width, height
    left, right = int(np.clip(left, 0, width - 1)), int(np.clip(right, 1, width))
    top, bottom = int(np.clip(top, 0, height - 1)), int(np.clip(bottom, 1, height))
    if right - left < 20 or bottom - top < 20:
        return 0, 0, width, height
    return left, top, right, bottom


def build_debug_mosaic(
    frame: np.ndarray | None,
    pot_roi: tuple[int, int, int, int] | None,
    thermometer_roi: tuple[int, int, int, int] | None,
    *,
    stable: ContainerMeasurement | None = None,
    temperature_c: float | None = None,
    profile=None,
    vertical_bounds: tuple[int, int] | None = None,
    outline: tuple[tuple[int, int], ...] | None = None,
    face_quad: tuple[tuple[int, int], ...] | None = None,
    ink_cut: float | None = None,
) -> np.ndarray:
    """One BGR image with the stages of the pot, surface and thermometer reads.

    The pot panels show the raw per-frame detection; the orange line is what
    the app settled on after smoothing and outlier rejection.
    """
    mosaic = np.full((HEIGHT, WIDTH, 3), BACKGROUND, np.uint8)
    texts: list[Text] = []
    row2_y = TITLE_H + PANEL_H + GAP
    info_y = row2_y + TITLE_H + SECOND_ROW_H + GAP

    def place(view_canvas: np.ndarray, x: int, y: int, title: str) -> None:
        height, width = view_canvas.shape[:2]
        mosaic[y:y + TITLE_H, x:x + width] = TITLE_BAR
        mosaic[y + TITLE_H:y + TITLE_H + height, x:x + width] = view_canvas
        texts.append((x + 6, y + 3, title, WHITE, 12))

    raw = None
    if frame is not None:
        try:
            raw = measure_container(frame, roi=pot_roi, vertical_bounds=vertical_bounds, outline=outline)
        except cv2.error:
            raw = None
    scan = None
    if frame is not None and raw is not None and raw.marker_y is not None:
        scan = column_scan(frame, raw.left, raw.right, raw.top, raw.bottom, raw.marker_y)

    if frame is None:
        place(_View(None, (0, 0), PANEL_W, PANEL_H).canvas, 0, 0, "Sem imagem da câmera")
    else:
        left, top, right, bottom = _crop_box(frame, raw, pot_roi)
        crop = frame[top:bottom, left:right]
        origin = (left, top)
        views = [_View(crop, origin, PANEL_W, PANEL_H) for _ in range(3)]
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)[:, :, 0]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gradient = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
        gradient = np.clip(gradient / max(1.0, float(np.percentile(gradient, 99))) * 255, 0, 255).astype(np.uint8)
        light_view = _View(lab, origin, PANEL_W, PANEL_H)
        edge_view = _View(gradient, origin, PANEL_W, PANEL_H)
        detection_view = views[0]
        column_view = _View(None, origin, PANEL_W, PANEL_H)
        column_view.scale, column_view.ox, column_view.oy = detection_view.scale, detection_view.ox, detection_view.oy

        if raw is not None:
            _draw_detection(detection_view, raw, stable)
            _draw_light(light_view, raw, scan, lab, left, top, stable)
            _draw_edges(edge_view, raw)
            _draw_columns(column_view, raw, scan, stable, lab, left, top)
        for view, title in (
            (detection_view, "1 · Detecção"),
            (light_view, "2 · Luminosidade (L)"),
            (edge_view, "3 · Bordas (gradiente X)"),
            (column_view, "4 · Topo por coluna"),
        ):
            index = (detection_view, light_view, edge_view, column_view).index(view)
            place(view.canvas, index * (PANEL_W + GAP), 0, title)

    width_panel = _profile_panel(raw, stable, texts, row2_y)
    place(width_panel, 0, row2_y, "5 · Largura e volume por linha")
    inspection = (
        inspect_temperature(
            frame, thermometer_roi, None if profile is None else profile.infrared, face_quad, ink_cut
        )
        if frame is not None and thermometer_roi is not None
        else None
    )
    thermo = _thermometer_panel(inspection, thermometer_roi, texts, row2_y)
    place(thermo, PANEL_W + GAP, row2_y, "6 · Termômetro: recorte · face · binário · dígitos")

    _info_strip(texts, info_y, raw, scan, stable, temperature_c, thermometer_roi, frame, inspection, profile, vertical_bounds)
    return _render_texts(mosaic, texts)


def _draw_detection(view: _View, raw: ContainerMeasurement, stable: ContainerMeasurement | None) -> None:
    canvas = view.canvas
    walls = None
    if raw.edge_profile:
        edges = np.asarray(raw.edge_profile, np.float32)
        rows = raw.top + np.arange(len(edges))
        left_wall = [view.pt(x, y) for x, y in zip(edges[:, 0], rows)]
        right_wall = [view.pt(x, y) for x, y in zip(edges[:, 1], rows)]
        walls = (left_wall, right_wall)
        below = rows >= raw.surface_y
        if below.any():
            region = np.asarray(
                [p for p, keep in zip(left_wall, below) if keep]
                + [p for p, keep in zip(right_wall, below) if keep][::-1], np.int32
            )
            shade = canvas.copy()
            cv2.fillPoly(shade, [region], (255, 150, 60))
            cv2.addWeighted(shade, 0.35, canvas, 0.65, 0, dst=canvas)
        view.polyline(walls[0], CYAN, 1)
        view.polyline(walls[1], CYAN, 1)
    cv2.rectangle(canvas, view.pt(raw.left, raw.top), view.pt(raw.right, raw.bottom), GREEN, 1)
    view.hline(raw.surface_y, YELLOW, thickness=2)
    if stable is not None:
        view.hline(stable.surface_y, ORANGE, thickness=1, dashed=True)
    draw_markers(view, raw, stable)


def _draw_light(view: _View, raw, scan, lab: np.ndarray, left: int, top: int, stable=None) -> None:
    canvas = view.canvas
    if scan is not None:
        mask = np.zeros(lab.shape, np.uint8)
        mask[lab >= scan.threshold] = 255
        mask_view = _View(mask, (left, top), canvas.shape[1], canvas.shape[0], background=(0, 0, 0))
        pale = mask_view.canvas[:, :, 0] > 0
        tint = canvas.copy()
        tint[pale] = (80, 200, 90)
        cv2.addWeighted(tint, 0.30, canvas, 0.70, 0, dst=canvas)
        for x, y in scan.tops:
            cv2.circle(canvas, view.pt(x, y), 2, RED, -1)
    view.hline(raw.surface_y, YELLOW, thickness=2)
    draw_markers(view, raw, stable)


def _draw_edges(view: _View, raw: ContainerMeasurement) -> None:
    cv2.rectangle(view.canvas, view.pt(raw.left, raw.top), view.pt(raw.right, raw.bottom), GREEN, 1)
    if raw.edge_profile:
        edges = np.asarray(raw.edge_profile, np.float32)
        rows = raw.top + np.arange(len(edges))
        view.polyline([view.pt(x, y) for x, y in zip(edges[:, 0], rows)], CYAN, 2)
        view.polyline([view.pt(x, y) for x, y in zip(edges[:, 1], rows)], CYAN, 2)
    view.hline(raw.surface_y, YELLOW, thickness=1, dashed=True)


def _draw_columns(view: _View, raw, scan, stable, lab: np.ndarray, left: int, top: int) -> None:
    canvas = view.canvas
    width = canvas.shape[1]
    # Lightness of each row (median over the vessel interior) with the threshold.
    inner_left = raw.left + int((raw.right - raw.left) * 0.10) - left
    inner_right = raw.right - int((raw.right - raw.left) * 0.10) - left
    if inner_right > inner_left:
        rows = np.arange(lab.shape[0])
        levels = np.median(lab[:, inner_left:inner_right], axis=1)
        points = [(int(6 + level / 255 * (width - 12)), view.pt(0, top + y)[1]) for y, level in zip(rows, levels)]
        view.polyline(points, MUTED, 1)
        if scan is not None:
            x = int(6 + scan.threshold / 255 * (width - 12))
            cv2.line(canvas, (x, 0), (x, canvas.shape[0] - 1), GREEN, 1)
    if scan is not None:
        spread = [x for x, _ in scan.tops]
        low, high = min(spread), max(spread)
        for x, y in scan.tops:
            px = int(30 + (x - low) / max(1, high - low) * (width - 60))
            cv2.circle(canvas, (px, view.pt(0, y)[1]), 2, RED, -1)
    view.hline(raw.surface_y, YELLOW, thickness=2)
    if stable is not None:
        view.hline(stable.surface_y, ORANGE, dashed=True)
    draw_markers(view, raw, stable)


def _profile_panel(raw, stable, texts: list[Text], row2_y: int) -> np.ndarray:
    view = _View(None, (0, 0), PANEL_W, SECOND_ROW_H)
    canvas = view.canvas
    if raw is None or not raw.edge_profile:
        texts.append((8, row2_y + TITLE_H + 8, "sem perfil (pote não detectado)", MUTED, 11))
        return canvas
    edges = np.asarray(raw.edge_profile, np.float32)
    widths = edges[:, 1] - edges[:, 0]
    count = len(widths)
    ys = np.linspace(6, SECOND_ROW_H - 6, count).astype(int)
    xs_width = (6 + widths / max(1.0, widths.max()) * (PANEL_W - 12)).astype(int)
    view.polyline(list(zip(xs_width.tolist(), ys.tolist())), CYAN, 1)
    if raw.volume_below_profile:
        below = np.asarray(raw.volume_below_profile, np.float32)
        ys_volume = np.linspace(6, SECOND_ROW_H - 6, len(below)).astype(int)
        xs_volume = (6 + below * (PANEL_W - 12)).astype(int)
        view.polyline(list(zip(xs_volume.tolist(), ys_volume.tolist())), ORANGE, 1)

    def row_of(y: int) -> int:
        return int(6 + np.clip((y - raw.top) / max(1, raw.bottom - raw.top), 0, 1) * (SECOND_ROW_H - 12))

    cv2.line(canvas, (0, row_of(raw.surface_y)), (PANEL_W, row_of(raw.surface_y)), YELLOW, 1)
    texts.append((PANEL_W - 70, row_of(raw.surface_y) + TITLE_H + row2_y - TITLE_H - 14, f"{raw.volume_fraction:.0%}", YELLOW, 11))
    used, found, manual = marker_rows(raw, stable)
    if manual and found is not None and used is not None and abs(found - used) > 1:
        cv2.line(canvas, (0, row_of(found)), (PANEL_W, row_of(found)), AUTO_MARKER, 1)
    if used is not None:
        cv2.line(canvas, (0, row_of(used)), (PANEL_W, row_of(used)), BLUE, 2 if manual else 1)
    texts.append((6, row2_y + TITLE_H + 2, "ciano: largura · laranja: volume abaixo da linha", YELLOW, 9))
    return canvas


def _thermometer_panel(inspection, roi, texts: list[Text], row2_y: int) -> np.ndarray:
    width = 3 * PANEL_W + 2 * GAP
    canvas = np.full((SECOND_ROW_H, width, 3), BACKGROUND, np.uint8)
    base_x, base_y = PANEL_W + GAP, row2_y + TITLE_H
    if inspection is None or roi is None:
        texts.append((base_x + 8, base_y + 8, "marque a região do termômetro", MUTED, 11))
        return canvas
    if inspection.roi_image is not None:
        shown = _View(inspection.roi_image, (0, 0), PANEL_W - 8, 118)
        canvas[0:118, 0:PANEL_W - 8] = shown.canvas
    if inspection.face is not None:
        shown = _View(inspection.face, (0, 0), PANEL_W - 8, 112)
        canvas[124:236, 0:PANEL_W - 8] = shown.canvas
    column_x = PANEL_W + 4
    if inspection.binary is not None:
        view = _View(inspection.binary, (0, 0), PANEL_W - 8, 118)
        for left, top, right, bottom in inspection.cell_boxes:
            cv2.rectangle(view.canvas, view.pt(left, top), view.pt(right, bottom), GREEN, 1)
        canvas[0:118, column_x:column_x + PANEL_W - 8] = view.canvas
    texts.append((base_x + 4, base_y + 2, "recorte", YELLOW, 9))
    texts.append((base_x + 4, base_y + 126, "face do visor", YELLOW, 9))
    texts.append((base_x + column_x + 2, base_y + 2, "binário desinclinado + células", (20, 20, 20), 9))
    cells_x = 2 * PANEL_W + 8
    for index, cell in enumerate(inspection.cells):
        x0 = cells_x + index * 72
        view = _View(cell, (0, 0), 66, 100)
        canvas[0:100, x0:x0 + 66] = view.canvas
        digit = inspection.digits[index]
        texts.append((base_x + x0 + 22, base_y + 104, "?" if digit is None else str(digit), GREEN if digit is not None else RED, 24))
        for segment, value in enumerate(inspection.occupancy[index]):
            bar_x = x0 + 2 + segment * 9
            bar_h = int(value * 60)
            colour = GREEN if value >= 0.45 else (110, 116, 112)
            cv2.rectangle(canvas, (bar_x, 192 - bar_h), (bar_x + 6, 192), colour, -1)
            texts.append((base_x + bar_x - 1, base_y + 195, "abcdefg"[segment], MUTED, 8))
        cv2.line(canvas, (x0, 192 - int(0.45 * 60)), (x0 + 64, 192 - int(0.45 * 60)), YELLOW, 1)
    if not inspection.cells:
        texts.append((base_x + cells_x, base_y + 40, "dígitos não localizados", RED, 11))
    return canvas


def _info_strip(texts: list[Text], y: int, raw, scan, stable, temperature_c, thermometer_roi, frame, inspection=None, profile=None, vertical_bounds=None) -> None:
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if profile is not None:
        imposed = (
            "topo e fundo detectados pela imagem"
            if vertical_bounds is None
            else f"topo e fundo do pote fixados em y={vertical_bounds[0]}–{vertical_bounds[1]} (medidos de dia)"
        )
        lines.append((f"Perfil de captura: {profile.name} · {imposed}", YELLOW))
    if frame is None:
        lines.append(("Sem imagem da câmera.", RED))
    elif raw is None:
        lines.append(("Pote: não detectado neste quadro.", RED))
    elif raw.empty:
        lines.append(("Pote vazio: o contorno está marcado e não há levain dentro dele.", YELLOW))
    else:
        used, found, manual = marker_rows(raw, stable)
        marker_text = f"{used} (manual; faixa detectada em {found})" if manual else f"{used} (faixa detectada)"
        method = "colunas (claridade)" if scan is not None else "bordas (reserva)"
        lines.append((
            f"Pote: caixa {raw.left}-{raw.right} × {raw.top}-{raw.bottom}   marcador y={marker_text}   "
            f"superfície (bruta) y={raw.surface_y}   método: {method}",
            WHITE,
        ))
        extra = f"   luz: levain {scan.levain:.0f} · vidro {scan.glass:.0f} · limiar {scan.threshold:.0f}   colunas: {len(scan.tops)}/{len(scan.columns)}" if scan else ""
        lines.append((f"Volume bruto {raw.volume_fraction:.1%}   confiança {raw.confidence:.2f}{extra}", WHITE))
        if stable is not None:
            lines.append((
                f"Estável (usado pelo app): superfície y={stable.surface_y}   volume {stable.volume_fraction:.1%}"
                + (f"   ×{stable.growth_factor:.2f} do início" if stable.growth_factor else ""),
                ORANGE,
            ))
    reading = None if inspection is None else inspection.reading
    lines.append((
        "Termômetro: "
        + ("região não marcada" if thermometer_roi is None else f"leitura deste quadro {reading if reading is not None else 'ilegível'}")
        + ("" if temperature_c is None else f"   confirmada pelo app {temperature_c:.1f} °C"),
        WHITE,
    ))
    lines.append(("Legenda: amarelo = superfície bruta · laranja tracejado = estável · azul = marca inicial usada · azul fraco = faixa detectada · ciano = parede", MUTED))
    for number, (text, colour) in enumerate(lines):
        texts.append((6, y + 4 + number * 17, text, colour, 12))


def _render_texts(mosaic: np.ndarray, texts: list[Text]) -> np.ndarray:
    image = Image.fromarray(cv2.cvtColor(mosaic, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    for x, y, text, colour, size in texts:
        draw.text((x, y), text, font=_font(size), fill=(colour[2], colour[1], colour[0]))
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def _diagnostics_worker(
    get_state: Callable[[], dict], frames: "queue.Queue[np.ndarray]", stop: threading.Event
) -> None:
    """Rebuild the mosaic until told to stop.

    A plain function on purpose: if the thread held the window, the last
    reference to its Tk image could be dropped here, and Tk objects must only be
    destroyed on the thread that made them.
    """
    while not stop.is_set():
        started = time.perf_counter()
        try:
            state = get_state()
            mosaic = build_debug_mosaic(
                state.get("frame"), state.get("pot_roi"), state.get("thermometer_roi"),
                stable=state.get("stable"), temperature_c=state.get("temperature_c"),
                profile=state.get("profile"), vertical_bounds=state.get("vertical_bounds"),
                outline=state.get("outline"), face_quad=state.get("face_quad"),
                ink_cut=state.get("ink_cut"),
            )
        except Exception:  # noqa: BLE001 - a diagnostics glitch must not kill the thread
            logging.exception("Falha ao montar o diagnóstico")
            stop.wait(2.0)
            continue
        try:
            frames.get_nowait()
        except queue.Empty:
            pass
        frames.put_nowait(mosaic)
        # Rest several times as long as the work took, so this thread never
        # takes more than about a sixth of the interpreter's time.
        stop.wait(max(0.5, 5 * (time.perf_counter() - started)))


def ink_slider_box() -> tuple[int, int, int]:
    """(x, y, width) in mosaic pixels of the black/white control: right under the binary picture."""
    row2_y = TITLE_H + PANEL_H + GAP
    base_x, base_y = PANEL_W + GAP, row2_y + TITLE_H
    return base_x + PANEL_W + 4, base_y + 142, PANEL_W - 8


class DebugWindow:
    """Floating window that rebuilds the mosaic about twice a second."""

    def __init__(
        self, root: tk.Tk, get_state: Callable[[], dict],
        on_ink_cut: Callable[[float | None, bool], None] | None = None,
    ) -> None:
        self._get_state = get_state
        self._on_ink_cut = on_ink_cut
        self._dragging_cut = False
        self.window = tk.Toplevel(root, class_="Pythontkfloating")
        self.window.title("LevainPeak · Diagnóstico")
        self.window.geometry("980x780")
        self.window.minsize(560, 440)
        # The window's own background shows around the picture as a border to grab.
        self.window.configure(background="#4a504d")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._label = tk.Label(self.window, background="#262928", borderwidth=0)
        self._label.pack(fill="both", expand=True, padx=BORDER, pady=BORDER)
        # The black/white control sits on the picture, right under the binary image of the display.
        self._cut_text = tk.Label(self._label, background="#262928", foreground="#e8c547", font=("TkDefaultFont", 8))
        self._cut_var = tk.DoubleVar(value=50)
        self._cut_scale = ttk.Scale(self._label, from_=0, to=100, orient="horizontal", variable=self._cut_var)
        # Back to the default cut of the running profile (None = reset).
        self._cut_auto = ttk.Button(
            self._label, text="auto", width=6, command=lambda: self._on_ink_cut and self._on_ink_cut(None, True)
        )
        self._cut_scale.bind("<ButtonPress-1>", lambda _e: setattr(self, "_dragging_cut", True), add="+")
        self._cut_scale.bind("<B1-Motion>", lambda _e: self._cut_changed(False), add="+")
        self._cut_scale.bind("<ButtonRelease-1>", lambda _e: self._cut_changed(True), add="+")
        # Drag the picture to move the window, the border to resize it.
        self._controls = FloatingWindowControls(self.window, self._label, on_close=self.close)
        self._photo: ImageTk.PhotoImage | None = None
        self._latest: np.ndarray | None = None
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_diagnostics_worker, args=(get_state, self._frames, self._stop),
            name="debug-view", daemon=True,
        )
        self._thread.start()
        self.window.after(150, self._poll)

    @property
    def open(self) -> bool:
        return not self._stop.is_set() and bool(self.window.winfo_exists())

    def lift(self) -> None:
        self.window.lift()

    def close(self) -> None:
        self._stop.set()
        if self.window.winfo_exists():
            self.window.destroy()

    def _poll(self) -> None:
        if self._stop.is_set() or not self.window.winfo_exists():
            return
        try:
            self._latest = self._frames.get_nowait()
        except queue.Empty:
            pass
        if self._latest is not None:
            self._show(self._latest)
        self.window.after(150, self._poll)

    def _cut_changed(self, release: bool) -> None:
        if self._on_ink_cut is not None:
            self._on_ink_cut(float(self._cut_var.get()), release)
        if release:
            self._dragging_cut = False

    def _place_cut_control(self, scale: float, offset: tuple[int, int]) -> None:
        """Put the control over the picture, or hide it when there is no display marked."""
        try:
            control = self._get_state().get("ink_control")
        except Exception:  # noqa: BLE001 - the window must survive a bad read
            control = None
        if not control:
            self._cut_scale.place_forget()
            self._cut_text.place_forget()
            self._cut_auto.place_forget()
            return
        if not self._dragging_cut:
            self._cut_var.set(control["percent"])
        self._cut_text.configure(text=f"traço do visor · perfil {control['profile']} · {control['percent']:.0f}%")
        self._cut_auto.configure(text="auto ✓" if control.get("auto") else "auto")
        x, y, width = ink_slider_box()
        self._cut_text.place(x=offset[0] + int(x * scale), y=offset[1] + int(y * scale) - 15)
        slider_width = max(60, int(width * scale) - 62)
        self._cut_scale.place(x=offset[0] + int(x * scale), y=offset[1] + int(y * scale) + 2, width=slider_width)
        self._cut_auto.place(x=offset[0] + int(x * scale) + slider_width + 6, y=offset[1] + int(y * scale) - 2)

    def _show(self, mosaic: np.ndarray) -> None:
        available_w = max(1, self._label.winfo_width())
        available_h = max(1, self._label.winfo_height())
        scale = min(available_w / mosaic.shape[1], available_h / mosaic.shape[0])
        size = (max(1, int(mosaic.shape[1] * scale)), max(1, int(mosaic.shape[0] * scale)))
        self._place_cut_control(scale, ((available_w - size[0]) // 2, (available_h - size[1]) // 2))
        image = Image.fromarray(cv2.cvtColor(mosaic, cv2.COLOR_BGR2RGB)).resize(size, Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self._label.configure(image=self._photo)
