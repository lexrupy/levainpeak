"""Doubling forecast that uses the temperature history as context."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Callable

from levainpeak.filters import valid_after_recovery

REFERENCE_TEMPERATURE_C = 24.0

# Relative yeast activity versus 24 °C, read by eye from the "Atividade vs.
# Temperatura" chart (madrepaesartesanais.com.br): C. milleri, the yeast that
# produces the gas that raises the dough, peaks near 27 °C and collapses
# toward zero by about 36 °C. The lactobacillus curve (peak near 33 °C) drives
# acidity, not volume, so it is not used here. Values are approximate and only
# stand in until the session has enough data of its own.
LITERATURE_ACTIVITY = (
    (4.0, 0.02),
    (8.0, 0.08),
    (12.0, 0.20),
    (16.0, 0.36),
    (18.0, 0.47),
    (20.0, 0.62),
    (22.0, 0.80),
    (24.0, 1.00),
    (26.0, 1.19),
    (27.0, 1.31),
    (28.0, 1.22),
    (30.0, 0.94),
    (32.0, 0.59),
    (33.0, 0.38),
    (34.0, 0.16),
    (36.0, 0.03),
)
# Activity never reaches exactly zero so thermal time keeps advancing.
MIN_ACTIVITY = 0.02

SEGMENT_SECONDS = 300
MIN_SEGMENTS = 8
MIN_TEMPERATURE_SPAN_C = 3.0
MIN_Q10, MAX_Q10 = 1.2, 4.0

TemperatureModel = Callable[[float | None], float]

# Before the levain has really risen there is no rate of its own to measure, so the forecast comes from
# the table: this long to double at 24 °C with the reference ratio (starter:water:flour), scaled by the
# temperature the dough went through, and moved by how much starter it began with.
REFERENCE_RATIO = (1.0, 5.0, 5.0)
HOURS_PER_HALVING = 1.0  # one more hour each time the starter share is halved
MIN_BASE_HOURS = 1.5


def inoculation(ratio: tuple[float, float, float]) -> float:
    """The starter's share of the whole dough."""
    return ratio[0] / sum(ratio)


def base_hours_for(base_hours: float, ratio: tuple[float, float, float] | None) -> float:
    """Hours to double at 24 °C for this ratio, from the value for the reference ratio."""
    if ratio is None or min(ratio) <= 0:
        return base_hours
    shift = HOURS_PER_HALVING * math.log2(inoculation(REFERENCE_RATIO) / inoculation(ratio))
    return max(MIN_BASE_HOURS, base_hours + shift)


def literature_factor(temperature_c: float | None) -> float:
    """Relative activity from the fixed literature table (linear interpolation)."""
    if temperature_c is None:
        return 1.0
    temps = [point[0] for point in LITERATURE_ACTIVITY]
    if temperature_c <= temps[0]:
        return max(MIN_ACTIVITY, LITERATURE_ACTIVITY[0][1])
    if temperature_c >= temps[-1]:
        return max(MIN_ACTIVITY, LITERATURE_ACTIVITY[-1][1])
    index = bisect_right(temps, temperature_c)
    (t0, f0), (t1, f1) = LITERATURE_ACTIVITY[index - 1], LITERATURE_ACTIVITY[index]
    return max(MIN_ACTIVITY, f0 + (f1 - f0) * (temperature_c - t0) / (t1 - t0))


def fit_session_model(
    segments: list[tuple[float, float]],
) -> TemperatureModel | None:
    """Fit ln(rate) = a + b*T on (mean temperature, rise rate) segments.

    Returns None when the session does not cover enough different
    temperatures, or the fitted sensitivity is implausible.
    """
    if len(segments) < MIN_SEGMENTS:
        return None
    temps = [temp for temp, _ in segments]
    if max(temps) - min(temps) < MIN_TEMPERATURE_SPAN_C:
        return None
    logs = [math.log(rate) for _, rate in segments]
    mean_t = sum(temps) / len(temps)
    mean_l = sum(logs) / len(logs)
    denominator = sum((t - mean_t) ** 2 for t in temps)
    if denominator <= 0:
        return None
    slope = sum((t - mean_t) * (v - mean_l) for t, v in zip(temps, logs, strict=True)) / denominator
    if not math.log(MIN_Q10) / 10 <= slope <= math.log(MAX_Q10) / 10:
        return None
    return lambda temperature_c: (
        1.0
        if temperature_c is None
        else math.exp(slope * (temperature_c - REFERENCE_TEMPERATURE_C))
    )


@dataclass(frozen=True)
class Forecast:
    minutes: float
    uses_temperature: bool
    model: str  # "sessão", "tabela" or "" when no temperature was available


