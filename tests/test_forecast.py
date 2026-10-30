import pytest

from levainpeak.forecast import DoublingForecaster, literature_factor


def feed(forecaster, rate_per_min, temperature, minutes=10, start=0.0, volume=0.3):
    for step in range(minutes * 6 + 1):
        t = start + step * 10
        forecaster.add(t, volume + rate_per_min * t / 60, temperature)


def test_needs_enough_history():
    forecaster = DoublingForecaster()
    forecaster.add(0, 0.3, 24)
    assert forecaster.minutes_until(0.3, 0.6) is None


def test_plain_trend_without_temperature():
    forecaster = DoublingForecaster()
    feed(forecaster, 0.001, None)
    current = 0.3 + 0.001 * 10
    forecast = forecaster.minutes_until(current, 0.6)
    assert forecast.uses_temperature is False
    assert forecast.minutes == pytest.approx((0.6 - current) / 0.001, rel=0.01)


def test_constant_temperature_keeps_observed_pace():
    cool, warm = DoublingForecaster(), DoublingForecaster()
    feed(cool, 0.001, 20)
    feed(warm, 0.001, 28)
    assert warm.minutes_until(0.31, 0.6).minutes == pytest.approx(
        cool.minutes_until(0.31, 0.6).minutes, rel=0.01
    )


def test_warming_up_shortens_the_forecast_versus_constant_temperature():
    steady, warming = DoublingForecaster(), DoublingForecaster()
    feed(steady, 0.001, 24)
    for step in range(61):
        warming.add(step * 10, 0.3 + 0.001 * step * 10 / 60, 24 + step * 0.05)
    assert warming.minutes_until(0.31, 0.6).minutes < steady.minutes_until(0.31, 0.6).minutes


def test_cooling_down_lengthens_forecast_versus_constant_temperature():
    steady, cooling = DoublingForecaster(), DoublingForecaster()
    feed(steady, 0.001, 26)
    for step in range(61):
        cooling.add(step * 10, 0.3 + 0.001 * step * 10 / 60, 26 - step * 0.05)
    assert cooling.minutes_until(0.31, 0.6).minutes > steady.minutes_until(0.31, 0.6).minutes


def test_no_rising_trend_returns_none():
    forecaster = DoublingForecaster()
    feed(forecaster, 0.0, 24)
    assert forecaster.minutes_until(0.3, 0.6) is None


def test_literature_table_follows_the_yeast_curve():
    assert literature_factor(24) == pytest.approx(1.0)
    assert literature_factor(25) == pytest.approx(1.095)
    assert literature_factor(None) == 1.0
    # peak near 27 °C, then a sharp collapse toward zero by about 36 °C
    assert literature_factor(27) == max(literature_factor(t / 2) for t in range(8, 90))
    assert literature_factor(33) < literature_factor(24) < literature_factor(27)
    assert 0 < literature_factor(40) <= 0.03
    assert literature_factor(-5) == literature_factor(4) > 0


def test_uses_table_when_session_lacks_data():
    forecaster = DoublingForecaster()
    feed(forecaster, 0.001, 24)
    assert forecaster.minutes_until(0.31, 0.6).model == "tabela"


def test_learns_sensitivity_from_session_history():
    """Rate doubles every 10 °C in the data; the table's slope is different."""
    forecaster = DoublingForecaster()
    t, volume = 0.0, 0.3
    for step in range(12 * 60):  # 2 hours at 10 s, temperature drifting 20 -> 30 °C
        temperature = 20 + 10 * step / (12 * 60)
        forecaster.add(t, volume, temperature)
        volume += 0.0005 * 2 ** ((temperature - 24) / 10) / 6
        t += 10
    forecast = forecaster.minutes_until(volume, volume + 0.1)
    assert forecast.model == "sessão"
    # at 30 °C the rate is 0.0005 * 2**0.6 per minute
    assert forecast.minutes == pytest.approx(0.1 / (0.0005 * 2 ** 0.6), rel=0.1)


def test_flat_temperature_cannot_fit_session_model():
    forecaster = DoublingForecaster()
    feed(forecaster, 0.001, 25, minutes=90)
    assert forecaster.minutes_until(0.4, 0.6).model == "tabela"
