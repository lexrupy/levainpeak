"""The session graph's data, shared by the desktop window and the phone panel."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

from levainpeak.rate import rate_series

from levainpeak.filters import bridge_bumps, bridge_holds, despike, enforce_trend, valid_after_recovery
from levainpeak.graph_phases import graph_phase_segments
from levainpeak.sessions import PhaseTracker


# Fitted to the five sessions that doubled (6 to 8/11), each laid on a time axis where 1.0 is its doubling:
# the mean error against the real curves falls from 0.17 (straight 2**f) to 0.05.
FORECAST_LAG_SHARE = 0.1  # of the time to the doubling spent near the start level
FORECAST_SHARPNESS = 0.5  # how steeply the exponential climbs once it has started (low: nearly a parabola)


def forecast_growth(fraction: float, lag_share: float = FORECAST_LAG_SHARE, sharpness: float = FORECAST_SHARPNESS) -> float:
    """The growth factor expected at ``fraction`` (0 to 1) of the way from the start to the doubling.

    The levain sits near its start level for a while (the lag), then takes off exponentially and reaches
    twice the initial volume at 1.0. The take-off begins with no slope, as a real curve does, so the
    line bends instead of breaking at the end of the lag."""
    if fraction <= lag_share:
        return 1.0
    u = min(1.0, (fraction - lag_share) / (1.0 - lag_share))
    return 1.0 + (np.exp(sharpness * u) - 1.0 - sharpness * u) / (np.exp(sharpness) - 1.0 - sharpness)


def prepare_graph_data(path: Path, *, peak_seconds: float = 600, artifact_seconds: float = 720) -> dict:
    """The graph's series from a session CSV: cleaned, phased and smoothed.

    ``peak_seconds``: how long the readings stay level before the levain is at its peak.
    ``artifact_seconds``: a departure that comes back within this is dropped (a lamp, a lifted pot).
    """
    samples = []
    phases: list[str] = []
    last_temperature: float | None = None
    baseline = None
    mix_time = None
    session_name = path.stem
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
            if row.get("mix_time"):
                try:
                    mix_time = datetime.fromisoformat(row["mix_time"])
                except ValueError:
                    pass
    if len(samples) < 2:
        return {"too_few": True, "session_name": session_name}
    # Drop dips and peaks that leave the trend and come back (for example the
    # surface being read at the dark band for a few minutes).
    # The heating lamp comes on for up to ten minutes and shifts the picture's brightness; a
    # departure that returns within that time is an artifact. The window is in time, not samples.
    steps = sorted((b[0] - a[0]).total_seconds() for a, b in zip(samples, samples[1:]) if b[0] > a[0])
    step = steps[len(steps) // 2] if steps else 10.0
    window = max(80, int(round(2 * artifact_seconds / max(1.0, step))))
    origin_stamp = samples[0][0]
    seconds = [(sample[0] - origin_stamp).total_seconds() for sample in samples]
    # A step away from the trend and back (the lamp's brightness shift) is drawn as the line between its
    # ends. This goes first: the spike filter alone would cut such a bump into pieces.
    cleaned = bridge_bumps(seconds, [sample[1] for sample in samples], max_seconds=artifact_seconds)
    cleaned = despike(cleaned, window=window, threshold=0.008)
    # Readings held dead by the lamp, then a leap: drawn as the line between them.
    cleaned = bridge_holds(seconds, cleaned)
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
    # Up to the peak nothing sinks below what it reached; after it nothing rises again (a rise after a fall is a misread).
    cleaned = enforce_trend(cleaned, seconds)
    # Phases are recomputed from the cleaned readings, so the shading does
    # not inherit labels that were produced while a wrong reading was live.
    graph_tracker = PhaseTracker(filter_dips=False, plateau_seconds=peak_seconds, artifact_seconds=artifact_seconds)  # the readings are already clean
    # The mark may sit below the level the session started at (placed late, or the dough already
    # up): the rise to judge is the one since the first reading, not since the mark.
    graph_tracker.reset(max(baseline, cleaned[0]) if baseline and baseline > 0 else cleaned[0])
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

    return {
        "too_few": False,
        "session_name": session_name,
        "samples": samples,
        "phases": phases,
        "baseline": baseline,
        "mix_time": mix_time,
    }


VOLUME_SCALES = ("auto", "máximo", "capacidade")


def volume_scale(
    baseline: float | None, volumes: list[float], mode: str = "auto"
) -> tuple[float, float, str]:
    """(0% level, 100% level, label) of the volume axis.

    0% is the initial volume. ``auto``: 100% is the doubling of it until that is
    reached, then the session's peak. ``máximo``: always the peak. ``capacidade``:
    always the full pot.
    """
    start = baseline if baseline and baseline > 0 else volumes[0]
    peak = max(volumes)
    if mode == "capacidade":
        return start, 1.0, "capacidade"
    if mode == "máximo":
        return start, max(peak, start + 1e-3), "máximo atingido"
    double_target = 2 * start
    if double_target > 1.0:
        return start, 1.0, "capacidade"
    if peak >= double_target:
        return start, peak, "máximo atingido"
    return start, double_target, "dobro"


def chart_payload(
    data: dict, max_points: int = 400, *, temperature_scale: str = "ampla", volume_mode: str = "auto"
) -> dict | None:
    """The series for the phone panel, small enough for one spreadsheet cell.

    Volumes come already on the graph's axis (0% = initial volume, 100% = the
    doubling, or the peak once past it). Times are seconds since ``t0``. None
    while there is too little data to draw.
    """
    if data.get("too_few"):
        return None
    samples = data["samples"]
    baseline, full_scale, label = volume_scale(data["baseline"], [v for _, v, _ in samples], volume_mode)
    span = max(1e-6, full_scale - baseline)
    first = samples[0][0]
    stride = max(1, -(-len(samples) // max_points))
    indexes = list(range(0, len(samples), stride))
    if indexes[-1] != len(samples) - 1:
        indexes.append(len(samples) - 1)  # the newest reading is always there
    points = [
        [
            int((samples[i][0] - first).total_seconds()),
            round(100 * (samples[i][1] - baseline) / span, 1),
            None if samples[i][2] is None else round(samples[i][2], 1),
        ]
        for i in indexes
    ]
    segments = graph_phase_segments([sample[0] for sample in samples], data["phases"])
    phases = [
        [name, int((start - first).total_seconds()), int((end - first).total_seconds())]
        for name, start, end in segments
    ]
    payload = {
        "nome": data["session_name"],
        "t0": first.isoformat(timespec="seconds"),
        "escala": label,
        "pts": points,
        "fases": phases,
    }
    pace = rate_series([(sample[0].timestamp(), sample[1]) for sample in samples])
    if len(pace) >= 2:  # the growth rate (%/h, last 15 minutes) for the strip under the panel's chart
        pace_stride = max(1, -(-len(pace) // 120))
        payload["taxa"] = [[int(t - first.timestamp()), round(value, 1)] for t, value in pace[::pace_stride]]
    temperatures = [sample[2] for sample in samples if sample[2] is not None]
    if temperatures:
        payload["temp_eixo"] = list(temperature_axis(temperatures, temperature_scale))
    return payload


# Narrowest range (degrees) the temperature axis may zoom to, by mode.
TEMPERATURE_SCALES = {"ampla": 4, "zoom": 1, "larga": 12}


def temperature_axis(values: list[float], mode: str = "ampla") -> tuple[float, float]:
    """Limits of the temperature axis.

    ``zoom`` fits the data (one degree at the least): a 0.1 degree wobble becomes a
    spike as tall as the chart. ``ampla`` and ``larga`` keep a wider range, in whole
    degrees, a multiple of four so the five ticks fall on whole degrees, with the data
    in the middle.
    """
    low_value, high_value = min(values), max(values)
    minimum = TEMPERATURE_SCALES.get(mode, TEMPERATURE_SCALES["ampla"])
    if mode == "zoom":
        if high_value - low_value < 1.0:
            center = (high_value + low_value) / 2
            return center - 0.5, center + 0.5
        margin = (high_value - low_value) * 0.08
        return low_value - margin, high_value + margin
    span = max(minimum, -(-(high_value - low_value + 0.5) // 4) * 4)
    low = int((low_value + high_value) / 2 // 1 - span // 2)
    while low_value - 0.25 < low:
        low -= 1
    while high_value + 0.25 > low + span:
        span += 4
    return float(low), float(low + span)
