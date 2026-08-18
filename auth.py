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


def _load_detector():
    classifier = getattr(cv2, "CascadeClassifier", None)
    if classifier is None:
        return None
    detector = classifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if hasattr(detector, "empty") and detector.empty():
        return None
    return detector


def _center_face_crop(gray_frame):
    h, w = gray_frame.shape[:2]
    side = int(min(h, w) * 0.58)
    side = max(64, side)
    cx = w // 2
    cy = h // 2
    x1 = max(0, cx - side // 2)
    y1 = max(0, cy - side // 2)
    x2 = min(w, x1 + side)
    y2 = min(h, y1 + side)
    if x2 <= x1 or y2 <= y1:
        return None
    return gray_frame[y1:y2, x1:x2]


def _extract_primary_face(gray_frame, detector):
    if detector is None:
        return _center_face_crop(gray_frame)

    faces = detector.detectMultiScale(
        gray_frame,
        scaleFactor=1.1,
        minNeighbors=6,
        minSize=(64, 64),
    )
    if len(faces) == 0:
        return _center_face_crop(gray_frame)
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return gray_frame[y : y + h, x : x + w]


def _similarity_metrics(reference_face, live_face):
    ref_norm = cv2.equalizeHist(cv2.resize(reference_face, (160, 160)))
    live_norm = cv2.equalizeHist(cv2.resize(live_face, (160, 160)))

    # Correlation is robust for quick checks while MSE rejects near-random matches.
    corr = cv2.matchTemplate(ref_norm, live_norm, cv2.TM_CCOEFF_NORMED)[0][0]
    mse = float(np.mean((ref_norm.astype("float32") - live_norm.astype("float32")) ** 2)) / (255.0 ** 2)
    return float(corr), mse


def _is_face_match(corr: float, mse: float) -> bool:
    # Calibrated gates: reject low-correlation accidental matches while allowing lighting variance.
    strong_match = corr >= 0.36 and mse <= 0.17
    balanced_match = corr >= 0.30 and mse <= 0.13
    return bool(strong_match or balanced_match)


def verify_face(reference_image: str | os.PathLike[str] | None = None, camera_index: int = 0) -> Tuple[bool, str]:
    """Capture a frame from the default webcam and compare it to a reference image."""
    ref_path = Path(reference_image or "auth_reference.jpg")
    if not ref_path.exists():
        return False, f"Reference image not found: {ref_path}"

    if cv2 is None or np is None:
        return False, "OpenCV/numpy are not available in this environment"

    try:
        detector = _load_detector()

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

        frames = []
        # Read a few frames so camera auto-exposure can settle.
        for _ in range(14):
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
        capture.release()
        if not frames:
            return False, "Unable to read a frame from the webcam"

        best_corr = -1.0
        best_mse = 1.0
        face_seen = False
        for frame in frames:
            frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            live_face = _extract_primary_face(frame_gray, detector)
            if live_face is None:
                continue
            face_seen = True
            corr, mse = _similarity_metrics(reference_face, live_face)
            if corr > best_corr:
                best_corr, best_mse = corr, mse
            if _is_face_match(corr, mse):
                return True, f"Face verified (corr={corr:.2f}, mse={mse:.3f})"

        if not face_seen:
            return False, "No face was detected in the webcam frame"
        return False, f"Face did not match (best corr={best_corr:.2f}, mse={best_mse:.3f})"
    except Exception as exc:
        return False, f"Face verification failed: {exc}"


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "auth_reference.jpg"
    ok, reason = verify_face(ref)
    print(json.dumps({"ok": ok, "reason": reason}))
