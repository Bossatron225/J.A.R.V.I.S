import json
import os
import sys
import tempfile
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

_HAS_FACE_MODULE = bool(cv2 is not None and hasattr(cv2, "face"))
_LBPH_FACE_SIZE = (200, 200)
_LBPH_LABEL = 1
_LBPH_DEFAULT_THRESHOLD = 75.0


def _load_detector():
    classifier = getattr(cv2, "CascadeClassifier", None)
    if classifier is None:
        return None
    detector = classifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if hasattr(detector, "empty") and detector.empty():
        return None
    return detector


def _load_profile_detector():
    """haarcascade_profileface.xml is trained on left-facing profiles; the caller
    also tries it against a horizontally flipped frame to catch right-facing turns."""
    classifier = getattr(cv2, "CascadeClassifier", None)
    if classifier is None:
        return None
    detector = classifier(str(Path(cv2.data.haarcascades) / "haarcascade_profileface.xml"))
    if hasattr(detector, "empty") and detector.empty():
        return None
    return detector


def _load_detectors():
    return _load_detector(), _load_profile_detector()


def _detect_faces(gray_frame, detector):
    if detector is None:
        return []
    return list(detector.detectMultiScale(
        gray_frame,
        scaleFactor=1.1,
        minNeighbors=6,
        minSize=(64, 64),
    ))


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


def _extract_primary_face(gray_frame, detectors):
    """detectors is a (frontal, profile) tuple. Frontal is tried first (best quality
    crop); if it finds nothing — which happens whenever the head is turned or tilted,
    since the frontal cascade is frontal-only — profile is tried against the frame as-is
    and against a horizontally flipped copy, so both left- and right-facing turns are
    covered. Only falls back to an uncentered guess if no cascade found anything at all."""
    frontal, profile = detectors if isinstance(detectors, tuple) else (detectors, None)

    faces = _detect_faces(gray_frame, frontal)
    if faces:
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return gray_frame[y : y + h, x : x + w]

    faces = _detect_faces(gray_frame, profile)
    if faces:
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return gray_frame[y : y + h, x : x + w]

    if profile is not None:
        flipped = cv2.flip(gray_frame, 1)
        faces = _detect_faces(flipped, profile)
        if faces:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            return flipped[y : y + h, x : x + w]

    return _center_face_crop(gray_frame)


def _biometric_models_dir() -> Path:
    path = Path(__file__).resolve().parent / "config" / "biometric_models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _model_path(profile_key: str) -> Path:
    safe_key = "".join(ch for ch in profile_key.strip().lower() if ch.isalnum() or ch in ("-", "_")) or "primary"
    return _biometric_models_dir() / f"{safe_key}.yml"


def _normalize_face_crop(gray_face):
    resized = cv2.resize(gray_face, _LBPH_FACE_SIZE)
    return cv2.equalizeHist(resized)


def _extract_face_for_training(image_bytes: bytes):
    """Decode an enrollment sample and return a normalized face crop, or None if unusable."""
    if cv2 is None or np is None or not image_bytes:
        return None
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face = _extract_primary_face(gray, _load_detectors())
        if face is None:
            return None
        return _normalize_face_crop(face)
    except Exception:
        return None


def train_face_model(sample_images: list, min_samples: int = 3) -> Tuple[bool, "bytes | None", str]:
    """Train an LBPH recognizer from several enrollment samples (ideally spanning
    different angles/distances) and return the serialized model bytes."""
    if not _HAS_FACE_MODULE:
        return False, None, "cv2.face is unavailable (install opencv-contrib-python)"

    faces = []
    for image_bytes in sample_images or []:
        face = _extract_face_for_training(image_bytes)
        if face is not None:
            faces.append(face)

    if len(faces) < min_samples:
        return False, None, f"Only {len(faces)} usable face sample(s) captured; need at least {min_samples}"

    try:
        recognizer = cv2.face.LBPHFaceRecognizer_create(radius=1, neighbors=8, grid_x=8, grid_y=8)
        labels = np.array([_LBPH_LABEL] * len(faces))
        recognizer.train(faces, labels)
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            recognizer.write(tmp_path)
            model_bytes = Path(tmp_path).read_bytes()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return True, model_bytes, f"Trained on {len(faces)} face sample(s)"
    except Exception as exc:
        return False, None, f"Training failed: {exc}"


