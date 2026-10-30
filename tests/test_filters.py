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
