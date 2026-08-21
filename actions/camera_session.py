import subprocess
import sys
import threading
import time
from contextlib import contextmanager

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None


# Requested capture resolution for every camera-using feature (person/visitor
# detection, continuous monitoring, vision capture). This is a *request*, not
# a requirement: UVC/AVFoundation camera drivers clamp an unsupported request
# down to the device's nearest supported mode rather than failing, so asking
# for 4K is safe even on a camera that only supports 1080p or lower — it just
# gets that camera's best available resolution instead.
TARGET_FRAME_WIDTH = 3840
TARGET_FRAME_HEIGHT = 2160

# JPEG quality for the persistent monitoring stream — this feeds face
# detection/recognition, not human viewing, so a touch below full quality is
# fine and keeps the per-frame pipe/decode cost more reasonable at 4K.
MONITOR_JPEG_QUALITY = 85

# Self-throttle inside the subprocess to roughly match the continuous
# monitor's own poll cadence (main.py's _visitor_monitor_loop waits ~0.4s
# between cycles) — no point JPEG-encoding faster than anyone reads.
MONITOR_CAPTURE_INTERVAL_SECONDS = 0.4

# After the persistent subprocess dies unexpectedly (camera unplugged, claimed
# by another app, etc.), wait this long before trying to relaunch it — avoids
# a tight respawn-loop if the camera stays unavailable for a while.
RETRY_COOLDOWN_SECONDS = 5.0

_LENGTH_PREFIX_BYTES = 4


def apply_target_resolution(capture) -> None:
    """Ask an opened cv2.VideoCapture for the target resolution. Best-effort —
    swallow errors so a camera/backend that rejects the property set still
    works at whatever resolution it already opened with. Used by callers that
    open their own short-lived cv2.VideoCapture directly (not through
    CameraSession, which holds the camera open via a subprocess instead —
    see module docstring on CameraSession for why)."""
    if capture is None:
        return
    try:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_FRAME_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_FRAME_HEIGHT)
    except Exception:
        pass


# Persistent camera-streaming subprocess. macOS only completes the
# AVFoundation camera-permission handshake when it owns a plain,
# single-threaded process's main thread — inside this app (Qt event loop +
# asyncio + extra worker threads), that handshake silently fails no matter
# how it's retried (cv2.VideoCapture(...).isOpened() just returns False,
# forever, with no exception). This subprocess is exactly the "plain process"
# shape that reliably works, and unlike a one-shot grab it stays alive,
# capturing continuously, so the parent can hold a persistent stream instead
# of re-paying the ~1.4s camera-open cost (and a fresh subprocess spawn) on
# every poll.
_PERSISTENT_CAMERA_SUBPROCESS_SRC = r"""
import sys, time
import cv2

index, width, height, quality = (int(a) for a in sys.argv[1:5])
interval = float(sys.argv[5])

cap = cv2.VideoCapture(index)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
if not cap.isOpened():
    sys.stderr.write("CAMERA_OPEN_FAILED\n")
    sys.stderr.flush()
    sys.exit(1)

out = sys.stdout.buffer
while True:
    ok, frame = cap.read()
    if not ok:
        continue
    ok2, jbuf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok2:
        continue
    data = jbuf.tobytes()
    try:
        out.write(len(data).to_bytes(4, "big"))
        out.write(data)
        out.flush()
    except (BrokenPipeError, OSError):
        break
    time.sleep(interval)
"""


