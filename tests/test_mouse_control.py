"""Tests for pointer control.

Two real bugs sit behind these:

1. `move` with no coordinates defaulted x and y to 0, parking the pointer in
   the top-left corner and reporting "Mouse → (0, 0)" as success. The agent log
   shows exactly that: `{'action': 'move'}` → `Mouse → (0, 0)`.

2. Nothing ever checked that the pointer moved. Without Accessibility
   permission macOS accepts a pyautogui move and performs none of it — no
   error, no exception — so "I have clicked it for you, sir" was said about
   clicks that never happened.

Plus the reason it aimed wrongly even when it did move: coordinates came from
asking a vision model to read pixels off a Retina screenshot. Real window
geometry replaces the guess.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from actions import computer_control as cc


class _FakePyAutoGUI:
    """Pointer that may or may not obey, like a Mac without Accessibility."""

    def __init__(self, obeys=True, start=(10, 10), size=(1470, 956)):
        self.obeys = obeys
        self._pos = start
        self._size = size
        self.clicks = []

    def position(self):
        return self._pos

    def size(self):
        return self._size

    def moveTo(self, x, y, duration=0):
        if self.obeys:
            self._pos = (x, y)

    def click(self, x=None, y=None, button="left", clicks=1):
        self.clicks.append((x, y, button, clicks))
        if self.obeys and x is not None:
            self._pos = (x, y)


@pytest.fixture
def gui(monkeypatch):
    fake = _FakePyAutoGUI()
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cc, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)
    return fake


# ── the silent-failure guard ───────────────────────────────────────────────

def test_a_move_that_does_nothing_is_reported_not_claimed(monkeypatch):
    """The whole point: an ignored move must not read as success."""
    fake = _FakePyAutoGUI(obeys=False, start=(10, 10))
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cc, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    out = cc._move(700, 450)
    assert "Could not move" in out
    assert "Accessibility" in out, "should name the actual likely cause"


def test_a_move_that_works_is_reported_as_success(gui):
    assert cc._move(700, 450) == "Mouse → (700, 450)"


def test_a_click_that_never_lands_is_reported(monkeypatch):
    fake = _FakePyAutoGUI(obeys=False, start=(10, 10))
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cc, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    out = cc._click(700, 450)
    assert "Could not click" in out and "Accessibility" in out


def test_a_click_that_lands_is_reported_as_success(gui):
    assert "Clicked (700, 450)" in cc._click(700, 450)


def test_small_pointer_drift_is_tolerated(monkeypatch):
    """Hardware can land a pixel off; that is not a failure."""
    fake = _FakePyAutoGUI()
    fake.moveTo = lambda x, y, duration=0: setattr(fake, "_pos", (x + 1, y - 1))
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cc, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    assert cc._move(700, 450) == "Mouse → (700, 450)"


# ── never default to (0, 0) ────────────────────────────────────────────────

def test_move_without_coordinates_or_a_target_refuses(gui, monkeypatch):
    """Regression: this used to move to (0, 0) and call it done."""
    monkeypatch.setattr(cc, "find_window", lambda q: None)
    out = cc.computer_control({"action": "move"})
    assert "need either coordinates or a description" in out
    assert gui.position() == (10, 10), "the pointer must not have moved"


def test_move_with_an_unfindable_target_refuses(gui, monkeypatch):
    monkeypatch.setattr(cc, "find_window", lambda q: None)
    monkeypatch.setattr(cc, "_screen_find", lambda d: None)
    out = cc.computer_control({"action": "move", "description": "the flux capacitor"})
    assert "couldn't find" in out
    assert gui.position() == (10, 10)


# ── real window geometry ───────────────────────────────────────────────────

_WINDOWS = [
    {"owner": "Claude", "title": "Claude", "x": 5, "y": 33, "width": 992, "height": 800},
    {"owner": "Code", "title": "Jarvis improvements — Mark-L-main",
     "x": 314, "y": 33, "width": 1152, "height": 900},
    {"owner": "Python", "title": "JARVIS — MARK XLIX",
     "x": 245, "y": 111, "width": 980, "height": 732},
]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(cc, "list_windows", lambda: list(_WINDOWS))


def test_a_window_centre_is_computed_correctly():
    assert cc.window_centre(_WINDOWS[0]) == (501, 433)


def test_an_app_is_found_by_exact_name(windows):
    assert cc.find_window("Claude")["owner"] == "Claude"


def test_an_app_is_found_by_a_spoken_phrase(windows):
    """The user says 'Claude code'; the process is called 'Code'."""
    assert cc.find_window("Claude code")["owner"] == "Code"


def test_a_window_is_found_by_its_title(windows):
    assert cc.find_window("Mark-L-main")["owner"] == "Code"


def test_matching_is_case_insensitive(windows):
    assert cc.find_window("CLAUDE")["owner"] == "Claude"


def test_an_unknown_target_returns_nothing(windows):
    assert cc.find_window("Microsoft Excel") is None


def test_an_empty_query_returns_nothing(windows):
    assert cc.find_window("") is None
    assert cc.find_window(None) is None


def test_the_largest_matching_window_wins(monkeypatch):
    """A palette or inspector must not beat the main window."""
    monkeypatch.setattr(cc, "list_windows", lambda: [
        {"owner": "Code", "title": "palette", "x": 0, "y": 0, "width": 200, "height": 150},
        {"owner": "Code", "title": "main", "x": 0, "y": 0, "width": 1152, "height": 900},
    ])
    assert cc.find_window("Code")["title"] == "main"


def test_moving_to_a_window_uses_its_real_centre(gui, windows):
    out = cc.computer_control({"action": "move", "description": "Claude"})
    assert gui.position() == (501, 433)
    assert "Claude" in out


def test_moving_to_a_window_names_what_it_found(gui, windows):
    out = cc.computer_control({"action": "move", "description": "Claude code"})
    assert "Mark-L-main" in out, "should say which window it actually chose"


def test_window_geometry_is_preferred_over_the_vision_search(gui, windows, monkeypatch):
    """Window geometry is exact; the vision search is a guess. Asking a model
    to read pixels off a Retina screenshot returned x=959 for something in the
    top-right of a 2940px-wide image."""
    monkeypatch.setattr(cc, "_screen_find",
                        lambda d: pytest.fail("must not guess when the window is known"))
    cc.computer_control({"action": "move", "description": "Claude"})
    assert gui.position() == (501, 433)


def test_the_vision_search_is_still_used_for_non_windows(gui, monkeypatch):
    monkeypatch.setattr(cc, "list_windows", lambda: [])
    monkeypatch.setattr(cc, "_screen_find", lambda d: (300, 200))
    out = cc.computer_control({"action": "move", "description": "the save button"})
    assert gui.position() == (300, 200)
    assert "approximate" in out, "a guessed location should be flagged as such"


# ── screen_click ───────────────────────────────────────────────────────────

def test_screen_click_on_a_window_clicks_its_centre(gui, windows):
    out = cc.computer_control({"action": "screen_click", "description": "Claude"})
    assert gui.clicks and gui.clicks[0][:2] == (501, 433)
    assert "Clicked" in out


def test_screen_click_reports_a_pointer_that_never_moved(monkeypatch, windows):
    fake = _FakePyAutoGUI(obeys=False)
    monkeypatch.setattr(cc, "pyautogui", fake)
    monkeypatch.setattr(cc, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(cc.time, "sleep", lambda *_: None)

    out = cc.computer_control({"action": "screen_click", "description": "Claude"})
    assert "Could not click" in out


def test_screen_click_refuses_when_it_cannot_find_the_target(gui, monkeypatch):
    monkeypatch.setattr(cc, "list_windows", lambda: [])
    monkeypatch.setattr(cc, "_screen_find", lambda d: None)
    out = cc.computer_control({"action": "screen_click", "description": "nothing at all"})
    assert "couldn't find" in out
    assert gui.clicks == []


# ── the Retina scaling that made every guess wrong ─────────────────────────

def test_vision_coordinates_are_scaled_out_of_screenshot_space():
    """The screenshot is 2x the click space on this Mac, so raw screenshot
    pixels land at double the intended position."""
    from actions.computer_use import scale_to_click_space
    assert scale_to_click_space(2940, 1912, (2940, 1912), (1470, 956)) == (1469, 955)
    assert scale_to_click_space(1000, 600, (2940, 1912), (1470, 956)) == (500, 300)


def test_the_prompt_describes_the_image_actually_sent():
    """It used to quote pyautogui.size() while sending a 2x screenshot, so the
    coordinates coming back were meaningless."""
    source = (REPO_ROOT / "actions" / "computer_control.py").read_text(encoding="utf-8")
    body = source.split("def _screen_find", 1)[1][:1600]
    assert "iw, ih = img.size" in body
    assert "{iw}×{ih}" in body
    assert "w, h  = pyautogui.size()" not in body
