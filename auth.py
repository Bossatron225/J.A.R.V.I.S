import json
import os
import sys
import tempfile
from functools import lru_cache
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
_HAS_SFACE = bool(cv2 is not None and hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF"))

_FACE_MODELS_DIR = Path(__file__).resolve().parent / "config" / "face_models"
_YUNET_MODEL_PATH = _FACE_MODELS_DIR / "face_detection_yunet.onnx"
_SFACE_MODEL_PATH = _FACE_MODELS_DIR / "face_recognition_sface.onnx"

# Official OpenCV Zoo recommended decision threshold for FaceRecognizerSF cosine
# similarity (calibrated on LFW) — same identity if score is at or above this.
_SFACE_DEFAULT_THRESHOLD = 0.363
_YUNET_SCORE_THRESHOLD = 0.6


_BUNDLED_CASCADES_DIR = Path(__file__).resolve().parent / "config" / "haarcascades"


def _cascade_path(filename: str) -> str:
    """opencv-contrib-python 5.0.0.93 (the version pinned in requirements.txt) shipped
    cv2.data.haarcascades as an empty directory — no XML files at all, on both macOS
    and Linux wheels, confirmed by inspecting the installed package's file list. Every
    face detection call was silently falling back to a centered guess-crop with zero
    actual face detection, which is why recognition only ever worked when dead-on and
    centered. Cascades are bundled here (extracted from opencv-contrib-python
    4.11.0.86, the last version confirmed to ship them) instead of depending on
    whatever a given opencv wheel happens to include; cv2.data.haarcascades is still
    tried first in case a future/different install does ship them."""
    bundled = _BUNDLED_CASCADES_DIR / filename
    if bundled.exists():
        return str(bundled)
    return str(Path(cv2.data.haarcascades) / filename)


def _load_detector():
    classifier = getattr(cv2, "CascadeClassifier", None)
    if classifier is None:
        return None
    detector = classifier(_cascade_path("haarcascade_frontalface_default.xml"))
    if hasattr(detector, "empty") and detector.empty():
        return None
    return detector


def _load_profile_detector():
    """haarcascade_profileface.xml is trained on left-facing profiles; the caller
    also tries it against a horizontally flipped frame to catch right-facing turns."""
    classifier = getattr(cv2, "CascadeClassifier", None)
    if classifier is None:
        return None
    detector = classifier(_cascade_path("haarcascade_profileface.xml"))
    if hasattr(detector, "empty") and detector.empty():
        return None
    return detector


@lru_cache(maxsize=1)
def _load_detectors():
    """Cascades are stateless once loaded, and this now runs per-frame in a burst
    (up to ~16 frames per scan/enrollment sample), so cache instead of re-reading
    the XML files from disk on every single frame."""
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
    """.npy, not .yml — this is a stack of SFace embeddings now, not a serialized LBPH
    model. The extension change is deliberate: it makes any model trained before this
    upgrade simply invisible to has_face_model()/load_face_model() rather than being
    misread, so a stale LBPH-era model can never get compared against a real embedding."""
    safe_key = "".join(ch for ch in profile_key.strip().lower() if ch.isalnum() or ch in ("-", "_")) or "primary"
    return _biometric_models_dir() / f"{safe_key}.npy"


@lru_cache(maxsize=1)
def _load_yunet_detector():
    if not _HAS_SFACE or not _YUNET_MODEL_PATH.exists():
        return None
    try:
        return cv2.FaceDetectorYN.create(str(_YUNET_MODEL_PATH), "", (320, 320), _YUNET_SCORE_THRESHOLD, 0.3, 5000)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_sface_recognizer():
    if not _HAS_SFACE or not _SFACE_MODEL_PATH.exists():
        return None
    try:
        return cv2.FaceRecognizerSF.create(str(_SFACE_MODEL_PATH), "")
    except Exception:
        return None


def _detect_and_embed(frame_bgr):
    """Detect the largest/most-confident face (YuNet), align it via the detector's own
    5-point landmarks, and return its 128-d SFace embedding — or None if no usable face
    was found. Alignment is what makes this robust to head angle/tilt in a way the old
    LBPH pipeline's raw bounding-box crop never was: it warps the face to a canonical
    112x112 pose before embedding, rather than comparing whatever rotated/skewed crop
    the camera happened to capture."""
    detector = _load_yunet_detector()
    recognizer = _load_sface_recognizer()
    if detector is None or recognizer is None or frame_bgr is None:
        return None
    try:
        h, w = frame_bgr.shape[:2]
        if h < 2 or w < 2:
            return None
        detector.setInputSize((w, h))
        _, faces = detector.detect(frame_bgr)
        if faces is None or len(faces) == 0:
            return None
        best = max(faces, key=lambda f: f[-1])  # highest detection confidence
        aligned = recognizer.alignCrop(frame_bgr, best)
        return recognizer.feature(aligned)
    except Exception:
        return None


def _embed_image_bytes(image_bytes: bytes):
    """Decode an enrollment/verification sample and return its face embedding, or None."""
    if cv2 is None or np is None or not image_bytes:
        return None
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        return _detect_and_embed(frame)
    except Exception:
        return None


def train_face_model(sample_images: list, min_samples: int = 3) -> Tuple[bool, "bytes | None", str]:
    """Build a per-profile embedding set from several enrollment samples (ideally
    spanning different angles/distances) and return it serialized as .npy bytes."""
    if not _HAS_SFACE:
        return False, None, "cv2 FaceDetectorYN/FaceRecognizerSF are unavailable (need opencv-contrib-python 4.5.4+)"
    if not _YUNET_MODEL_PATH.exists() or not _SFACE_MODEL_PATH.exists():
        return False, None, "face recognition model files are missing from config/face_models/"

    embeddings = []
    for image_bytes in sample_images or []:
        emb = _embed_image_bytes(image_bytes)
        if emb is not None:
            embeddings.append(emb.reshape(-1))

    if len(embeddings) < min_samples:
        return False, None, f"Only {len(embeddings)} usable face sample(s) captured; need at least {min_samples}"

    try:
        stacked = np.stack(embeddings).astype(np.float32)
        buf = io.BytesIO()
        np.save(buf, stacked, allow_pickle=False)
        return True, buf.getvalue(), f"Trained on {len(embeddings)} face sample(s)"
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
    """Load a previously trained embedding set for a profile, or None if none exists."""
    if not _HAS_SFACE or np is None:
        return None
    path = _model_path(profile_key)
    if not path.exists():
        return None
    try:
        return np.load(path, allow_pickle=False)
    except Exception:
        return None


def _sface_threshold() -> float:
    try:
        return float(os.environ.get("JARVIS_SFACE_THRESHOLD", _SFACE_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _SFACE_DEFAULT_THRESHOLD


def _best_cosine_match(recognizer, live_embedding, stored_embeddings) -> float:
    best = -1.0
    for stored in stored_embeddings:
        score = float(recognizer.match(
            live_embedding.reshape(1, -1).astype(np.float32),
            stored.reshape(1, -1).astype(np.float32),
            cv2.FaceRecognizerSF_FR_COSINE,
        ))
        if score > best:
            best = score
    return best


def verify_face_against_model(
    recognizer,
    camera_index: int = 0,
    num_frames: int = 14,
    threshold: float | None = None,
) -> Tuple[bool, str]:
    """Capture a short burst of live frames; for each, detect+align+embed via
    YuNet/SFace and compare (cosine similarity) against every stored enrollment
    embedding. Takes the single best score across the whole burst rather than one
    snapshot, and stops early once a clear match is found. `recognizer` here is
    actually the stored embedding array from load_face_model (kept as the parameter
    name other callers already use)."""
    stored_embeddings = recognizer
    if stored_embeddings is None or len(stored_embeddings) == 0:
        return False, "no-model"
    if not _HAS_SFACE or np is None:
        return False, "opencv-unavailable"

    sface = _load_sface_recognizer()
    if sface is None:
        return False, "opencv-unavailable"

    gate = _sface_threshold() if threshold is None else threshold

    try:
        capture = cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            capture.release()
            return False, "no-webcam"

        best_score = None
        face_seen = False
        for _ in range(num_frames):
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            embedding = _detect_and_embed(frame)
            if embedding is None:
                continue
            face_seen = True
            score = _best_cosine_match(sface, embedding, stored_embeddings)
            if best_score is None or score > best_score:
                best_score = score
            if best_score is not None and best_score >= gate:
                break
        capture.release()

        if not face_seen:
            return False, "no-face-detected"
        if best_score is None:
            return False, "predict-failed"
        if best_score >= gate:
            return True, f"Face matched trained model (similarity={best_score:.3f}, threshold={gate:.3f})"
        return False, f"Face did not match trained model (similarity={best_score:.3f}, threshold={gate:.3f})"
    except Exception as exc:
        return False, f"Face verification failed: {exc}"


def has_face_model(profile_key: str) -> bool:
    """Whether a trained embedding model file exists for this profile, without loading it."""
    return _model_path(profile_key).exists()


def verify_face_against_model_bytes(
    recognizer,
    frames: list,
    threshold: float | None = None,
) -> Tuple[bool, str]:
    """Match a burst of already-captured frames (e.g. uploaded from a browser) against
    a trained per-profile embedding set. Same best-of-burst matching as
    verify_face_against_model, but sourced from provided image bytes instead of a
    local cv2.VideoCapture, so it works for cameras the server process can't open
    directly (a phone's camera arriving over HTTP)."""
    stored_embeddings = recognizer
    if stored_embeddings is None or len(stored_embeddings) == 0:
        return False, "no-model"
    if not _HAS_SFACE or np is None:
        return False, "opencv-unavailable"

    sface = _load_sface_recognizer()
    if sface is None:
        return False, "opencv-unavailable"

    gate = _sface_threshold() if threshold is None else threshold
    best_score = None
    face_seen = False

    for image_bytes in frames or []:
        embedding = _embed_image_bytes(image_bytes)
        if embedding is None:
            continue
        face_seen = True
        score = _best_cosine_match(sface, embedding, stored_embeddings)
        if best_score is None or score > best_score:
            best_score = score
        if best_score is not None and best_score >= gate:
            break

    if not face_seen:
        return False, "no-face-detected"
    if best_score is None:
        return False, "predict-failed"
    if best_score >= gate:
        return True, f"Face matched trained model (similarity={best_score:.3f}, threshold={gate:.3f})"
    return False, f"Face did not match trained model (similarity={best_score:.3f}, threshold={gate:.3f})"


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
        detectors = _load_detectors()

        reference_bgr = cv2.imread(str(ref_path))
        if reference_bgr is None:
            return False, "Unable to read the reference image"
        reference_gray = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2GRAY)
        reference_face = _extract_primary_face(reference_gray, detectors)
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
            live_face = _extract_primary_face(frame_gray, detectors)
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
