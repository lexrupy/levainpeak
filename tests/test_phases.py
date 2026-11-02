import random

from levainpeak.sessions import PhaseTracker


def run(tracker, volume_at, minutes, start=0.0, noise=0.0, seed=1):
    rng = random.Random(seed)
    states = []
    for step in range(int(minutes * 6)):
        t = start + step * 10
        states.append(tracker.update(t, volume_at(t / 60) + rng.gauss(0, noise), 0.2))
    return states


def test_slow_noisy_growth_does_not_flip_to_stationary():
    """~1.6% of capacity per hour with measurement noise, as seen in a real session."""
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.1 * min(1, m / 10), 10)  # initial rise
    states = run(tracker, lambda m: 0.3 + 0.00027 * (m - 10), 90, start=600, noise=0.004)
    settled = states[len(states) // 4:]
    assert settled.count(PhaseTracker.PLATEAU) + settled.count(PhaseTracker.PEAK) == 0


def test_true_plateau_is_still_recognized_and_becomes_a_peak():
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.2 * min(1, m / 60), 60)
    states = run(tracker, lambda m: 0.4, 40, start=3600, noise=0.002)
    assert PhaseTracker.PLATEAU in states
    assert states[-1] == PhaseTracker.PEAK


def test_late_rise_after_a_plateau_is_called_rising_again():
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.1 * min(1, m / 10), 10)
    run(tracker, lambda m: 0.3, 40, start=600, noise=0.002)
    states = run(tracker, lambda m: 0.3 + 0.004 * m, 20, start=3000, noise=0.002)
    assert states[-1] == PhaseTracker.RISING


def test_persistent_fall_is_degradation():
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.2 * min(1, m / 30), 30)
    run(tracker, lambda m: 0.4, 25, start=1800, noise=0.001)  # the top levels off first
    states = run(tracker, lambda m: 0.4 - 0.002 * (m - 55), 30, start=3300)
    assert states[-1] == PhaseTracker.FALLING


def test_a_sudden_drop_while_still_rising_is_not_a_decline():
    """The pot lifted out, or a lamp coming on: the volume falls in seconds, with no top before it."""
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.2 * min(1, m / 30), 30)
    states = run(tracker, lambda m: 0.23, 8, start=1800)  # 0.17 lower for eight minutes
    assert PhaseTracker.FALL_CHECK not in states and PhaseTracker.FALLING not in states


def test_a_dip_that_recovers_never_becomes_a_decline():
    """The surface read at the dark band for minutes, then back to the real level."""
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.1 * min(1, m / 10), 10)
    steady = lambda m: 0.3 + 0.0005 * (m - 10)
    run(tracker, steady, 30, start=600)
    run(tracker, lambda m: steady(m) - 0.06, 6, start=2400)  # wrong readings
    states = run(tracker, steady, 20, start=2760)
    assert PhaseTracker.FALLING not in states
    assert states[-1] in (PhaseTracker.RISING, PhaseTracker.PLATEAU)


def test_a_flat_levain_that_has_not_risen_yet_is_not_at_its_peak():
    tracker = PhaseTracker()
    states = set()
    # The mark sits 0.04 below the level at the start, so the volume starts above it and stays flat.
    for second in range(0, 3 * 3600, 10):
        states.add(tracker.update(float(second), 0.39, 0.35))
    assert states <= {PhaseTracker.WAITING, PhaseTracker.RISING}
    assert PhaseTracker.PEAK not in states and PhaseTracker.PLATEAU not in states


def test_an_abrupt_collapse_that_does_not_come_back_is_a_decline_even_with_no_top_before_it():
    tracker = PhaseTracker()
    run(tracker, lambda m: 0.2 + 0.2 * min(1, m / 30), 30)
    states = run(tracker, lambda m: 0.23, 30, start=1800)  # down by 0.17 and staying down
    assert states[-1] == PhaseTracker.FALLING
    assert PhaseTracker.FALLING not in states[:60]  # not in the first ten minutes


def test_once_rising_a_slow_stretch_or_a_moved_mark_does_not_go_back_to_warming_up():
    from levainpeak.sessions import PhaseTracker

    tracker = PhaseTracker()
    time = 0.0
    state = tracker.update(time, 0.20, 0.20)
    for step in range(1, 40):
        time += 10
        state = tracker.update(time, 0.20 + step * 0.001, 0.20)
    assert state == PhaseTracker.RISING
    for _ in range(120):  # a long, almost flat stretch
        time += 10
        state = tracker.update(time, 0.2395, 0.20)
        assert state != PhaseTracker.WAITING
    tracker.set_baseline(0.23)  # the mark moved up: the rise over it is smaller than the minimum
    time += 10
    assert tracker.update(time, 0.2395, 0.23) != PhaseTracker.WAITING
