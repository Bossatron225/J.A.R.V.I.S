import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import system_health as sh


class _FakeUI:
    pass


class _FakeThread:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _FakeTask:
    def __init__(self, done: bool = False):
        self._done = done

    def done(self):
        return self._done


class _FakeJarvis:
    """Stands in for a running JarvisLive. Defaults describe a fully healthy
    process; each test perturbs exactly one thing."""

    def __init__(self, **overrides):
        now = time.monotonic()
        self.ui = _FakeUI()
        self.session = object()
        self._last_live_activity_ts = now
        self._local_worker_last_poll_ts = now
        self._visitor_monitor_thread = _FakeThread(alive=True)
        self._visitor_watch_cfg = {"camera_index": 0}
        self._background_tasks = {_FakeTask(), _FakeTask()}
        self._gate = False
        for k, v in overrides.items():
            setattr(self, k, v)

    def _local_speech_gate_active(self):
        return self._gate


def _named(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


@pytest.fixture(autouse=True)
def _healthy_camera(monkeypatch):
    """Default the camera probe to 'streaming' so tests that aren't about the
    camera don't have to stub it."""
    from actions import camera_session

    class _FakeSession:
        def last_frame_age_seconds(self):
            return 1.0

    monkeypatch.setattr(camera_session, "get_camera_session", lambda idx=0: _FakeSession())
    monkeypatch.setattr("actions.visitor_log.is_watch_active", lambda: True)
    monkeypatch.delenv("JARVIS_VPS_URL", raising=False)


def test_healthy_process_reports_ok():
    report = sh.collect_health(_FakeJarvis())
    assert report["ok"] is True


def test_missing_live_session_is_a_failure():
    report = sh.collect_health(_FakeJarvis(session=None))
    assert report["ok"] is False
    assert _named(report, "live_session")["ok"] is False


def test_stale_live_session_is_flagged_even_though_object_exists():
    """The real outage: session object present, socket dead, every dashboard
    command silently dropped. Liveness said fine; traffic age is what catches it."""
    stale = time.monotonic() - (sh.LIVE_ACTIVITY_MAX_AGE + 60)
    report = sh.collect_health(_FakeJarvis(_last_live_activity_ts=stale))
    assert _named(report, "live_session")["ok"] is False
    assert "dead socket" in _named(report, "live_session")["detail"]


def test_camera_never_delivering_a_frame_is_a_failure(monkeypatch):
    """The Nanny-cam outage: thread alive and 'engaged' for 8+ minutes while
    the camera never opened once."""
    from actions import camera_session

    class _NeverDelivers:
        def last_frame_age_seconds(self):
            return None

    monkeypatch.setattr(camera_session, "get_camera_session", lambda idx=0: _NeverDelivers())

    report = sh.collect_health(_FakeJarvis())
    check = _named(report, "camera_frames")
    assert check["ok"] is False
    assert "NEVER" in check["detail"]


def test_stalled_camera_is_a_failure(monkeypatch):
    from actions import camera_session

    class _Stalled:
        def last_frame_age_seconds(self):
            return sh.CAMERA_FRAME_MAX_AGE + 30

    monkeypatch.setattr(camera_session, "get_camera_session", lambda idx=0: _Stalled())

    assert _named(sh.collect_health(_FakeJarvis()), "camera_frames")["ok"] is False


def test_camera_not_marked_failed_when_disengaged(monkeypatch):
    """Disengaging the Nanny-cam is a user choice, not a fault."""
    monkeypatch.setattr("actions.visitor_log.is_watch_active", lambda: False)
    report = sh.collect_health(_FakeJarvis())
    assert _named(report, "camera_frames")["applicable"] is False
    assert report["ok"] is True


def test_camera_not_marked_failed_while_biometric_lock_holds_it_off():
    report = sh.collect_health(_FakeJarvis(_gate=True))
    assert _named(report, "camera_frames")["applicable"] is False
    assert report["ok"] is True


def test_dead_visitor_monitor_thread_is_a_failure():
    report = sh.collect_health(_FakeJarvis(_visitor_monitor_thread=_FakeThread(alive=False)))
    assert _named(report, "visitor_monitor")["ok"] is False


def test_stalled_vps_worker_link_is_a_failure(monkeypatch):
    """The garbage-collected poller: object still present, polling stopped."""
    monkeypatch.setenv("JARVIS_VPS_URL", "http://example.invalid:8000")
    stale = time.monotonic() - (sh.LOCAL_WORKER_MAX_AGE + 60)
    report = sh.collect_health(_FakeJarvis(_local_worker_last_poll_ts=stale))
    check = _named(report, "vps_worker_link")
    assert check["ok"] is False
    assert "stopped polling" in check["detail"]


def test_worker_link_is_na_without_vps_configured():
    report = sh.collect_health(_FakeJarvis())
    assert _named(report, "vps_worker_link")["applicable"] is False


def test_headless_vps_does_not_report_camera_as_broken():
    """The VPS has no camera by design — that must read as N/A, not a fault,
    or the watchdog would cry wolf on every VPS health cycle."""
    class _HeadlessUI:
        pass

    jarvis = _FakeJarvis()
    jarvis.ui = _HeadlessUI()
    report = sh.collect_health(jarvis)
    assert _named(report, "camera_frames")["applicable"] is False
    assert _named(report, "visitor_monitor")["applicable"] is False
    assert report["ok"] is True


def test_format_health_summarizes_failures_first():
    report = sh.collect_health(_FakeJarvis(session=None))
    text = sh.format_health(report)
    assert "degraded" in text.lower()
    assert "live_session" in text


def test_format_health_reports_nominal_when_ok():
    text = sh.format_health(sh.collect_health(_FakeJarvis()))
    assert "nominal" in text.lower()


def test_report_includes_git_revision_for_drift_detection():
    report = sh.collect_health(_FakeJarvis())
    assert report["revision"]
