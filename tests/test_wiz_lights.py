import asyncio
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


def test_run_blocking_works_with_no_event_loop():
    async def _coro():
        return "done"

    assert wiz._run_blocking(_coro()) == "done"


def test_run_blocking_works_inside_a_running_event_loop():
    """The real bug: Jarvis calls tools from inside its live asyncio loop, where the
    old asyncio.run() raised "asyncio.run() cannot be called from a running event
    loop". Every WiZ command from Jarvis returned "WiZ control failed" because of
    this, while the identical call from a sync script succeeded — which is why it
    looked like a broken driver."""

    async def _coro():
        return "done"

    async def _main():
        return wiz._run_blocking(_coro())

    assert asyncio.run(_main()) == "done"


def test_run_blocking_propagates_exceptions_from_the_worker_thread():
    """Errors raised on the worker thread must surface to the caller so
    wiz_lights() can report them, rather than being swallowed."""

    async def _boom():
        raise ValueError("kaboom")

    async def _main():
        return wiz._run_blocking(_boom())

    with pytest.raises(ValueError, match="kaboom"):
        asyncio.run(_main())


def test_wiz_lights_is_delegated_to_the_mac():
    """WiZ bulbs are on the home LAN, unreachable from the VPS datacenter, so the
    tool has to run on the Mac via the local-worker relay."""
    from local_worker import ACTION_HANDLERS, LOCAL_ACTIONS

    assert "wiz_lights" in LOCAL_ACTIONS
    assert ACTION_HANDLERS["wiz_lights"] == ("actions.wiz_lights", "wiz_lights")
