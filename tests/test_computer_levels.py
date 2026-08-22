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


class _FakeDisplayServices:
    """Stands in for the DisplayServices private framework."""

    def __init__(self, level=0.5, get_rc=0, set_rc=0):
        self.level = level
        self.get_rc = get_rc
        self.set_rc = set_rc
        self.argtypes = None
        self.restype = None

    # ctypes function objects are attributes; mimic just enough of that shape.
    @property
    def DisplayServicesGetBrightness(self):
        def _get(display, out_ptr):
            if self.get_rc == 0:
                out_ptr._obj.value = self.level
            return self.get_rc
        _get.argtypes = _get.restype = None
        return _get

    @property
    def DisplayServicesSetBrightness(self):
        def _set(display, value):
            if self.set_rc == 0:
                self.level = value.value if hasattr(value, "value") else float(value)
            return self.set_rc
        _set.argtypes = _set.restype = None
        return _set


@pytest.fixture
def fake_ds(darwin, monkeypatch):
    ds = _FakeDisplayServices()
    monkeypatch.setattr(cs, "_display_services", lambda: (ds, 1))
    return ds


def test_brightness_is_read_from_display_services(fake_ds):
    fake_ds.level = 0.62
    assert cs.get_brightness() == 62


def test_brightness_read_is_clamped(fake_ds):
    fake_ds.level = 1.9
    assert cs.get_brightness() == 100


def test_brightness_falls_back_to_ioreg_when_the_framework_is_missing(darwin, monkeypatch):
    """Older/locked-down machines may not expose the private framework; the
    raw backlight value is a last resort rather than nothing at all."""
    monkeypatch.setattr(cs, "_display_services", lambda: (None, None))
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result(_IOREG_SAMPLE))
    assert cs.get_brightness() == 50


def test_ioreg_fallback_tries_the_intel_service_too(darwin, monkeypatch):
    monkeypatch.setattr(cs, "_display_services", lambda: (None, None))

    def fake_run(cmd, **kw):
        if "AppleARMBacklight" in cmd:
            return _Result("")  # not present on this machine
        return _Result(_IOREG_SAMPLE.replace("AppleARMBacklight", "AppleBacklightDisplay"))

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    assert cs.get_brightness() == 50


def test_external_display_reports_none_rather_than_zero(darwin, monkeypatch):
    """An external monitor exposes no backlight control. Reporting 0% would be
    a lie; None lets the caller say it can't see it."""
    monkeypatch.setattr(cs, "_display_services", lambda: (None, None))
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result("no matches"))
    assert cs.get_brightness() is None


def test_failed_read_falls_through_rather_than_reporting_zero(darwin, monkeypatch):
    monkeypatch.setattr(cs, "_display_services", lambda: (_FakeDisplayServices(get_rc=1), 1))
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result("no matches"))
    assert cs.get_brightness() is None


def test_zero_max_does_not_divide_by_zero(darwin, monkeypatch):
    monkeypatch.setattr(cs, "_display_services", lambda: (None, None))
    weird = '"brightness"={"min"=0,"max"=0,"value"=0}'
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _Result(weird))
    assert cs.get_brightness() is None


# ── setting brightness ─────────────────────────────────────────────────────

def test_brightness_set_writes_the_exact_level(fake_ds):
    out = cs.brightness_set(85)
    assert fake_ds.level == pytest.approx(0.85)
    assert "85%" in out


def test_brightness_set_clamps_out_of_range_values(fake_ds):
    cs.brightness_set(500)
    assert fake_ds.level == pytest.approx(1.0)
    cs.brightness_set(-20)
    assert fake_ds.level == pytest.approx(0.0)


def test_brightness_set_reports_a_refusal_rather_than_claiming_success(darwin, monkeypatch):
    monkeypatch.setattr(cs, "_display_services", lambda: (_FakeDisplayServices(set_rc=1), 1))
    assert "refused" in cs.brightness_set(50)


def test_brightness_set_says_so_when_control_is_unavailable(darwin, monkeypatch):
    monkeypatch.setattr(cs, "_display_services", lambda: (None, None))
    assert "can't control" in cs.brightness_set(50)


def test_relative_nudges_move_the_real_control(fake_ds):
    """Regression: brightness_up/down sent System Events key code 144/145,
    which exits 0 on modern macOS and does nothing at all."""
    fake_ds.level = 0.50
    cs.brightness_up()
    assert fake_ds.level == pytest.approx(0.60)
    cs.brightness_down()
    assert fake_ds.level == pytest.approx(0.50)


def test_nudge_does_nothing_when_brightness_is_unreadable(darwin, monkeypatch):
    monkeypatch.setattr(cs, "get_brightness", lambda: None)
    calls = []
    monkeypatch.setattr(cs, "brightness_set", lambda v: calls.append(v))
    cs.brightness_up()
    assert calls == []


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
