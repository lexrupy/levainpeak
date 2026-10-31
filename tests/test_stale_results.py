from levainpeak.analysis import ContainerAnalyzer


def test_a_result_measured_with_a_replaced_region_is_marked_stale():
    analyzer = ContainerAnalyzer(stream=object())
    analyzer.roi = (10, 10, 200, 300)
    old_key = analyzer.region_key()
    analyzer._publish((1, None, 24.0), old_key)  # the analyzer thread finishes a pass with the old region
    analyzer.roi = (50, 20, 400, 500)  # ...and the user replaces the region meanwhile
    result = analyzer.latest_result()
    assert result == (1, None, 24.0)
    assert analyzer.latest_key != analyzer.region_key()  # the window drops it: it is not about the new region


def test_a_result_measured_with_the_current_regions_is_not_stale():
    analyzer = ContainerAnalyzer(stream=object())
    analyzer.outline = ((1, 2), (3, 4), (5, 6), (7, 8))
    analyzer.temperature_quad = ((1, 1), (9, 1), (9, 5), (1, 5))
    analyzer._publish((2, None, None), analyzer.region_key())
    assert analyzer.latest_result() == (2, None, None)
    assert analyzer.latest_key == analyzer.region_key()


def test_changing_the_contour_or_the_face_corners_changes_the_key():
    analyzer = ContainerAnalyzer(stream=object())
    key = analyzer.region_key()
    analyzer.outline = ((1, 2), (3, 4), (5, 6), (7, 8))
    assert analyzer.region_key() != key
    key = analyzer.region_key()
    analyzer.temperature_quad = ((1, 1), (9, 1), (9, 5), (1, 5))
    assert analyzer.region_key() != key
