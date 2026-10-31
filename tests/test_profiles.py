import numpy as np

from levainpeak.imaging import is_infrared
from levainpeak.profiles import DAY, INFRARED, ProfileSelector
from levainpeak.sessions import SessionLog


def color_frame():
    frame = np.zeros((200, 300, 3), np.uint8)
    frame[:] = (120, 90, 60)  # clearly colored
    return frame


def gray_frame():
    return np.full((200, 300, 3), 130, np.uint8)


def test_infrared_frames_are_exactly_gray_and_daylight_ones_are_not():
    assert is_infrared(gray_frame())
    assert not is_infrared(color_frame())
    assert not is_infrared(None)


def test_selector_follows_the_picture_after_a_few_frames():
    selector = ProfileSelector(confirm_frames=3)
    assert selector.update(color_frame()) is DAY
    assert [selector.update(gray_frame()).name for _ in range(3)] == ["dia", "dia", "infravermelho"]
    assert selector.update(gray_frame()) is INFRARED


def test_a_stray_frame_does_not_switch_the_profile():
    selector = ProfileSelector(confirm_frames=3)
    selector.update(color_frame())
    selector.update(gray_frame())
    selector.update(gray_frame())
    assert selector.update(color_frame()) is DAY
    assert selector.update(gray_frame()) is DAY  # the count started over


def test_vessel_bounds_survive_saving_and_resuming(tmp_path):
    log = SessionLog(tmp_path)
    log.start(name="teste")
    log.save_regions((1, 2, 3, 4), (5, 6, 7, 8))
    log.save_vessel_bounds({"dia": (313, 930)})
    path = log.path
    log.record(0.0, type("M", (), {"volume_fraction": 0.3, "initial_volume_fraction": 0.2, "growth_factor": 1.5})(), "Crescendo")
    log.stop()
    resumed = SessionLog(tmp_path)
    resumed.resume(path)
    assert resumed.vessel_bounds == {"dia": (313, 930)}
    assert resumed.regions["pot"] == (1, 2, 3, 4)
    resumed.stop()


def _analyzer():
    from levainpeak.analysis import ContainerAnalyzer

    return ContainerAnalyzer(stream=object())  # never started: no camera or thread


def _measurement(top, bottom, confidence):
    from levainpeak.vision import ContainerMeasurement

    return ContainerMeasurement(
        left=10, right=100, top=top, bottom=bottom, surface_y=top + 50,
        volume_fraction=0.3, confidence=confidence,
    )


def test_infrared_reuses_the_vessel_bounds_measured_by_day():
    analyzer = _analyzer()
    analyzer.vessel_bounds = {"dia": (313, 930)}
    analyzer.profile = DAY
    assert analyzer.effective_bounds() is None  # daylight detects them itself
    analyzer.profile = INFRARED
    assert analyzer.effective_bounds() == (313, 930)


def test_infrared_without_any_daylight_measurement_detects_from_the_picture():
    analyzer = _analyzer()
    analyzer.profile = INFRARED
    assert analyzer.effective_bounds() is None


def test_bounds_are_learned_only_from_confident_daylight_measurements():
    analyzer = _analyzer()
    analyzer.profile = INFRARED
    analyzer._learn_vessel_bounds(_measurement(599, 939, 0.9))
    assert analyzer.vessel_bounds == {}  # infrared never teaches the bounds
    analyzer.profile = DAY
    analyzer._learn_vessel_bounds(_measurement(313, 930, 0.4))
    assert analyzer.vessel_bounds == {}  # not confident enough
    analyzer._learn_vessel_bounds(_measurement(313, 930, 0.9))
    assert analyzer.vessel_bounds == {"dia": (313, 930)}
    analyzer._learn_vessel_bounds(_measurement(314, 931, 0.9))
    assert analyzer.vessel_bounds == {"dia": (313, 930)}  # a 1 px wobble is ignored
