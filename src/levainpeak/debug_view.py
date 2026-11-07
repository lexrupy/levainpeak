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
from levainpeak.surface_strips import read_strips
from levainpeak.vision import ContainerMeasurement, column_scan, inner_span, measure_container

PANEL_W, PANEL_H, TITLE_H, GAP = 230, 380, 22, 8
SECOND_ROW_H, INFO_H = 240, 114
WIDTH = 5 * PANEL_W + 4 * GAP
HEIGHT = TITLE_H + PANEL_H + GAP + TITLE_H + SECOND_ROW_H + GAP + INFO_H

BACKGROUND = (38, 41, 40)
TITLE_BAR = (62, 68, 66)
GRID = (74, 80, 78)
YELLOW, ORANGE, CYAN, GREEN, BLUE, RED = (40, 210, 255), (60, 140, 255), (255, 220, 60), (110, 200, 90), (255, 160, 50), (70, 70, 235)
WHITE, MUTED = (240, 240, 235), (170, 176, 172)
MAGENTA = (220, 80, 220)  # the walls the automatic search found, to compare with the drawn contour
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
    surface_bands: tuple[tuple[float, float], ...] = (),
    auto_strips: bool = False,
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
            raw = measure_container(
                frame, roi=pot_roi, vertical_bounds=vertical_bounds, outline=outline,
                surface_bands=surface_bands, auto_strips=auto_strips,
            )
        except cv2.error:
            raw = None
    detected = raw
    if frame is not None and outline is not None:
        # What the automatic search finds without the drawn contour, to check the drawing against.
        try:
            detected = measure_container(frame, roi=pot_roi, vertical_bounds=vertical_bounds)
        except cv2.error:
            detected = None
    scan = None
    if frame is not None and raw is not None and raw.marker_y is not None:
        scan = column_scan(frame, raw.left, raw.right, raw.top, raw.bottom, raw.marker_y)

    strips = strip_context = None
    if frame is not None and outline is not None and (raw is None or getattr(raw, "method", "") in ("automático", "faixas")):
        span = inner_span(tuple(tuple(point) for point in outline), (frame.shape[1], frame.shape[0]))
        if span is not None:
            infrared = float(np.abs(frame[..., 0].astype(np.int16) - frame[..., 2]).mean()) < 3
            gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Panel 4 shows the way the level was really read: strips for "automático" and "faixas"
            # (only inside the drawn bands for the latter), the old row profile for "central".
            used = getattr(raw, "method", "") if raw is not None else ""
            drawn = surface_bands if len(surface_bands) >= 2 and used != "central" else ()
            if used == "faixas" and len(surface_bands) >= 2:
                drawn = surface_bands
            skyline = read_strips(gray_full, *span[:2], span[2], span[3], infrared)  # every strip, for the chart
            strips = read_strips(gray_full, *span[:2], span[2], span[3], infrared, bands=drawn) if drawn else skyline
            strip_context = (skyline, drawn, span[:2])

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
            if strips is not None and scan is None:
                _draw_strips(column_view, raw, strips, strip_context, stable, texts)
            else:
                _draw_columns(column_view, raw, scan, stable, lab, left, top)
        for view, title in (
            (detection_view, "1 · Detecção"),
            (light_view, "2 · Luminosidade (L)"),
            (edge_view, "3 · Bordas (gradiente X)"),
            (column_view, _panel4_title(raw, strips is not None and scan is None)),
        ):
            index = (detection_view, light_view, edge_view, column_view).index(view)
            place(view.canvas, index * (PANEL_W + GAP), 0, title)
        walls_view = _View(crop, origin, PANEL_W, PANEL_H)
        _draw_walls(walls_view, detected, outline, texts)
        place(walls_view.canvas, 4 * (PANEL_W + GAP), 0, "5 · Paredes detectadas")

    width_panel = _profile_panel(raw, stable, texts, row2_y)
    place(width_panel, 0, row2_y, "6 · Largura e volume por linha")
    inspection = (
        inspect_temperature(
            frame, thermometer_roi, None if profile is None else profile.infrared, face_quad, ink_cut
        )
        if frame is not None and thermometer_roi is not None
        else None
    )
    thermo = _thermometer_panel(inspection, thermometer_roi, texts, row2_y)
    place(thermo, PANEL_W + GAP, row2_y, "7 · Termômetro: recorte · face · binário · dígitos")

    _info_strip(texts, info_y, raw, scan, stable, temperature_c, thermometer_roi, frame, inspection, profile, vertical_bounds, drawn=outline is not None)
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


