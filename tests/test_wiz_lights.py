import pytest

from actions import wiz_lights as wiz


class _FakeState:
    """Stands in for pywizlight's PilotParser."""

    def __init__(self, brightness=128):
        self._brightness = brightness

    def get_state(self):
        return True

    def get_brightness(self):
        return self._brightness

    def get_rgb(self):
        return (None, None, None)

    def get_colortemp(self):
        return 2700

    def get_scene(self):
        return None


def test_unwrap_state_accepts_list_from_pywizlight_0_6():
    """pywizlight >= 0.6 returns updateState() as a list of PilotParser rather than
    a bare one. Every WiZ status call died with "'list' object has no attribute
    'get_state'" until _unwrap_state handled it — this is that regression."""
    state = _FakeState()
    assert wiz._unwrap_state([state]) is state


def test_unwrap_state_accepts_bare_parser_from_older_pywizlight():
    """The Mac and the VPS can run different pywizlight versions, so the old
    single-object shape has to keep working too."""
    state = _FakeState()
    assert wiz._unwrap_state(state) is state


def test_unwrap_state_skips_none_entries_for_dual_head_bulbs():
    """Dual-head bulbs report a 2-element list; a head that didn't answer is None."""
    state = _FakeState()
    assert wiz._unwrap_state([None, state]) is state


@pytest.mark.parametrize("empty", [[], [None], None])
def test_unwrap_state_raises_when_no_state_reported(empty):
    """The status caller catches per-light exceptions and reports them, so raising
    here surfaces a real failure instead of an AttributeError further down."""
    with pytest.raises(RuntimeError):
        wiz._unwrap_state(empty)


@pytest.mark.parametrize("percent", [1, 2, 5, 10, 25, 50, 75, 100])
def test_brightness_percent_round_trips(percent):
    """Brightness goes in as 1-100% but the bulb reports raw 0-255; status used to
    print the raw number, so asking for 2% read back as "brightness=5"."""
    raw = wiz._brightness_to_wiz(percent)
    assert wiz._wiz_to_brightness(raw) == pytest.approx(percent, abs=1)


def test_wiz_to_brightness_handles_none():
    assert wiz._wiz_to_brightness(None) is None
