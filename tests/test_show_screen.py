"""Tests for pushing the Mac's screen to the dashboard.

The bug this replaces: "show me my Mac screen" from the phone routed to
computer_settings action=screenshot, which saved a PNG to the Mac's own
desktop. Jarvis then reported "A screenshot has been saved to your desktop,
sir" — entirely true, and entirely useless to someone holding a phone in
another room.

So the tests care about two things: the image reaches the person who asked,
and every failure path says so plainly instead of claiming success.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dashboard.server import DashboardServer


# ── the screenshot store ───────────────────────────────────────────────────

class _Store(DashboardServer):
    """Just the screenshot store, without standing up a web server."""

    def __init__(self):  # noqa: D107 - deliberately skips DashboardServer.__init__
        self._screenshots = {}


@pytest.fixture
def store():
    return _Store()


def test_a_screenshot_can_be_stored_and_read_back(store):
    shot_id = store.put_screenshot(b"IMAGE", "image/jpeg", "Mac screen")
    shot = store.get_screenshot(shot_id)
    assert shot["data"] == b"IMAGE"
    assert shot["mime"] == "image/jpeg"
    assert shot["label"] == "Mac screen"


def test_ids_are_unguessable(store):
    """The id is the only thing between a URL and a picture of the whole
    screen, so it must not be sequential."""
    ids = {store.put_screenshot(b"x") for _ in range(5)}
    assert len(ids) == 5
    assert all(len(i) >= 16 for i in ids)


def test_screenshots_expire(store):
    shot_id = store.put_screenshot(b"IMAGE")
    store._screenshots[shot_id]["created_at"] = time.time() - store.SCREENSHOT_TTL_SECONDS - 1
    assert store.get_screenshot(shot_id) is None


def test_only_a_bounded_number_are_retained(store):
    """A picture of the user's screen is not something to accumulate in memory
    indefinitely, and a burst of requests must not grow without bound."""
    ids = [store.put_screenshot(bytes([i])) for i in range(store.SCREENSHOT_MAX_HELD + 3)]
    assert len(store._screenshots) <= store.SCREENSHOT_MAX_HELD
    assert store.get_screenshot(ids[0]) is None, "oldest should have been dropped"
    assert store.get_screenshot(ids[-1]) is not None, "newest must survive"


def test_an_unknown_id_returns_nothing(store):
    assert store.get_screenshot("nope") is None


def test_screenshots_are_never_written_to_disk(store, tmp_path, monkeypatch):
    """Held in memory on purpose — a capture of the whole screen should not be
    left lying around in a folder."""
    monkeypatch.chdir(tmp_path)
    store.put_screenshot(b"IMAGE")
    assert list(tmp_path.iterdir()) == []


# ── the tool: it must not claim success it didn't achieve ──────────────────

class _FakeDashboard:
    def __init__(self):
        self.broadcasts = []
        self.stored = []

    def put_screenshot(self, data, mime="image/jpeg", label="screen"):
        self.stored.append((data, mime, label))
        return "shot123"

    async def broadcast(self, msg):
        self.broadcasts.append(msg)


class _Jarvis:
    """Minimal stand-in exposing only what _send_screen_to_dashboard touches."""

    def __init__(self, dashboard=None, bridge=None):
        self._dashboard = dashboard
        self._remote_bridge = bridge

    def _current_time_text(self):
        return "13:10"


def _bind(jarvis):
    import main
    return main.JarvisLive._send_screen_to_dashboard.__get__(jarvis)


@pytest.fixture
def send(monkeypatch):
    def _make(dashboard=None, bridge=None):
        return _bind(_Jarvis(dashboard, bridge))
    return _make


def test_no_dashboard_is_reported_honestly(send):
    out = asyncio.run(send(dashboard=None)({}))
    assert "no dashboard" in out.lower()
    assert "sir" in out


def test_a_rejection_from_the_mac_is_reported_not_papered_over(send):
    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            return {"status": "rejected", "reason": "not a local worker action"}

    dash = _FakeDashboard()
    out = asyncio.run(send(dashboard=dash, bridge=_Bridge())({}))
    assert "couldn't capture" in out
    assert "not a local worker action" in out
    assert dash.broadcasts == [], "must not announce an image it never got"


def test_an_empty_capture_is_reported(send):
    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            return {"status": "completed", "result": {}}

    dash = _FakeDashboard()
    out = asyncio.run(send(dashboard=dash, bridge=_Bridge())({}))
    assert "nothing came back" in out
    assert dash.broadcasts == []


def test_undecodable_image_data_is_reported(send):
    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            return {"status": "completed", "result": {"image_b64": "!!!not base64!!!"}}

    dash = _FakeDashboard()
    out = asyncio.run(send(dashboard=dash, bridge=_Bridge())({}))
    assert "unreadable" in out
    assert dash.broadcasts == []


def test_a_capture_exception_is_reported(send):
    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            raise RuntimeError("mac offline")

    out = asyncio.run(send(dashboard=_FakeDashboard(), bridge=_Bridge())({}))
    assert "couldn't capture" in out and "mac offline" in out


def test_a_successful_capture_is_stored_and_broadcast(send):
    import base64

    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            assert action == "capture_screen"
            return {"status": "completed", "result": {
                "image_b64": base64.b64encode(b"JPEGBYTES").decode(),
                "mime_type": "image/jpeg", "label": "screen"}}

    dash = _FakeDashboard()
    out = asyncio.run(send(dashboard=dash, bridge=_Bridge())({}))

    assert dash.stored == [(b"JPEGBYTES", "image/jpeg", "screen")]
    assert len(dash.broadcasts) == 1
    msg = dash.broadcasts[0]
    assert msg["type"] == "screen_image"
    assert msg["shot_id"] == "shot123"
    assert "dashboard" in out and "sir" in out


def test_the_broadcast_does_not_carry_the_image_itself(send):
    """The image goes by reference: broadcast history is replayed to every new
    client, so embedding ~140KB would bloat memory and re-show an old capture
    of the user's screen on any device that connects later."""
    import base64

    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            return {"status": "completed", "result": {
                "image_b64": base64.b64encode(b"X" * 5000).decode()}}

    dash = _FakeDashboard()
    asyncio.run(send(dashboard=dash, bridge=_Bridge())({}))
    serialised = repr(dash.broadcasts[0])
    assert len(serialised) < 500
    assert "image_b64" not in serialised