def save_face_model(profile_key: str, model_bytes: bytes) -> Path:
    path = _model_path(profile_key)
    path.write_bytes(model_bytes)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_face_model(profile_key: str):
    """Load a previously trained LBPH model for a profile, or None if none exists."""
    if not _HAS_FACE_MODULE:
        return None
    path = _model_path(profile_key)
    if not path.exists():
        return None
    try:
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.read(str(path))
        return recognizer
    except Exception:
        return None


def _lbph_threshold() -> float:
    try:
        return float(os.environ.get("JARVIS_LBPH_THRESHOLD", _LBPH_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _LBPH_DEFAULT_THRESHOLD


def verify_face_against_model(
    recognizer,
    camera_index: int = 0,
    num_frames: int = 14,
    threshold: float | None = None,
) -> Tuple[bool, str]:
    """Capture a short burst of live frames and match the best face crop against a
    trained per-profile LBPH model. Robust to head angle/distance because the model
    was trained on samples spanning those variations, and matching is done against
    the single best (lowest-distance) frame in the burst rather than one snapshot."""
    if recognizer is None:
        return False, "no-model"
    if cv2 is None or np is None:
        return False, "opencv-unavailable"

    gate = _lbph_threshold() if threshold is None else threshold

    try:
        capture = cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            capture.release()
            return False, "no-webcam"

        detector = _load_detector()
        best_confidence = None
        face_seen = False
        for _ in range(num_frames):
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            face = _extract_primary_face(gray, detector)
            if face is None:
                continue
            face_seen = True
            normalized = _normalize_face_crop(face)
            try:
                _, confidence = recognizer.predict(normalized)
            except Exception:
                continue
            if best_confidence is None or confidence < best_confidence:
                best_confidence = confidence
            if best_confidence is not None and best_confidence <= gate:
                break
        capture.release()

        if not face_seen:
            return False, "no-face-detected"
        if best_confidence is None:
            return False, "predict-failed"
        if best_confidence <= gate:
            return True, f"Face matched trained model (confidence={best_confidence:.1f}, threshold={gate:.1f})"
        return False, f"Face did not match trained model (confidence={best_confidence:.1f}, threshold={gate:.1f})"
    except Exception as exc:
        return False, f"Face verification failed: {exc}"


def has_face_model(profile_key: str) -> bool:
    """Whether a trained LBPH model file exists for this profile, without loading it."""
    return _model_path(profile_key).exists()


def verify_face_against_model_bytes(
    recognizer,
    frames: list,
    threshold: float | None = None,
) -> Tuple[bool, str]:
    """Match a burst of already-captured frames (e.g. uploaded from a browser) against
    a trained per-profile LBPH model. Same best-of-burst matching as
    verify_face_against_model, but sourced from provided image bytes instead of a
    local cv2.VideoCapture, so it works for cameras the server process can't open
    directly (a phone's camera arriving over HTTP)."""
    if recognizer is None:
        return False, "no-model"
    if cv2 is None or np is None:
        return False, "opencv-unavailable"

    gate = _lbph_threshold() if threshold is None else threshold
    detector = _load_detector()
    best_confidence = None
    face_seen = False

    for image_bytes in frames or []:
        face = _extract_face_for_training(image_bytes)
        if face is None:
            continue
        face_seen = True
        try:
            _, confidence = recognizer.predict(face)
        except Exception:
            continue
        if best_confidence is None or confidence < best_confidence:
            best_confidence = confidence
        if best_confidence is not None and best_confidence <= gate:
            break

    if not face_seen:
        return False, "no-face-detected"
    if best_confidence is None:
        return False, "predict-failed"
    if best_confidence <= gate:
        return True, f"Face matched trained model (confidence={best_confidence:.1f}, threshold={gate:.1f})"
    return False, f"Face did not match trained model (confidence={best_confidence:.1f}, threshold={gate:.1f})"


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
