import math

from levainpeak.rate import growth_rate


def series(per_hour_percent, hours=2.5, step=60.0, start_volume=0.3):
    """Readings of an exponential that gains ``per_hour_percent`` per hour."""
    k = math.log(1 + per_hour_percent / 100.0) / 3600.0
    return [(i * step, start_volume * math.exp(k * i * step)) for i in range(int(hours * 3600 / step) + 1)]


def test_a_steady_exponential_reads_its_hourly_rate_and_a_steady_pace():
    points = series(10.0)
    rate = growth_rate(points, points[-1][0])
    assert abs(rate.per_hour - 10.0) < 0.05 and rate.trend == "estavel"
    assert rate.text() == "+10,0%/h →"


def test_the_arrow_shows_whether_the_pace_is_rising_or_falling():
    slowing = [(t, 0.3 * math.exp(0.1 * math.log(1 + t / 3600) * 0 + (0.12 * t / 3600 - 0.02 * (t / 3600) ** 2))) for t, _ in series(0.0)]
    rate = growth_rate(slowing, slowing[-1][0])
    assert rate.trend == "desce" and rate.text().endswith("↘")
    speeding = [(t, 0.3 * math.exp(0.02 * (t / 3600) ** 2 + 0.02 * t / 3600)) for t, _ in series(0.0)]
    assert growth_rate(speeding, speeding[-1][0]).text().endswith("↗")


def test_a_falling_volume_has_a_negative_rate():
    points = series(-5.0)
    assert growth_rate(points, points[-1][0]).per_hour < -4.9


def test_too_few_or_too_short_readings_give_no_rate_and_no_arrow_without_the_earlier_hour():
    assert growth_rate(series(10.0, hours=0.2), 0.2 * 3600) is None
    points = series(10.0, hours=1.0)
    rate = growth_rate(points, points[-1][0])
    assert rate is not None and rate.trend is None and not rate.text().endswith(("↗", "↘", "→"))