def _draw_walls(view: _View, detected, outline, texts: list[Text]) -> None:
    """The drawn contour (cyan) over the walls the automatic search found (magenta)."""
    if outline is not None:
        view.polyline([view.pt(x, y) for x, y in outline] + [view.pt(*outline[0])], CYAN, 1)
    if detected is None or not detected.edge_profile:
        texts.append((PANEL_W * 4 + 4 * GAP + 6, TITLE_H + PANEL_H - 18, "paredes não detectadas", MUTED, 11))
        return
    edges = np.asarray(detected.edge_profile, np.float32)
    rows = detected.top + np.arange(len(edges))
    view.polyline([view.pt(x, y) for x, y in zip(edges[:, 0], rows)], MAGENTA, 2)
    view.polyline([view.pt(x, y) for x, y in zip(edges[:, 1], rows)], MAGENTA, 2)
    if outline is not None:
        xs_left, xs_right = _outline_walls(outline, detected.top, len(edges))
        if xs_left is not None:
            left = float(np.abs(edges[:, 0] - xs_left).mean())
            right = float(np.abs(edges[:, 1] - xs_right).mean())
            texts.append((PANEL_W * 4 + 4 * GAP + 6, TITLE_H + PANEL_H - 18,
                          f"diferença média: esq {left:.0f} px · dir {right:.0f} px", WHITE, 11))


def _outline_walls(outline, top: int, rows: int):
    """x of the drawn contour's left and right sides at ``rows`` rows from ``top``, or None."""
    points = np.asarray(outline, np.float32)
    ys = top + np.arange(rows, dtype=np.float32)
    lefts, rights = [], []
    for y in ys:
        crossings = []
        for (x0, y0), (x1, y1) in zip(points, np.roll(points, -1, axis=0)):
            if (y0 <= y < y1) or (y1 <= y < y0):
                crossings.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        if len(crossings) < 2:
            lefts.append(np.nan)
            rights.append(np.nan)
        else:
            lefts.append(min(crossings))
            rights.append(max(crossings))
    left, right = np.asarray(lefts), np.asarray(rights)
    if np.isnan(left).all():
        return None, None
    fill = lambda a: np.where(np.isnan(a), np.nanmean(a), a)  # noqa: E731
    return fill(left), fill(right)


def _draw_edges(view: _View, raw: ContainerMeasurement) -> None:
    cv2.rectangle(view.canvas, view.pt(raw.left, raw.top), view.pt(raw.right, raw.bottom), GREEN, 1)
    if raw.edge_profile:
        edges = np.asarray(raw.edge_profile, np.float32)
        rows = raw.top + np.arange(len(edges))
        view.polyline([view.pt(x, y) for x, y in zip(edges[:, 0], rows)], CYAN, 2)
        view.polyline([view.pt(x, y) for x, y in zip(edges[:, 1], rows)], CYAN, 2)
    view.hline(raw.surface_y, YELLOW, thickness=1, dashed=True)


STRIP_WINDOW = 60  # rows shown around the level in the strips panel: the spread is a few rows, not the whole pot


def _panel4_title(raw, strips_shown: bool) -> str:
    if not strips_shown:
        return "4 · Topo por coluna (leitura central)" if getattr(raw, "method", "") == "central" else "4 · Topo por coluna"
    if getattr(raw, "method", "") == "faixas":
        return "4 · Faixas desenhadas (mediana)"
    return "4 · Faixas de 3 px: verde vota"


