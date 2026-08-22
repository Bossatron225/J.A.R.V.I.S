"""Tests for reading (and setting) volume and brightness.

Jarvis could already change both but had no way to read either, so he told the
user he was "unable to report the current level". These tests pin the reading
path, and — just as importantly — pin the behaviour when a level genuinely
can't be read (Intel Macs, external monitors, Linux without brightnessctl),
since a wrong number is worse than an honest "I can't see it".
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import computer_settings as cs


class _Result:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(cs, "_OS", "Darwin")


# ── volume ─────────────────────────────────────────────────────────────────

def test_volume_is_read_from_osascript(darwin, monkeypatch):
    def fake_run(cmd, **kw):
        script = cmd[-1]
        return _Result("42\n" if "output volume" in script else "false\n")

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    assert cs.get_volume() == {"level": 42, "muted": False}


def test_muted_state_is_reported(darwin, monkeypatch):
    def fake_run(cmd, **kw):
        return _Result("25\n" if "output volume" in cmd[-1] else "true\n")

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    assert cs.get_volume()["muted"] is True


def test_volume_read_failure_returns_none_not_a_guess(darwin, monkeypatch):
    """A wrong number spoken confidently is worse than admitting ignorance."""
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert cs.get_volume() == {"level": None, "muted": None}


def test_unparseable_volume_output_does_not_raise(darwin, monkeypatch):
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result("not a number"))
    assert cs.get_volume()["level"] is None


# ── brightness ─────────────────────────────────────────────────────────────

_IOREG_SAMPLE = '''
  +-o AppleARMBacklight  <class AppleARMBacklight>
      "IODisplayParameters" = {"bgsc"={"min"=0,"max"=65536,"value"=65536},"brightness"={"min"=0,"max"=65536,"value"=32768}}
'''


def test_brightness_is_parsed_from_ioreg(darwin, monkeypatch):
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result(_IOREG_SAMPLE))
    assert cs.get_brightness() == 50


def test_brightness_falls_back_to_the_intel_service(darwin, monkeypatch):
    """Apple Silicon exposes AppleARMBacklight; Intel Macs use a different
    service. Only finding one of them must not mean 'unreadable'."""
    def fake_run(cmd, **kw):
        if "AppleARMBacklight" in cmd:
            return _Result("")  # not present on this machine
        return _Result(_IOREG_SAMPLE.replace("AppleARMBacklight", "AppleBacklightDisplay"))

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    assert cs.get_brightness() == 50


def test_external_display_reports_none_rather_than_zero(darwin, monkeypatch):
    """An external monitor exposes no backlight service. Reporting 0% would be
    a lie; None lets the caller say it can't see it."""
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result("no matches"))
    assert cs.get_brightness() is None


def test_zero_max_does_not_divide_by_zero(darwin, monkeypatch):
    weird = '"brightness"={"min"=0,"max"=0,"value"=0}'
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result(weird))
    assert cs.get_brightness() is None


def test_brightness_percentage_is_clamped(darwin, monkeypatch):
    over = '"brightness"={"min"=0,"max"=100,"value"=999}'
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result(over))
    assert cs.get_brightness() == 100


# ── setting brightness ─────────────────────────────────────────────────────

def test_brightness_set_steps_toward_the_target(darwin, monkeypatch):
    """No supported macOS API sets brightness directly, so it steps the media
    keys and re-reads. The point of the test is that it converges and stops."""
    state = {"level": 20}
    presses = []

    def fake_press(key):
        presses.append(key)
        state["level"] += 6 if key == "brightnessup" else -6

    monkeypatch.setattr(cs, "get_brightness", lambda: state["level"])
    monkeypatch.setattr(cs.pyautogui, "press", fake_press)
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)

    cs.brightness_set(80)
    assert presses and set(presses) == {"brightnessup"}
    assert abs(state["level"] - 80) <= cs._BRIGHTNESS_STEP_PERCENT


