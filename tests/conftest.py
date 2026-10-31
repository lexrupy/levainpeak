import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--gui", action="store_true", default=False,
        help="also run the tests that open real windows on the desktop and take the focus",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "gui: opens real windows on the desktop; run with --gui")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--gui"):
        return
    skip = pytest.mark.skip(reason="opens real windows on the desktop; run with --gui")
    for item in items:
        if "gui" in item.keywords:
            item.add_marker(skip)