def _draw_strips(view: _View, raw, voting, context, stable, texts: list[Text]) -> None:
    """One bar per narrow strip across the whole glass, rising from the bottom to the edge that strip
    found, zoomed on the surface. Green: the strips that voted (the stretch that gave the level); grey: the
    rest; the drawn bands show as lighter columns behind; the level is the yellow line."""
    skyline, bands, (left, right) = context
    canvas = view.canvas
    height, width = canvas.shape[:2]
    values = [y for y in skyline.ys if y is not None]
    centre = voting.level if voting.level is not None else (float(np.median(values)) if values else raw.surface_y)
    scale = height / STRIP_WINDOW

    def row(y: float) -> int:
        return int(round(height / 2 + (y - centre) * scale))

    xs = [x for x, y in zip(skyline.xs, skyline.ys) if y is not None]
    low, high = (min(xs), max(xs)) if xs else (left, right)
    across = (width - 20) / max(1.0, high - low)  # panel pixels per frame pixel along the glass

    def column(x: float) -> int:
        return int(round(10 + (x - low) * across))

    for start, end in bands:  # where the user asked the surface to be read
        x0, x1 = column(left + start * (right - left)), column(left + end * (right - left))
        canvas[:, max(0, x0):max(0, x1) + 1] = np.clip(canvas[:, max(0, x0):max(0, x1) + 1].astype(np.int16) + 14, 0, 255).astype(np.uint8)
    for offset in range(-STRIP_WINDOW // 2, STRIP_WINDOW // 2 + 1, 10):  # a faint ruler every 10 rows
        cv2.line(canvas, (0, row(centre + offset)), (width - 1, row(centre + offset)), GRID, 1)
    voted = {round(x, 1) for x, k in zip(voting.xs, voting.kept) if k}
    half = max(1, int(round(1.5 * across)))  # half a bar: a strip is 3 px wide
    for x, y in zip(skyline.xs, skyline.ys):
        if y is None:
            continue
        px = column(x)
        top_row = int(np.clip(row(y), 0, height - 1))
        colour = GREEN if round(x, 1) in voted else (95, 100, 98)
        cv2.rectangle(canvas, (px - half, top_row), (px + half, height - 1), colour, -1)
    if voting.level is not None:
        cv2.line(canvas, (0, row(voting.level)), (width - 1, row(voting.level)), YELLOW, 2)
    if stable is not None and 0 <= row(stable.surface_y) < height:
        for x in range(0, width, 10):
            cv2.line(canvas, (x, row(stable.surface_y)), (min(x + 5, width - 1), row(stable.surface_y)), ORANGE, 1)
    state = (
        "sem acordo" if voting.level is None
        else f"nível {voting.level:.1f} · {voting.confidence:.0%} votam" + (" · domo" if voting.dome else "")
    )
    texts.append((3 * (PANEL_W + GAP) + 4, TITLE_H + 4, state, YELLOW, 10))
    texts.append((3 * (PANEL_W + GAP) + 4, TITLE_H + 18, f"±{STRIP_WINDOW // 2} linhas · régua a cada 10 · passo {voting.step:.1f}", MUTED, 9))


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
        if raw.volume_below_profile:
            index = int(np.clip(used - raw.top, 0, len(raw.volume_below_profile) - 1))
            below_mark = raw.volume_below_profile[index]
            texts.append((PANEL_W - 70, row_of(used) + row2_y + TITLE_H + 4, f"{below_mark:.0%}", BLUE, 11))
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


def _info_strip(texts: list[Text], y: int, raw, scan, stable, temperature_c, thermometer_roi, frame, inspection=None, profile=None, vertical_bounds=None, drawn=False) -> None:
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
        method = "por baixo (contorno desenhado)" if drawn else "colunas (claridade)" if scan is not None else "bordas (reserva)"
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
    lines.append(("Legenda: amarelo = superfície bruta · laranja tracejado = estável · azul = marca inicial usada · azul fraco = faixa detectada · ciano = contorno desenhado · magenta = parede detectada", MUTED))
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
                surface_bands=state.get("surface_bands", ()), auto_strips=state.get("auto_strips", False),
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


def panel_boxes() -> tuple[tuple[int, int, int, int], ...]:
    """(x, y, width, height) in mosaic pixels of panels 1 to 7, title bar included."""
    row2_y = TITLE_H + PANEL_H + GAP
    top = tuple((index * (PANEL_W + GAP), 0, PANEL_W, TITLE_H + PANEL_H) for index in range(5))
    return top + (
        (0, row2_y, PANEL_W, TITLE_H + SECOND_ROW_H),
        (PANEL_W + GAP, row2_y, 3 * PANEL_W + 2 * GAP, TITLE_H + SECOND_ROW_H),
    )


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
        self._zoomed: int | None = None  # index of the panel shown alone (double click), or None
        self._view_scale, self._view_offset = 1.0, (0, 0)
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
        self._controls = FloatingWindowControls(self.window, self._label, on_close=self._escape)
        self._label.bind("<Double-Button-1>", self._toggle_panel_zoom, add="+")
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

    def _escape(self) -> None:
        """Esc leaves a zoomed panel first; the window closes only from the mosaic."""
        if self._zoomed is None:
            self.close()
            return
        self._zoomed = None
        if self._latest is not None:
            self._show(self._latest)

    def _toggle_panel_zoom(self, event: tk.Event) -> None:
        """Double click on a panel shows it alone; a double click again brings back the mosaic."""
        if self._zoomed is not None:
            self._zoomed = None
        elif self._latest is not None:
            crop_x, crop_y = 0, 0
            x = (event.x - self._view_offset[0]) / self._view_scale + crop_x
            y = (event.y - self._view_offset[1]) / self._view_scale + crop_y
            for index, (left, top, width, height) in enumerate(panel_boxes()):
                if left <= x < left + width and top <= y < top + height:
                    self._zoomed = index
                    break
        if self._latest is not None:
            self._show(self._latest)

    def _place_cut_control(self, scale: float, offset: tuple[int, int], crop: tuple[int, int] = (0, 0)) -> None:
        """Put the control over the picture, or hide it when there is no display marked."""
        try:
            control = self._get_state().get("ink_control")
        except Exception:  # noqa: BLE001 - the window must survive a bad read
            control = None
        if not control or (self._zoomed is not None and self._zoomed != 6):
            self._cut_scale.place_forget()
            self._cut_text.place_forget()
            self._cut_auto.place_forget()
            return
        if not self._dragging_cut:
            self._cut_var.set(control["percent"])
        self._cut_text.configure(text=f"traço do visor · perfil {control['profile']} · {control['percent']:.0f}%")
        self._cut_auto.configure(text="auto ✓" if control.get("auto") else "auto")
        x, y, width = ink_slider_box()
        x, y = x - crop[0], y - crop[1]
        self._cut_text.place(x=offset[0] + int(x * scale), y=offset[1] + int(y * scale) - 15)
        slider_width = max(60, int(width * scale) - 62)
        self._cut_scale.place(x=offset[0] + int(x * scale), y=offset[1] + int(y * scale) + 2, width=slider_width)
        self._cut_auto.place(x=offset[0] + int(x * scale) + slider_width + 6, y=offset[1] + int(y * scale) - 2)

    def _show(self, mosaic: np.ndarray) -> None:
        crop = (0, 0)
        if self._zoomed is not None:
            left, top, width, height = panel_boxes()[self._zoomed]
            mosaic = mosaic[top:top + height, left:left + width]
            crop = (left, top)
        available_w = max(1, self._label.winfo_width())
        available_h = max(1, self._label.winfo_height())
        scale = min(available_w / mosaic.shape[1], available_h / mosaic.shape[0])
        size = (max(1, int(mosaic.shape[1] * scale)), max(1, int(mosaic.shape[0] * scale)))
        offset = ((available_w - size[0]) // 2, (available_h - size[1]) // 2)
        self._view_scale, self._view_offset = scale, offset
        self._place_cut_control(scale, offset, crop)
        image = Image.fromarray(cv2.cvtColor(mosaic, cv2.COLOR_BGR2RGB)).resize(size, Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self._label.configure(image=self._photo)
