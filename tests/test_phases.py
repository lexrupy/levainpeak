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
    states = run(tracker, lambda m: 0.4 - 0.002 * m, 30, start=1800)
    assert states[-1] == PhaseTracker.FALLING
