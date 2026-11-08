from levainpeak.filters import OutlierGate


def test_discards_isolated_spike_and_keeps_trusted_value():
    gate = OutlierGate(max_jump=2.0)
    assert gate.accept(25.0)
    assert not gate.accept(75.0)
    assert gate.trusted == 25.0
    assert gate.accept(25.4)


def test_adopts_new_level_after_consistent_rereads():
    gate = OutlierGate(max_jump=0.1, confirmations=3)
    gate.accept(0.30)
    assert not gate.accept(0.60)
    assert not gate.accept(0.61)
    assert gate.accept(0.60)
    assert gate.trusted == 0.60


def test_scattered_outliers_never_get_adopted():
    gate = OutlierGate(max_jump=0.1, confirmations=3)
    gate.accept(0.30)
    for value in (0.9, 0.5, 0.8, 0.6, 0.95):
        assert not gate.accept(value)
    assert gate.trusted == 0.30


def test_despike_removes_a_dip_that_comes_back():
    from levainpeak.filters import despike

    series = [0.30] * 100 + [0.26] * 36 + [0.30] * 100
    cleaned = despike(series)
    assert max(abs(value - 0.30) for value in cleaned) < 0.005


def test_despike_keeps_a_lasting_step_and_a_ramp():
    from levainpeak.filters import despike

    step = [0.30] * 100 + [0.36] * 100
    assert despike(step) == step
    ramp = [0.30 + 0.0004 * index for index in range(200)]
    assert despike(ramp) == ramp


def test_despike_leaves_the_edges_alone():
    from levainpeak.filters import despike

    series = [0.30] * 100 + [0.20] * 5  # the latest samples cannot be judged yet
    assert despike(series)[-5:] == [0.20] * 5


def test_valid_after_recovery_invalidates_a_dip_that_is_overtaken():
    from levainpeak.filters import valid_after_recovery

    series = [0.29] * 30 + [0.23] * 40 + [0.32] * 30
    flags = valid_after_recovery(series)
    assert flags[:30] == [True] * 30
    assert flags[30:70] == [False] * 40
    assert flags[70:] == [True] * 30


def test_valid_after_recovery_keeps_growth_noise_and_real_decline():
    from levainpeak.filters import valid_after_recovery

    ramp = [0.20 + 0.001 * i for i in range(100)]
    assert all(valid_after_recovery(ramp))
    plateau = [0.30 + (0.004 if i % 2 else -0.004) for i in range(100)]
    assert all(valid_after_recovery(plateau))
    decline = [0.40] * 30 + [0.40 - 0.002 * i for i in range(60)]
    assert all(valid_after_recovery(decline))


def test_valid_after_recovery_trusts_the_newest_readings_until_overruled():
    from levainpeak.filters import valid_after_recovery

    series = [0.30] * 50 + [0.20] * 5
    assert valid_after_recovery(series)[-5:] == [True] * 5


def test_valid_after_recovery_ignores_an_isolated_high_spike():
    from levainpeak.filters import valid_after_recovery

    series = [0.18, 0.18, 0.18, 0.216, 0.18] + [0.18] * 70 + [0.27] * 20
    assert all(valid_after_recovery(series))


def test_a_big_jump_needs_many_more_confirmations_than_a_small_one():
    from levainpeak.filters import OutlierGate

    gate = OutlierGate(max_jump=0.25, confirmations=6, big_jump=1.0, big_confirmations=40)
    gate.trusted = 25.6
    assert not any(gate.accept(20.8) for _ in range(39))  # a steady misread of 5 as 0 is still refused
    assert gate.accept(20.8) and gate.trusted == 20.8  # ...but a real, lasting change gets in
    gate.trusted = 25.6
    assert not any(gate.accept(26.2) for _ in range(5)) and gate.accept(26.2)  # a small step: the usual 6


def test_a_big_temperature_jump_that_holds_for_minutes_is_still_rejected():
    from levainpeak.analysis import BIG_TEMPERATURE_CONFIRMATIONS, BIG_TEMPERATURE_JUMP_C
    from levainpeak.filters import OutlierGate

    gate = OutlierGate(
        max_jump=3.0, confirmations=3,
        big_jump=BIG_TEMPERATURE_JUMP_C, big_confirmations=BIG_TEMPERATURE_CONFIRMATIONS,
    )
    gate.accept(24.7)
    assert not any(gate.accept(43.6) for _ in range(BIG_TEMPERATURE_CONFIRMATIONS - 1))
    assert gate.accept(43.6)  # a swapped thermometer is adopted in the end
    assert gate.accept(46.0)  # and small steps after it are fine


def test_a_sudden_change_of_brightness_holds_for_the_hold_time_and_a_second_one_extends_it():
    from levainpeak.filters import LampWatch

    watch = LampWatch()

    def run(start, end, brightness):
        return [watch.update(float(t), brightness, 600) for t in range(start, end, 2)]

    assert not any(run(0, 60, 100.0))
    assert watch.update(60.0, 141.0, 600)  # the lamp comes on
    assert all(run(62, 300, 141.0))
    assert run(300, 400, 100.0)[-1]  # and goes off: held again from there
    assert all(run(400, 900, 100.0))
    assert not any(run(1000, 1100, 100.0))


def test_slow_brightness_drift_and_a_zero_hold_never_hold():
    from levainpeak.filters import LampWatch

    watch = LampWatch()
    assert not any(watch.update(float(t), 100.0 + t * 0.01, 600) for t in range(0, 600, 2))
    other = LampWatch()
    assert not any(other.update(float(t), 100.0 if t < 40 else 150.0, 0) for t in range(0, 100, 2))


def test_a_light_that_keeps_changing_cannot_hold_the_volume_for_hours():
    from levainpeak.filters import LampWatch

    watch = LampWatch()
    held_late = []
    for second in range(0, 2 * 3600, 10):
        brightness = 60.0 if (second // 60) % 2 else 100.0  # flips every minute
        held = watch.update(float(second), brightness, 600.0)
        if second > 3600 + 1800:
            held_late.append(held)
    assert not all(held_late)


def test_a_lamp_that_moved_for_good_is_told_apart_from_one_that_went_off_and_on():
    from levainpeak.filters import LampWatch

    for final, stays in ((100.0, False), (150.0, True)):
        watch = LampWatch()
        now = 0.0
        for _ in range(12):
            watch.update(now, 100.0, 60.0)
            now += 10
        assert watch.update(now, 150.0, 60.0)  # the picture brightens: held
        for _ in range(20):
            now += 10
            watch.update(now, 150.0, 60.0)
        assert watch.stayed_changed(final) is stays


def test_a_stretch_held_dead_that_ends_in_a_leap_is_drawn_as_a_line():
    from levainpeak.filters import bridge_holds

    times = [float(i * 10) for i in range(60)]
    values = [0.40 + 0.0002 * i for i in range(20)] + [0.45] * 30 + [0.50 + 0.0002 * i for i in range(10)]
    cleaned = bridge_holds(times, values)
    held = cleaned[20:50]
    assert all(b >= a for a, b in zip(held, held[1:])) and held[0] < 0.43 and held[-1] > 0.48  # a ramp now
    # A short repeat, or a long one that ends flat, stays.
    assert bridge_holds([0.0, 10.0, 20.0, 30.0], [0.4, 0.4, 0.4, 0.4]) == [0.4, 0.4, 0.4, 0.4]
    assert bridge_holds(times[:6], [0.40, 0.45, 0.45, 0.45, 0.452, 0.455]) == [0.40, 0.45, 0.45, 0.45, 0.452, 0.455]
