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
    fitted = _robust_wall(80 + 0.2 * rows)
    assert fitted[-1] - fitted[0] > 35
