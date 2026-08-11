from pathlib import Path

from auth import verify_face


def test_verify_face_reports_missing_reference(monkeypatch, tmp_path):
    missing_reference = tmp_path / "missing.jpg"

    class DummyCapture:
        def __init__(self, *args, **kwargs):
            self.opened = True

        def isOpened(self):
            return True

        def read(self):
            return True, None

        def release(self):
            return None

    monkeypatch.setattr("auth.cv2", None)
    result, reason = verify_face(str(missing_reference))

    assert result is False
    assert "reference image" in reason.lower()
