import os
import threading
import time

import numpy as np
import pytest

from actions import camera_session as camera_session_module
from actions.camera_session import (
    CameraSession,
    RETRY_COOLDOWN_SECONDS,
    TARGET_FRAME_HEIGHT,
    TARGET_FRAME_WIDTH,
    apply_target_resolution,
    get_camera_session,
)

cv2 = pytest.importorskip("cv2")


def _encode_frame(fill: int) -> bytes:
    """A real, tiny JPEG so cv2.imdecode genuinely round-trips in the reader thread."""
    img = np.full((8, 8, 3), fill, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class _FakeStream:
    """A real os.pipe()-backed file object — gives the reader thread's blocking
    .read(n) calls genuine blocking semantics, same as a real subprocess pipe."""

    def __init__(self):
        r_fd, w_fd = os.pipe()
        self.read_file = os.fdopen(r_fd, "rb")
        self.write_file = os.fdopen(w_fd, "wb")

    def send_frame(self, jpeg_bytes: bytes) -> None:
        self.write_file.write(len(jpeg_bytes).to_bytes(4, "big"))
        self.write_file.write(jpeg_bytes)
        self.write_file.flush()

    def close_write_end(self) -> None:
        """Simulates the subprocess exiting — the reader thread's next read hits EOF."""
        self.write_file.close()


class _FakeProcess:
    def __init__(self):
        self.stdout_pipe = _FakeStream()
        self.stderr_pipe = _FakeStream()
        self.stdout = self.stdout_pipe.read_file
        self.stderr = self.stderr_pipe.read_file
        self._terminated = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return 0 if self._terminated.is_set() else None

    def terminate(self):
        self.terminate_calls += 1
        self._terminated.set()
        self.stdout_pipe.close_write_end()
        self.stderr_pipe.close_write_end()

    def kill(self):
        self.kill_calls += 1
        self._terminated.set()

    def wait(self, timeout=None):
        if not self._terminated.wait(timeout=timeout):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


@pytest.fixture(autouse=True)
def _isolate_sessions():
    camera_session_module._sessions.clear()
    yield
    camera_session_module._sessions.clear()


def _install_fake_popen(monkeypatch) -> list:
    processes: list[_FakeProcess] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(camera_session_module.subprocess, "Popen", _fake_popen)
    return processes


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── apply_target_resolution (unchanged direct cv2.VideoCapture callers) ─────

class _FakeCapture:
    def __init__(self):
        self.set_calls: list[tuple] = []

    def set(self, prop, value):
        self.set_calls.append((prop, value))
        return True


def test_apply_target_resolution_requests_4k():
    cap = _FakeCapture()
    apply_target_resolution(cap)
    assert (cv2.CAP_PROP_FRAME_WIDTH, TARGET_FRAME_WIDTH) in cap.set_calls
    assert (cv2.CAP_PROP_FRAME_HEIGHT, TARGET_FRAME_HEIGHT) in cap.set_calls
    assert TARGET_FRAME_WIDTH == 3840
    assert TARGET_FRAME_HEIGHT == 2160


def test_apply_target_resolution_is_best_effort_on_capture_without_set():
    class _NoSetCapture:
        pass

    apply_target_resolution(_NoSetCapture())  # must not raise
    apply_target_resolution(None)  # must not raise


# ── CameraSession (persistent subprocess) ───────────────────────────────────

def test_get_frame_returns_decoded_frame_once_subprocess_sends_one(monkeypatch):
    processes = _install_fake_popen(monkeypatch)
    session = CameraSession(camera_index=0)

    session.get_frame()  # triggers the subprocess launch
    assert len(processes) == 1

    processes[0].stdout_pipe.send_frame(_encode_frame(42))

    assert _wait_until(lambda: session.get_frame() is not None)
    frame = session.get_frame()
    assert frame.shape == (8, 8, 3)

    session.release()


def test_get_frame_reuses_running_subprocess_across_calls(monkeypatch):
    processes = _install_fake_popen(monkeypatch)
    session = CameraSession(camera_index=0)

    session.get_frame()
    session.get_frame()
    session.get_frame()

    assert len(processes) == 1  # only launched once across repeated calls
    session.release()


def test_request_exclusive_terminates_subprocess_and_monitor_reopens_after(monkeypatch):
    processes = _install_fake_popen(monkeypatch)
    session = CameraSession(camera_index=0)

    session.get_frame()
    assert len(processes) == 1

    with session.request_exclusive():
        assert processes[0].terminate_calls == 1
        assert session._proc is None

    # Reopening after a deliberate close must not be cooldown-gated — only a
    # genuine crash should ever incur the retry cooldown.
    assert _wait_until(lambda: len(processes) == 2, timeout=2.0)


def test_release_terminates_subprocess_and_clears_latest_frame(monkeypatch):
    processes = _install_fake_popen(monkeypatch)
    session = CameraSession(camera_index=0)

    session.get_frame()
    processes[0].stdout_pipe.send_frame(_encode_frame(1))
    assert _wait_until(lambda: session.get_frame() is not None)

    session.release()

    assert processes[0].terminate_calls == 1
    assert session._proc is None
    with session._frame_lock:
        assert session._latest_frame is None


def test_crashed_subprocess_is_cooldown_gated_before_relaunch(monkeypatch):
    processes = _install_fake_popen(monkeypatch)
    fake_now = [1000.0]
    monkeypatch.setattr(camera_session_module.time, "monotonic", lambda: fake_now[0])

    session = CameraSession(camera_index=0)
    session.get_frame()
    assert len(processes) == 1

    # Simulate the subprocess dying unexpectedly (not via release()/request_exclusive()).
    processes[0]._terminated.set()
    processes[0].stdout_pipe.close_write_end()
    assert _wait_until(lambda: session._proc_dead is True)

    # Still within the cooldown window — must not relaunch yet.
    fake_now[0] += RETRY_COOLDOWN_SECONDS / 2
    session.get_frame()
    assert len(processes) == 1

    # Cooldown elapsed — should relaunch now.
    fake_now[0] += RETRY_COOLDOWN_SECONDS
    session.get_frame()
    assert len(processes) == 2

    session.release()


def test_get_camera_session_returns_same_instance_per_index():
    a = get_camera_session(0)
    b = get_camera_session(0)
    c = get_camera_session(1)

    assert a is b
    assert a is not c
