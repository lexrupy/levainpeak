from levainpeak.settings import ARTIFACT_MINUTES, PEAK_MINUTES, Settings


def test_the_phase_times_have_defaults_and_stay_in_range():
    defaults = Settings().clamped()
    assert (defaults.peak_minutes, defaults.artifact_minutes) == (10, 12)
    wild = Settings(peak_minutes=0, artifact_minutes=9999).clamped()
    assert wild.peak_minutes == PEAK_MINUTES[0] and wild.artifact_minutes == ARTIFACT_MINUTES[1]