class DoublingForecaster:
    """Fits volume against thermal time, so warm and cool stretches weigh fairly.

    Thermal time is elapsed time scaled by a temperature factor. The factor
    comes from this session's own history once it is rich enough, otherwise
    from the literature table. Without temperature readings it is 1 and this
    reduces to a plain trend.
    """

    def __init__(
        self,
        window_seconds: float = 600,
        minimum_span_seconds: float = 120,
        sample_seconds: float = 10,
    ) -> None:
        self.window_seconds = window_seconds
        self.minimum_span_seconds = minimum_span_seconds
        self.sample_seconds = sample_seconds
        self.reset()

    def reset(self) -> None:
        # (time, volume, temperature)
        self._samples: deque[tuple[float, float, float | None]] = deque(maxlen=8640)

    def add(self, time_s: float, volume: float, temperature_c: float | None) -> None:
        if self._samples and time_s - self._samples[-1][0] < self.sample_seconds:
            return
        self._samples.append((time_s, volume, temperature_c))

    def _segments(self) -> list[tuple[float, float]]:
        """(mean temperature, rise rate per minute) for each rising 5-minute block."""
        segments: list[tuple[float, float]] = []
        block: list[tuple[float, float, float]] = []
        for time_s, volume, temperature in self._samples:
            if temperature is None:
                block.clear()
                continue
            if block and time_s - block[0][0] >= SEGMENT_SECONDS:
                rate = _slope([(t, v) for t, v, _ in block]) * 60
                if rate > 0:
                    segments.append((sum(b[2] for b in block) / len(block), rate))
                block = []
            block.append((time_s, volume, temperature))
        return segments

    def temperature_model(self) -> tuple[TemperatureModel, str]:
        fitted = fit_session_model(self._segments())
        if fitted is not None:
            return fitted, "sessão"
        return literature_factor, "tabela"

    def _window(self) -> list[tuple[float, float, float | None]]:
        if not self._samples:
            return []
        cutoff = self._samples[-1][0] - self.window_seconds
        # Readings that a later recovery proves were not real are left out.
        flags = valid_after_recovery([sample[1] for sample in self._samples])
        return [
            sample
            for sample, valid in zip(self._samples, flags, strict=True)
            if valid and sample[0] >= cutoff
        ]

    def _thermal_rate(
        self, model: TemperatureModel
    ) -> tuple[float, list[float | None]] | None:
        window = self._window()
        if len(window) < 6 or window[-1][0] - window[0][0] < self.minimum_span_seconds:
            return None
        thermal = 0.0
        points: list[tuple[float, float]] = []
        previous: tuple[float, float] | None = None
        for time_s, volume, temperature in window:
            factor = model(temperature)
            if previous is not None:
                thermal += (time_s - previous[0]) * (previous[1] + factor) / 2
            previous = (time_s, factor)
            points.append((thermal, math.log(max(volume, 1e-6))))
        # Levain grows like an exponential, not a line: the rate is that of ln(volume) over thermal time.
        return _slope(points) * 60, [sample[2] for sample in window]

    def rate_per_thermal_minute(self) -> float | None:
        result = self._thermal_rate(self.temperature_model()[0])
        return None if result is None else result[0]

    def table_forecast(
        self, mix_age_seconds: float, now: float, base_hours: float, ratio: tuple[float, float, float] | None = None
    ) -> Forecast | None:
        """Minutes left to double by the table alone: the base time for the ratio, less the thermal time the
        dough has had since it was mixed (the first temperature read stands for the hours before the app
        saw it), over the activity at the temperature it has now. None before any temperature is known."""
        known = [sample for sample in self._samples if sample[2] is not None]
        if not known:
            return None
        first_time, _, first_temperature = known[0]
        seen = max(0.0, now - first_time)
        thermal = max(0.0, mix_age_seconds - seen) * literature_factor(first_temperature)
        previous: tuple[float, float] | None = None
        for time_s, _, temperature in known:
            factor = literature_factor(temperature)
            if previous is not None:
                thermal += (time_s - previous[0]) * (previous[1] + factor) / 2
            previous = (time_s, factor)
        last_time, last_factor = previous
        thermal += max(0.0, now - last_time) * last_factor
        recent = [sample[2] for sample in known][-5:]
        current_factor = literature_factor(sum(recent) / len(recent))
        remaining = base_hours_for(base_hours, ratio) * 3600 - thermal
        return Forecast(minutes=max(0.0, remaining / current_factor / 60), uses_temperature=True, model="tabela")

    def minutes_until(self, current: float, target: float) -> Forecast | None:
        """Minutes until ``target`` volume, or None without a rising trend."""
        model, source = self.temperature_model()
        result = self._thermal_rate(model)
        if result is None or result[0] <= 0:
            return None
        rate, temperatures = result
        recent = [temp for temp in temperatures if temp is not None][-5:]
        temperature = sum(recent) / len(recent) if recent else None
        thermal_minutes = math.log(max(target, 1e-6) / max(current, 1e-6)) / rate
        return Forecast(
            minutes=max(0.0, thermal_minutes / model(temperature)),
            uses_temperature=bool(recent),
            model=source if recent else "",
        )


def _slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y over x; 0 when x has no spread."""
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator <= 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