def test_target_arguments_are_passed_through(send):
    import base64
    seen = {}

    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            seen.update(payload)
            return {"status": "completed", "result": {
                "image_b64": base64.b64encode(b"x").decode()}}

    asyncio.run(send(dashboard=_FakeDashboard(), bridge=_Bridge())(
        {"target_type": "app", "app_name": "Safari"}))
    assert seen["target_type"] == "app"
    assert seen["app_name"] == "Safari"


def test_it_defaults_to_the_whole_screen(send):
    import base64
    seen = {}

    class _Bridge:
        def request_local_action(self, action, payload, timeout):
            seen.update(payload)
            return {"status": "completed", "result": {
                "image_b64": base64.b64encode(b"x").decode()}}

    asyncio.run(send(dashboard=_FakeDashboard(), bridge=_Bridge())({}))
    assert seen["target_type"] == "screen"


def test_it_captures_locally_when_there_is_no_bridge(send, monkeypatch):
    """On the Mac itself there is nothing to delegate to."""
    import base64
    import actions.screen_processor as sp

    monkeypatch.setattr(sp, "capture_targeted_visual_b64",
                        lambda p: {"image_b64": base64.b64encode(b"LOCAL").decode(),
                                   "mime_type": "image/jpeg", "label": "screen"})
    dash = _FakeDashboard()
    out = asyncio.run(send(dashboard=dash, bridge=None)({}))
    assert dash.stored[0][0] == b"LOCAL"
    assert "dashboard" in out


# ── the client contract ────────────────────────────────────────────────────

def test_the_dashboard_page_renders_pushed_screenshots():
    page = (REPO_ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    assert "screen_image" in page, "no handler for the pushed-screenshot message"
    assert "_onScreenImage" in page
    assert "/screen/" in page, "must fetch the image by reference"


def test_the_page_handles_an_expired_capture():
    page = (REPO_ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    assert "expired" in page.lower()


def test_the_server_route_requires_a_token():
    source = (REPO_ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    route = source.split('@app.get("/screen/{shot_id}")', 1)[1][:900]
    assert "_is_token_valid" in route
    assert "401" in route
    assert "no-store" in route, "a picture of the screen must not be cached"
