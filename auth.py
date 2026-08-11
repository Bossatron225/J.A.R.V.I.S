import os
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import face_recognition
except Exception:  # pragma: no cover - optional dependency
    face_recognition = None


def verify_face(reference_image: str | os.PathLike[str] | None = None, camera_index: int = 0) -> Tuple[bool, str]:
    """Capture a frame from the default webcam and compare it to a reference image."""
    ref_path = Path(reference_image or "auth_reference.jpg")
    if not ref_path.exists():
        return False, f"Reference image not found: {ref_path}"

    if cv2 is None or face_recognition is None:
        return False, "OpenCV/face_recognition are not available in this environment"

    try:
        capture = cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            capture.release()
            return False, "No webcam was available"

        ok, frame = capture.read()
        capture.release()
        if not ok or frame is None:
            return False, "Unable to read a frame from the webcam"

        reference_image_rgb = face_recognition.load_image_file(str(ref_path))
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        reference_encodings = face_recognition.face_encodings(reference_image_rgb)
        frame_encodings = face_recognition.face_encodings(frame_rgb)

        if not reference_encodings:
            return False, "No face was detected in the reference image"
        if not frame_encodings:
            return False, "No face was detected in the webcam frame"

        matches = face_recognition.compare_faces([reference_encodings[0]], frame_encodings[0], tolerance=0.6)
        if matches and matches[0]:
            return True, "Face verified"
        return False, "Face did not match the reference image"
    except Exception as exc:
        return False, f"Face verification failed: {exc}"


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "auth_reference.jpg"
    ok, reason = verify_face(ref)
    print(json.dumps({"ok": ok, "reason": reason}))