class CameraSession:
    """Owns one physical camera by holding it open in a persistent subprocess
    (see _PERSISTENT_CAMERA_SUBPROCESS_SRC above for why a subprocess, rather
    than opening cv2.VideoCapture in-process, is required here). A background
    reader thread continuously decodes frames from the subprocess and keeps
    only the latest one, so get_frame() is always a cheap, non-blocking read.
    One-off consumers (biometric checks, vision-Q&A capture) use
    request_exclusive() instead of touching the camera directly, so they
    always get sole access to the device rather than racing a running monitor
    for it — the shared lock below is the single coordination point for both
    cases.
    """

    def __init__(self, camera_index: int = 0):
        self.camera_index = camera_index
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._latest_frame = None
        self._proc_dead = True
        self._last_crash_ts = 0.0
        # When the reader thread last decoded a real frame. This is the only
        # honest "is the camera actually working" signal — a live thread and a
        # running subprocess both stayed true during the outage where no frame
        # was ever produced, so health checks must look at frame age, not
        # liveness.
        self._last_frame_ts = 0.0

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        try:
            while True:
                header = _read_exact(stdout, _LENGTH_PREFIX_BYTES)
                if header is None:
                    break
                length = int.from_bytes(header, "big")
                payload = _read_exact(stdout, length)
                if payload is None:
                    break
                if np is None or cv2 is None:
                    continue
                arr = np.frombuffer(payload, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    with self._frame_lock:
                        self._latest_frame = frame
        except Exception:
            pass
        finally:
            self._proc_dead = True
            self._last_crash_ts = time.monotonic()

    @staticmethod
    def _stderr_loop(proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                if line:
                    print(f"[Camera] subprocess: {line.decode(errors='replace').strip()}")
        except Exception:
            pass

    def _open_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None and not self._proc_dead:
            return
        if self._proc_dead and (time.monotonic() - self._last_crash_ts) < RETRY_COOLDOWN_SECONDS:
            return  # still cooling down after a recent crash/open-failure
        self._close_locked()

        proc = subprocess.Popen(
            [
                sys.executable, "-c", _PERSISTENT_CAMERA_SUBPROCESS_SRC,
                str(self.camera_index), str(TARGET_FRAME_WIDTH), str(TARGET_FRAME_HEIGHT),
                str(MONITOR_JPEG_QUALITY), str(MONITOR_CAPTURE_INTERVAL_SECONDS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._proc = proc
        self._proc_dead = False
        self._reader_thread = threading.Thread(target=self._reader_loop, args=(proc,), daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, args=(proc,), daemon=True)
        self._stderr_thread.start()

    def _close_locked(self) -> None:
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        with self._frame_lock:
            self._latest_frame = None
        # A deliberate close (release()/request_exclusive()) also makes the
        # reader thread hit EOF, which stamps _last_crash_ts as if this were a
        # real crash — reset it here so the retry cooldown only ever applies
        # to genuine unexpected deaths, never to an intentional shutdown.
        self._proc_dead = True
        self._last_crash_ts = 0.0

    def get_frame(self):
        """Continuous-monitor fast path — call only from the monitor's own loop.
        Launches the streaming subprocess lazily on first use (or after a
        crash, once the retry cooldown elapses), then just returns whichever
        frame the reader thread most recently decoded."""
        with self._lock:
            self._open_locked()
        with self._frame_lock:
            return self._latest_frame

    def release(self) -> None:
        """Release the device (and turn off the indicator light) when nothing
        needs it — call when continuous monitoring stops."""
        with self._lock:
            self._close_locked()

    @contextmanager
    def request_exclusive(self):
        """One-off consumers use this instead of opening the camera
        directly. Closes the session's subprocess first (if a continuous
        monitor has it open) so the caller has sole access to the physical
        device for the duration of the block; the monitor lazily reopens on
        its next get_frame() call."""
        with self._lock:
            self._close_locked()
            yield self


def _read_exact(stream, size: int) -> bytes | None:
    """Read exactly `size` bytes from a blocking stream, or None on EOF."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


_sessions: dict[int, CameraSession] = {}
_sessions_lock = threading.Lock()


def get_camera_session(camera_index: int = 0) -> CameraSession:
    """Every caller for a given camera index must share the same CameraSession
    instance — otherwise each would have its own lock and the coordination this
    class exists for wouldn't happen."""
    with _sessions_lock:
        session = _sessions.get(camera_index)
        if session is None:
            session = CameraSession(camera_index)
            _sessions[camera_index] = session
        return session
