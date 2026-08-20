import threading
from contextlib import contextmanager

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None


# Requested capture resolution for every camera-using feature (person/visitor
# detection, continuous monitoring, vision capture). This is a *request*, not
# a requirement: UVC/AVFoundation camera drivers clamp an unsupported request
# down to the device's nearest supported mode rather than failing, so asking
# for 4K is safe even on a camera that only supports 1080p or lower — it just
# gets that camera's best available resolution instead.
TARGET_FRAME_WIDTH = 3840
TARGET_FRAME_HEIGHT = 2160


def apply_target_resolution(capture) -> None:
    """Ask an opened cv2.VideoCapture for the target resolution. Best-effort —
    swallow errors so a camera/backend that rejects the property set still
    works at whatever resolution it already opened with."""
    if capture is None:
        return
    try:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_FRAME_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_FRAME_HEIGHT)
    except Exception:
        pass


class CameraSession:
    """Owns one physical camera's cv2.VideoCapture handle, keeping it open across
    many get_frame() calls instead of opening/closing per call. Opening a camera
    measured at ~1.4s on this hardware (vs ~10-30ms per read once open), so any
    continuous polling loop needs to hold the device open rather than reopen it
    every cycle. One-off consumers (biometric checks, vision-Q&A capture) use
    request_exclusive() instead of opening their own cv2.VideoCapture, so they
    always get sole access to the device rather than racing a running monitor for
    it — the shared lock below is the single coordination point for both cases.
    """

    def __init__(self, camera_index: int = 0):
        self.camera_index = camera_index
        self._lock = threading.Lock()
        self._capture = None

    def _open_locked(self) -> None:
        if self._capture is not None:
            return
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Camera index {self.camera_index} could not be opened.")
        apply_target_resolution(cap)
        self._capture = cap

    def _close_locked(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def get_frame(self):
        """Continuous-monitor fast path — call only from the monitor's own loop.
        Opens the device lazily on first use, then reuses the open handle."""
        with self._lock:
            self._open_locked()
            ok, frame = self._capture.read()
            return frame if ok else None

    def release(self) -> None:
        """Release the device (and turn off the indicator light) when nothing
        needs it — call when continuous monitoring stops."""
        with self._lock:
            self._close_locked()

    @contextmanager
    def request_exclusive(self):
        """One-off consumers use this instead of opening cv2.VideoCapture
        directly. Closes the session's handle first (if a continuous monitor has
        it open) so the caller has sole access to the physical device for the
        duration of the block; the monitor lazily reopens on its next
        get_frame() call."""
        with self._lock:
            self._close_locked()
            yield self


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
