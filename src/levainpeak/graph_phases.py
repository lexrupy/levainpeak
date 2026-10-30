"""Growth-curve phases for the session graph, decided with hindsight."""

from __future__ import annotations

from datetime import datetime

from levainpeak.sessions import PhaseTracker

LATENCY = "Latência"
EXPONENTIAL = "Exponencial"
STATIONARY = "Estacionária"
DECLINE = "Decaimento"

_PLATEAU = "_plateau"
_GROUPS = {
    PhaseTracker.WAITING: LATENCY,
    PhaseTracker.RISING: EXPONENTIAL,
    PhaseTracker.PLATEAU: _PLATEAU,
    PhaseTracker.PEAK: _PLATEAU,
    PhaseTracker.FALL_CHECK: _PLATEAU,
    PhaseTracker.FALLING: DECLINE,
}


def graph_phase_segments(
    stamps: list[datetime],
    states: list[str],
    *,
    flicker_fraction: float = 0.03,
) -> list[tuple[str, datetime, datetime]]:
    """Segments (phase, start, end) named after the classic growth curve.

    The stationary phase is the level stretch at the peak, right before the
    volume starts to fall. A flat stretch that is followed by growth again was
    only a pause, and a flat stretch that never reached a confirmed peak is
    not a stationary phase either, so both are drawn as exponential growth.
    """
    if not stamps:
        return []
    duration = max(1.0, (stamps[-1] - stamps[0]).total_seconds())
    runs: list[dict] = []
    for stamp, state in zip(stamps, states, strict=True):
        group = _GROUPS.get(state)
        if group is None:
            continue
        if runs and runs[-1]["group"] == group:
            runs[-1]["end"] = stamp
            runs[-1]["states"].add(state)
        elif runs and (runs[-1]["end"] - runs[-1]["start"]).total_seconds() < duration * flicker_fraction:
            # A run too short to matter is absorbed by the previous one.
            runs[-1].update(group=group, end=stamp, states={state})
        else:
            runs.append({"group": group, "start": stamp, "end": stamp, "states": {state}})

    for index, run in enumerate(runs):
        if run["group"] != _PLATEAU:
            continue
        following = runs[index + 1]["group"] if index + 1 < len(runs) else None
        reached_peak = PhaseTracker.PEAK in run["states"]
        if following == DECLINE or (reached_peak and following != EXPONENTIAL):
            run["group"] = STATIONARY
        else:
            run["group"] = EXPONENTIAL

    merged: list[tuple[str, datetime, datetime]] = []
    for run in runs:
        if merged and merged[-1][0] == run["group"]:
            merged[-1] = (run["group"], merged[-1][1], run["end"])
        else:
            merged.append((run["group"], run["start"], run["end"]))
    return merged
