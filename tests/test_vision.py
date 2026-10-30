import numpy as np

from levainpeak.vision import _robust_wall


def test_robust_wall_ignores_rows_that_grabbed_other_edges():
    rows = np.arange(300, dtype=np.float32)
    wall = 100 + 0.05 * rows
    noisy = wall.copy()
    noisy[40:90] += 35  # wood-grain streak grabbed the edge
    noisy[200:230] -= 30
    fitted = _robust_wall(noisy)
    assert np.abs(fitted - wall).max() < 2.0


def test_robust_wall_keeps_a_conical_slope():
    rows = np.arange(200, dtype=np.float32)
    fitted = _robust_wall(80 + 0.04 * rows)
    assert fitted[-1] - fitted[0] > 7


def test_robust_wall_falls_back_to_vertical_when_the_fit_is_steep():
    rows = np.arange(300, dtype=np.float32)
    rng = np.random.default_rng(3)
    edge = 150 - 0.2 * rows + rng.normal(0, 3, 300)  # implausibly steep for glass
    fitted = _robust_wall(edge)
    assert np.ptp(fitted) == 0
