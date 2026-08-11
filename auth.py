import json
import os
import sys
from pathlib import Path
from typing import Tuple

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None


def _extract_primary_face(gray_frame, detector):
    faces = detector.detectMultiScale(
        gray_frame,
        scaleFactor=1.1,
        minNeighbors=6,
        minSize=(64, 64),
    )
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return gray_frame[y : y + h, x : x + w]


def _similarity_metrics(reference_face, live_face):
    ref_norm = cv2.equalizeHist(cv2.resize(reference_face, (160, 160)))
    live_norm = cv2.equalizeHist(cv2.resize(live_face, (160, 160)))

    # Correlation is robust for quick checks while MSE rejects near-random matches.
    corr = cv2.matchTemplate(ref_norm, live_norm, cv2.TM_CCOEFF_NORMED)[0][0]
    mse = float(np.mean((ref_norm.astype("float32") - live_norm.astype("float32")) ** 2)) / (255.0 ** 2)
    return float(corr), mse


def verify_face(reference_image: str | os.PathLike[str] | None = None, camera_index: int = 0) -> Tuple[bool, str]:
    """Capture a frame from the default webcam and compare it to a reference image."""
    ref_path = Path(reference_image or "auth_reference.jpg")
    if not ref_path.exists():
        return False, f"Reference image not found: {ref_path}"

    if cv2 is None or np is None:
        return False, "OpenCV/numpy are not available in this environment"

    try:
        detector = cv2.CascadeClassifier(
            str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        )
        if detector.empty():
            return False, "OpenCV Haar cascade was not available"

        reference_bgr = cv2.imread(str(ref_path))
        if reference_bgr is None:
            return False, "Unable to read the reference image"
        reference_gray = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2GRAY)
        reference_face = _extract_primary_face(reference_gray, detector)
        if reference_face is None:
            return False, "No face was detected in the reference image"

        capture = cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            capture.release()
            return False, "No webcam was available"

        ok, frame = capture.read()
        capture.release()
        if not ok or frame is None:
            return False, "Unable to read a frame from the webcam"

        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        live_face = _extract_primary_face(frame_gray, detector)
        if live_face is None:
            return False, "No face was detected in the webcam frame"

        corr, mse = _similarity_metrics(reference_face, live_face)
        if corr >= 0.58 and mse <= 0.15:
            return True, f"Face verified (corr={corr:.2f}, mse={mse:.3f})"
        return False, f"Face did not match (corr={corr:.2f}, mse={mse:.3f})"
    except Exception as exc:
        return False, f"Face verification failed: {exc}"


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "auth_reference.jpg"
    ok, reason = verify_face(ref)
    print(json.dumps({"ok": ok, "reason": reason}))
