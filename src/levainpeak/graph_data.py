"""The session graph's data, shared by the desktop window and the phone panel."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

from levainpeak.filters import despike, valid_after_recovery
from levainpeak.graph_phases import graph_phase_segments
from levainpeak.sessions import PhaseTracker


def prepare_graph_data(path: Path) -> dict:
    """The graph's series from a session CSV: cleaned, phased and smoothed."""
    samples = []
    phases: list[str] = []
    last_temperature: float | None = None
    baseline = None
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
    if len(samples) < 2:
        return {"too_few": True, "session_name": session_name}
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
    graph_tracker = PhaseTracker(filter_dips=False)  # the readings are already clean
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

    return {
        "too_few": False,
        "session_name": session_name,
        "samples": samples,
        "phases": phases,
        "baseline": baseline,
    }


def volume_scale(baseline: float | None, volumes: list[float]) -> tuple[float, float, str]:
    """(0% level, 100% level, label) of the volume axis.

    0% is the initial volume. 100% is the doubling of it until that is reached;
    once the volume goes past it, 100% is the session's peak.
    """
    start = baseline if baseline and baseline > 0 else volumes[0]
    peak = max(volumes)
    double_target = 2 * start
    if double_target > 1.0:
        return start, 1.0, "capacidade"
    if peak >= double_target:
        return start, peak, "máximo atingido"
    return start, double_target, "dobro"


def chart_payload(data: dict, max_points: int = 400) -> dict | None:
    """The series for the phone panel, small enough for one spreadsheet cell.

    Volumes come already on the graph's axis (0% = initial volume, 100% = the
    doubling, or the peak once past it). Times are seconds since ``t0``. None
    while there is too little data to draw.
    """
    if data.get("too_few"):
        return None
    samples = data["samples"]
    baseline, full_scale, label = volume_scale(data["baseline"], [v for _, v, _ in samples])
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
    return {
        "nome": data["session_name"],
        "t0": first.isoformat(timespec="seconds"),
        "escala": label,
        "pts": points,
        "fases": phases,
    }
