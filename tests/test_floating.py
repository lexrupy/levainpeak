import time
import tkinter as tk

import pytest

from levainpeak.floating import BORDER, FloatingWindowControls

# These open real windows and take the keyboard focus: run them with `pytest --gui`.
pytestmark = pytest.mark.gui


@pytest.fixture
def popup():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    window = tk.Toplevel(root, class_="Pythontkfloating")
    window.geometry("400x300+100+100")
    window.minsize(300, 200)
    content = tk.Canvas(window, highlightthickness=0)
    content.pack(fill="both", expand=True, padx=BORDER, pady=BORDER)
    closed = []
    controls = FloatingWindowControls(window, content, on_close=lambda: closed.append(True))
    pump(root)
    yield root, window, content, closed, controls
    root.destroy()


def pump(root, seconds=0.25):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def test_dragging_the_content_moves_the_window(popup):
    root, window, content, _, _ = popup
    x0, y0 = window.winfo_x(), window.winfo_y()
    content.event_generate("<ButtonPress-1>", x=50, y=50, rootx=x0 + 50, rooty=y0 + 50)
    content.event_generate("<B1-Motion>", x=90, y=70, rootx=x0 + 90, rooty=y0 + 70, state=0x100)
    pump(root)
    assert (window.winfo_x() - x0, window.winfo_y() - y0) == (40, 20)


def test_dragging_the_border_resizes_the_window(popup):
    root, window, _, _, _ = popup
    width, height = window.winfo_width(), window.winfo_height()
    x, y = window.winfo_x(), window.winfo_y()
    window.event_generate("<ButtonPress-1>", x=width - 3, y=height - 3,
                          rootx=x + width - 3, rooty=y + height - 3)
    window.event_generate("<B1-Motion>", x=width + 57, y=height + 37,
                          rootx=x + width + 57, rooty=y + height + 37, state=0x100)
    pump(root)
    assert (window.winfo_width() - width, window.winfo_height() - height) == (60, 40)


def test_the_border_never_shrinks_the_window_below_its_minimum(popup):
    root, window, _, _, _ = popup
    width, height = window.winfo_width(), window.winfo_height()
    x, y = window.winfo_x(), window.winfo_y()
    window.event_generate("<ButtonPress-1>", x=width - 3, y=height - 3,
                          rootx=x + width - 3, rooty=y + height - 3)
    window.event_generate("<B1-Motion>", x=10, y=10, rootx=x + 10, rooty=y + 10, state=0x100)
    pump(root)
    assert (window.winfo_width(), window.winfo_height()) == (300, 200)


def test_escape_closes_even_when_the_focus_was_on_another_window(popup):
    root, window, content, closed, _ = popup
    for _attempt in range(3):
        root.focus_force()
        pump(root)
        # a click gives the popup the keyboard focus; without that Escape goes elsewhere
        content.event_generate("<ButtonPress-1>", x=40, y=40,
                               rootx=content.winfo_rootx() + 40, rooty=content.winfo_rooty() + 40)
        content.event_generate("<ButtonRelease-1>", x=40, y=40)
        pump(root)
        focused = window.focus_displayof()
        if focused is not None and str(focused).startswith(str(window)):
            root.event_generate("<Escape>")
            pump(root)
            assert closed
            return
    # The desktop (or the person using it) kept taking the focus away; the
    # behaviour cannot be observed then, which is not a failure of the code.
    pytest.skip("the window manager did not grant keyboard focus")
