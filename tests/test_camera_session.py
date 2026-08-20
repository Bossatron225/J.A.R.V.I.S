from actions import camera_session as camera_session_module
from actions.camera_session import CameraSession, get_camera_session


class _FakeCapture:
    def __init__(self):
        self.opened = True
        self.released = False
        self.read_count = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_count += 1
        return True, f"frame-{self.read_count}"

    def release(self):
        self.released = True
        self.opened = False


def test_get_frame_opens_device_lazily_once(monkeypatch):
    captures = []

    def _fake_video_capture(index):
        cap = _FakeCapture()
        captures.append(cap)
        return cap

    monkeypatch.setattr(camera_session_module.cv2, "VideoCapture", _fake_video_capture)

    session = CameraSession(camera_index=0)
    frame1 = session.get_frame()
    frame2 = session.get_frame()

    assert frame1 == "frame-1"
    assert frame2 == "frame-2"
    assert len(captures) == 1  # only opened once across two get_frame() calls


def test_release_closes_the_device(monkeypatch):
    monkeypatch.setattr(camera_session_module.cv2, "VideoCapture", lambda idx: _FakeCapture())

    session = CameraSession(camera_index=0)
    session.get_frame()
    session.release()

    assert session._capture is None


def test_request_exclusive_closes_device_for_caller_and_monitor_reopens_after(monkeypatch):
    captures = []

    def _fake_video_capture(index):
        cap = _FakeCapture()
        captures.append(cap)
        return cap

    monkeypatch.setattr(camera_session_module.cv2, "VideoCapture", _fake_video_capture)

    session = CameraSession(camera_index=0)
    session.get_frame()  # monitor "opens" the device
    assert len(captures) == 1
    assert not captures[0].released

    with session.request_exclusive():
        assert captures[0].released  # closed for the exclusive caller
        assert session._capture is None

    # After the exclusive block, the monitor's next get_frame() reopens lazily.
    session.get_frame()
    assert len(captures) == 2


def test_get_camera_session_returns_same_instance_per_index():
    camera_session_module._sessions.clear()

    a = get_camera_session(0)
    b = get_camera_session(0)
    c = get_camera_session(1)

    assert a is b
    assert a is not c