def test_brightness_set_goes_down_too(darwin, monkeypatch):
    state = {"level": 90}
    monkeypatch.setattr(cs, "get_brightness", lambda: state["level"])
    monkeypatch.setattr(cs.pyautogui, "press",
                        lambda k: state.__setitem__("level", state["level"] - 6))
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)

    cs.brightness_set(30)
    assert abs(state["level"] - 30) <= cs._BRIGHTNESS_STEP_PERCENT


def test_brightness_set_is_bounded_when_the_display_never_responds(darwin, monkeypatch):
    """A display that ignores the keys must not spin forever."""
    presses = []
    monkeypatch.setattr(cs, "get_brightness", lambda: 10)  # never moves
    monkeypatch.setattr(cs.pyautogui, "press", lambda k: presses.append(k))
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)

    cs.brightness_set(100)
    assert len(presses) <= 24


def test_brightness_set_refuses_when_level_is_unreadable(darwin, monkeypatch):
    presses = []
    monkeypatch.setattr(cs, "get_brightness", lambda: None)
    monkeypatch.setattr(cs.pyautogui, "press", lambda k: presses.append(k))

    out = cs.brightness_set(50)
    assert presses == [], "must not blindly mash keys with no feedback"
    assert "can't read" in out


# ── the spoken report ──────────────────────────────────────────────────────

def test_report_states_both_levels(darwin, monkeypatch):
    monkeypatch.setattr(cs, "get_volume", lambda: {"level": 40, "muted": False})
    monkeypatch.setattr(cs, "get_brightness", lambda: 70)
    out = cs.report_levels()
    assert "40%" in out and "70%" in out


def test_report_mentions_mute(darwin, monkeypatch):
    monkeypatch.setattr(cs, "get_volume", lambda: {"level": 25, "muted": True})
    monkeypatch.setattr(cs, "get_brightness", lambda: 50)
    assert "muted" in cs.report_levels().lower()


def test_report_is_honest_about_unreadable_brightness(darwin, monkeypatch):
    monkeypatch.setattr(cs, "get_volume", lambda: {"level": 40, "muted": False})
    monkeypatch.setattr(cs, "get_brightness", lambda: None)
    out = cs.report_levels()
    assert "40%" in out
    assert "readable" in out.lower()


def test_report_addresses_the_user_as_sir(darwin, monkeypatch):
    monkeypatch.setattr(cs, "get_volume", lambda: {"level": 40, "muted": False})
    monkeypatch.setattr(cs, "get_brightness", lambda: 70)
    assert "sir" in cs.report_levels()


# ── dispatcher wiring ──────────────────────────────────────────────────────

@pytest.fixture
def stub_levels(monkeypatch):
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    monkeypatch.setattr(cs, "get_volume", lambda: {"level": 33, "muted": False})
    monkeypatch.setattr(cs, "get_brightness", lambda: 66)


@pytest.mark.parametrize("action", ["get_levels", "get_volume", "get_brightness", "levels"])
def test_read_actions_reach_the_user(stub_levels, action):
    """Regression: the generic dispatcher discarded return values and replied
    'Done: get_levels.', which told the user nothing."""
    out = cs.computer_settings({"action": action})
    assert "33%" in out and "66%" in out
    assert not out.startswith("Done:")


def test_brightness_set_action_passes_the_value(monkeypatch):
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    seen = {}

    def fake_set(v):
        seen["value"] = v
        return "ok, sir"

    monkeypatch.setattr(cs, "brightness_set", fake_set)
    assert cs.computer_settings({"action": "brightness_set", "value": "35"}) == "ok, sir"
    assert seen["value"] == 35


def test_ordinary_actions_still_report_done(monkeypatch):
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    called = []
    monkeypatch.setitem(cs.ACTION_MAP, "volume_up", lambda: called.append(1))
    assert cs.computer_settings({"action": "volume_up"}) == "Done: volume_up."
    assert called == [1]


def test_non_darwin_reads_report_unavailable_rather_than_shelling_out(monkeypatch):
    monkeypatch.setattr(cs, "_OS", "Windows")
    assert cs.get_volume() == {"level": None, "muted": None}
    assert cs.get_brightness() is None
