import math

from levainpeak.rate import growth_rate


def pieces(*parts, step=30.0):
    """Readings of an exponential whose pace changes: parts are (minutes, percent per hour)."""
    points, t, volume = [], 0.0, 0.3
    for minutes, percent in parts:
        k = math.log(1 + percent / 100.0) / 3600.0
        for _ in range(int(minutes * 60 / step)):
            points.append((t, volume))
            t += step
            volume *= math.exp(k * step)
    points.append((t, volume))
    return points


def test_a_steady_exponential_reads_its_hourly_rate_with_a_level_arrow():
    points = pieces((90, 10.0))
    rate = growth_rate(points, points[-1][0])
    assert abs(rate.per_hour - 10.0) < 0.05 and rate.trend == "estavel"
    assert rate.text() == "+10,0%/h →"


def test_the_value_is_the_last_15_minutes_and_the_arrow_compares_it_with_the_last_hour():
    slowing = pieces((60, 10.0), (15, 4.0))
    rate = growth_rate(slowing, slowing[-1][0])
    assert abs(rate.per_hour - 4.0) < 0.1 and rate.trend == "desce" and rate.text().endswith("↘")
    speeding = pieces((60, 4.0), (15, 10.0))
    rate = growth_rate(speeding, speeding[-1][0])
    assert abs(rate.per_hour - 10.0) < 0.1 and rate.text().endswith("↗")


def test_a_small_difference_from_the_hours_pace_keeps_the_arrow_level():
    points = pieces((60, 11.0), (15, 9.0))
    assert growth_rate(points, points[-1][0]).trend == "estavel"


def test_a_falling_volume_has_a_negative_rate():
    points = pieces((90, -5.0))
    assert growth_rate(points, points[-1][0]).per_hour < -4.9


def test_too_few_readings_give_no_rate_and_no_arrow_without_the_hours_readings():
    assert growth_rate(pieces((5, 10.0)), 5 * 60) is None
    points = pieces((14, 10.0))  # 15 minutes of readings but not an hour: the pace, no arrow
    rate = growth_rate(points, points[-1][0])
    assert rate is not None and rate.trend is None and not rate.text().endswith(("↗", "↘", "→"))
