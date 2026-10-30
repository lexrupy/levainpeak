from datetime import datetime, timedelta

from levainpeak.graph_phases import (
    DECLINE,
    EXPONENTIAL,
    LATENCY,
    STATIONARY,
    graph_phase_segments,
)
from levainpeak.sessions import PhaseTracker as P


def segments(*blocks):
    """blocks: (state, minutes) pairs sampled every 10 s."""
    start = datetime(2026, 10, 30, 8, 0)
    stamps, states = [], []
    for state, minutes in blocks:
        for _ in range(int(minutes * 6)):
            stamps.append(start + timedelta(seconds=10 * len(stamps)))
            states.append(state)
    return [phase for phase, _, _ in graph_phase_segments(stamps, states)]


def test_pause_during_growth_is_not_stationary():
    result = segments((P.WAITING, 10), (P.RISING, 60), (P.PLATEAU, 15), (P.RISING, 60))
    assert result == [LATENCY, EXPONENTIAL]


def test_level_stretch_before_the_fall_is_stationary():
    result = segments(
        (P.RISING, 90), (P.PLATEAU, 8), (P.PEAK, 20), (P.FALL_CHECK, 5), (P.FALLING, 40)
    )
    assert result == [EXPONENTIAL, STATIONARY, DECLINE]


def test_flat_stretch_that_never_became_a_peak_is_not_stationary():
    assert segments((P.RISING, 90), (P.PLATEAU, 6)) == [EXPONENTIAL]


def test_confirmed_peak_still_ongoing_is_stationary():
    assert segments((P.RISING, 90), (P.PLATEAU, 8), (P.PEAK, 12)) == [EXPONENTIAL, STATIONARY]


def test_peak_followed_by_more_growth_was_only_a_pause():
    result = segments((P.RISING, 60), (P.PEAK, 15), (P.RISING, 60))
    assert result == [EXPONENTIAL]
