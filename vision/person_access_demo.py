"""
Project JARVIS — Local Access Log Dashboard (educational demo)
================================================================

Orchestrates a real-time OpenCV loop that:
  1. Reads camera frames on a background thread (never blocks the main loop).
  2. Runs YOLOv8n to gate frames on "is a person present".
  3. Matches faces against a local `known_faces/` folder to resolve a name.
  4. Fires a SIMULATED asynchronous "verification" request exactly once per
     detected identity (known or unverified), and overlays the mock result.

IMPORTANT — this is a simulation, not a real background-check tool:
  * `VerificationEngine._simulate_network_fetch()` never makes a network call.
    It sleeps for a random interval and returns data drawn from
    `MOCK_PROFILE_DB`, a plain Python dict defined in this file.
  * There is no integration with any real people-search, background-check,
    or public-records service. Wiring this up to one would require a
    documented permissible purpose and consent flow that a webcam loop
    cannot provide on its own — see vision/README.md.
  * `known_faces/` is meant to hold photos of consenting household members
    you enroll yourself, not third parties.

Run:
    pip install -r vision/requirements-vision.txt
    python vision/person_access_demo.py
Press 'q' in the video window (or Ctrl+C in the terminal) to exit cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.access_demo")

try:
    import cv2
except ImportError as e:
    raise SystemExit(
        "opencv-python is required. Install with: pip install -r vision/requirements-vision.txt"
    ) from e

try:
    import numpy as np
except ImportError as e:
    raise SystemExit("numpy is required (installed automatically with opencv-python).") from e

try:
    from ultralytics import YOLO
except ImportError as e:
    raise SystemExit(
        "ultralytics is required. Install with: pip install -r vision/requirements-vision.txt"
    ) from e

try:
    import face_recognition
except ImportError as e:
    raise SystemExit(
        "face_recognition is required. Install with: pip install -r vision/requirements-vision.txt\n"
        "(it depends on dlib — see the comment at the top of requirements-vision.txt)"
    ) from e

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; falls back to plain os.environ / defaults

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None  # optional; falls back to console output


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    camera_index: int = field(default_factory=lambda: _env("CAMERA_INDEX", 0, int))
    yolo_model: str = field(default_factory=lambda: _env("YOLO_MODEL", "yolov8n.pt", str))
    known_faces_dir: Path = field(
        default_factory=lambda: (Path(__file__).resolve().parent
                                  / _env("KNOWN_FACES_DIR", "known_faces", str))
    )
    face_match_tolerance: float = field(default_factory=lambda: _env("FACE_MATCH_TOLERANCE", 0.6, float))
    detection_confidence: float = field(default_factory=lambda: _env("DETECTION_CONFIDENCE", 0.5, float))
    face_recognition_every_n_frames: int = field(
        default_factory=lambda: _env("FACE_RECOGNITION_EVERY_N_FRAMES", 10, int)
    )
    verification_workers: int = field(default_factory=lambda: _env("VERIFICATION_WORKERS", 4, int))
    sim_latency_min: float = field(default_factory=lambda: _env("SIM_LATENCY_MIN", 0.8, float))
    sim_latency_max: float = field(default_factory=lambda: _env("SIM_LATENCY_MAX", 2.2, float))
    sim_failure_rate: float = field(default_factory=lambda: _env("SIM_FAILURE_RATE", 0.05, float))
    access_log_path: Path = field(
        default_factory=lambda: (Path(__file__).resolve().parent
                                  / _env("ACCESS_LOG_PATH", "access_log_demo.jsonl", str))
    )
    max_track_disappeared_frames: int = 20
    track_match_max_distance: float = 120.0


# --------------------------------------------------------------------------
# Mock verification backend — SIMULATED, no network calls
# --------------------------------------------------------------------------

MOCK_PROFILE_DB: dict[str, dict] = {
    # Keyed by the display name derived from a known_faces/<Name>.jpg filename.
    # Extend this with your own enrolled household members.
    "default_known": {
        "authorized_clearance": "STANDARD",
        "activity_status": "ACTIVE",
    },
}


class VerificationStatus(Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass
class VerificationRecord:
    identity: str
    is_known: bool
    status: VerificationStatus = VerificationStatus.PENDING
    profile: Optional[dict] = None
    error: Optional[str] = None


class VerificationEngine:
    """Simulates an async 'enterprise AI access log' lookup.

    Fires the (fake) network fetch exactly once per unique identity for the
    lifetime of this engine — a lock-guarded state machine prevents a second
    submission from racing in while the first is still in flight, which is
    the actual spam vector in a naive per-frame implementation.
    """

    def __init__(self, config: Config, on_complete=None):
        self._config = config
        self._executor = ThreadPoolExecutor(
            max_workers=config.verification_workers, thread_name_prefix="verify"
        )
        self._records: dict[str, VerificationRecord] = {}
        self._lock = threading.Lock()
        self._on_complete = on_complete  # callback(VerificationRecord)

    def request_verification(self, identity: str, is_known: bool) -> VerificationRecord:
        """Idempotent: returns the existing record if one is already
        pending/in-flight/done for this identity, otherwise schedules a new
        simulated lookup and returns the freshly created PENDING record."""
        with self._lock:
            existing = self._records.get(identity)
            if existing is not None:
                return existing
            record = VerificationRecord(identity=identity, is_known=is_known)
            self._records[identity] = record
            record.status = VerificationStatus.IN_PROGRESS

        self._executor.submit(self._run, record)
        return record

    def get_record(self, identity: str) -> Optional[VerificationRecord]:
        with self._lock:
            return self._records.get(identity)

    def _run(self, record: VerificationRecord) -> None:
        try:
            profile = self._simulate_network_fetch(record.identity, record.is_known)
            with self._lock:
                record.profile = profile
                record.status = VerificationStatus.COMPLETE
        except TimeoutError as e:
            with self._lock:
                record.error = str(e)
                record.status = VerificationStatus.FAILED
            log.warning("Simulated verification timeout for %r: %s", record.identity, e)
        except Exception as e:  # defensive: a worker thread must never crash silently
            with self._lock:
                record.error = f"unexpected error: {e}"
                record.status = VerificationStatus.FAILED
            log.exception("Verification worker failed for %r", record.identity)
        finally:
            if self._on_complete:
                try:
                    self._on_complete(record)
                except Exception:
                    log.exception("on_complete callback raised")

    def _simulate_network_fetch(self, identity: str, is_known: bool) -> dict:
        """Stands in for `requests.get(INTERNAL_ACCESS_API, ...)`. Sleeps to
        mimic latency, then returns a mock profile. Raises TimeoutError at
        `sim_failure_rate` to exercise the error-handling path."""
        latency = random.uniform(self._config.sim_latency_min, self._config.sim_latency_max)
        time.sleep(latency)

        if random.random() < self._config.sim_failure_rate:
            raise TimeoutError(f"simulated network timeout after {latency:.1f}s")

        base = MOCK_PROFILE_DB.get(identity) or (
            MOCK_PROFILE_DB["default_known"] if is_known else
            {"authorized_clearance": "NONE", "activity_status": "UNVERIFIED_GUEST"}
        )
        history = [
            (datetime.now(timezone.utc).replace(microsecond=0) - _fake_days_ago(n)).isoformat()
            for n in sorted(random.sample(range(1, 60), k=3))
        ]
        return {
            "authorized_clearance": base.get("authorized_clearance", "NONE"),
            "activity_status": base.get("activity_status", "UNKNOWN"),
            "log_history": history,
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


def _fake_days_ago(n: int):
    from datetime import timedelta
    return timedelta(days=n)


# --------------------------------------------------------------------------
# Threaded camera capture — decouples frame grabbing from the render loop
# --------------------------------------------------------------------------

class ThreadedVideoStream:
    def __init__(self, camera_index: int):
        self._cap = cv2.VideoCapture(camera_index)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"Could not open camera index {camera_index}. Is it connected, "
                f"in use by another app, or does this OS need a camera permission grant?"
            )
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._ok = False
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._update, daemon=True, name="camera-reader")
        self._thread.start()

    def _update(self) -> None:
        while not self._stopped.is_set():
            ok, frame = self._cap.read()
            with self._lock:
                self._ok = ok
                if ok:
                    self._frame = frame
            if not ok:
                time.sleep(0.05)  # camera hiccup — avoid a tight spin loop

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        with self._lock:
            if self._frame is None:
                return False, None
            return self._ok, self._frame.copy()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=2.0)
        self._cap.release()


# --------------------------------------------------------------------------
# Person detection (YOLOv8n)
# --------------------------------------------------------------------------

PERSON_CLASS_ID = 0  # COCO class index for "person"


class PersonDetector:
    def __init__(self, model_path: str, confidence: float):
        log.info("Loading YOLO model %r ...", model_path)
        self._model = YOLO(model_path)
        self._confidence = confidence

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Returns person bounding boxes as (x1, y1, x2, y2)."""
        results = self._model.predict(
            frame, classes=[PERSON_CLASS_ID], conf=self._confidence, verbose=False
        )
        boxes: list[tuple[int, int, int, int]] = []
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                boxes.append((int(x1), int(y1), int(x2), int(y2)))
        return boxes


# --------------------------------------------------------------------------
# Simple centroid tracker — assigns a stable track_id to each person box
# across frames, so "first appearance" is well defined without a full
# re-identification model.
# --------------------------------------------------------------------------

class CentroidTracker:
    def __init__(self, max_disappeared: int, max_distance: float):
        self._next_id = 0
        self.objects: dict[int, tuple[int, int]] = {}       # track_id -> centroid
        self.bboxes: dict[int, tuple[int, int, int, int]] = {}
        self._disappeared: dict[int, int] = {}
        self._max_disappeared = max_disappeared
        self._max_distance = max_distance

    def _register(self, centroid, bbox) -> int:
        track_id = self._next_id
        self._next_id += 1
        self.objects[track_id] = centroid
        self.bboxes[track_id] = bbox
        self._disappeared[track_id] = 0
        return track_id

    def _deregister(self, track_id: int) -> None:
        self.objects.pop(track_id, None)
        self.bboxes.pop(track_id, None)
        self._disappeared.pop(track_id, None)

    def update(self, rects: list[tuple[int, int, int, int]]) -> dict[int, tuple[int, int, int, int]]:
        if not rects:
            for track_id in list(self._disappeared.keys()):
                self._disappeared[track_id] += 1
                if self._disappeared[track_id] > self._max_disappeared:
                    self._deregister(track_id)
            return dict(self.bboxes)

        input_centroids = [((x1 + x2) // 2, (y1 + y2) // 2) for (x1, y1, x2, y2) in rects]

        if not self.objects:
            for centroid, bbox in zip(input_centroids, rects):
                self._register(centroid, bbox)
            return dict(self.bboxes)

        track_ids = list(self.objects.keys())
        track_centroids = [self.objects[tid] for tid in track_ids]

        dist_matrix = np.linalg.norm(
            np.array(track_centroids)[:, None, :] - np.array(input_centroids)[None, :, :], axis=2
        )
        rows = dist_matrix.min(axis=1).argsort()
        cols = dist_matrix.argmin(axis=1)[rows]

        used_rows, used_cols = set(), set()
        for row, col in zip(rows, cols):
            if row in used_rows or col in used_cols:
                continue
            if dist_matrix[row, col] > self._max_distance:
                continue
            track_id = track_ids[row]
            self.objects[track_id] = input_centroids[col]
            self.bboxes[track_id] = rects[col]
            self._disappeared[track_id] = 0
            used_rows.add(row)
            used_cols.add(col)

        unused_rows = set(range(dist_matrix.shape[0])) - used_rows
        for row in unused_rows:
            track_id = track_ids[row]
            self._disappeared[track_id] += 1
            if self._disappeared[track_id] > self._max_disappeared:
                self._deregister(track_id)

        unused_cols = set(range(dist_matrix.shape[1])) - used_cols
        for col in unused_cols:
            self._register(input_centroids[col], rects[col])

        return dict(self.bboxes)


# --------------------------------------------------------------------------
# Face identification against the local known_faces/ folder
# --------------------------------------------------------------------------

class FaceIdentifier:
    def __init__(self, known_faces_dir: Path, tolerance: float):
        self._tolerance = tolerance
        self._names: list[str] = []
        self._encodings: list[np.ndarray] = []
        self._load_known_faces(known_faces_dir)

    def _load_known_faces(self, directory: Path) -> None:
        if not directory.is_dir():
            log.warning("known_faces directory %s does not exist — everyone will be unverified.", directory)
            return

        image_paths = sorted(
            p for p in directory.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        if not image_paths:
            log.warning("No reference images found in %s — everyone will be unverified.", directory)
            return

        for path in image_paths:
            display_name = path.stem.replace("_", " ").strip().title()
            try:
                image = face_recognition.load_image_file(str(path))
                encodings = face_recognition.face_encodings(image)
            except Exception as e:
                log.warning("Failed to read %s: %s — skipping.", path.name, e)
                continue
            if not encodings:
                log.warning("No face found in %s — skipping (unencodable reference photo).", path.name)
                continue
            self._names.append(display_name)
            self._encodings.append(encodings[0])
            log.info("Enrolled known face: %s", display_name)

        log.info("Loaded %d known face(s) from %s", len(self._names), directory)

    def identify(self, frame: np.ndarray) -> list[tuple[str, bool, tuple[int, int, int, int]]]:
        """Returns [(name_or_unverified_id, is_known, (x1, y1, x2, y2)), ...]
        for every face found in `frame`. Unknown faces get a fresh
        'unverified_visitor_<id>' handle per call — the caller is responsible
        for mapping that back to a stable track."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb)
        if not locations:
            return []
        encodings = face_recognition.face_encodings(rgb, locations)

        results: list[tuple[str, bool, tuple[int, int, int, int]]] = []
        for encoding, (top, right, bottom, left) in zip(encodings, locations):
            name, is_known = self._match(encoding)
            results.append((name, is_known, (left, top, right, bottom)))
        return results

    def _match(self, encoding: np.ndarray) -> tuple[str, bool]:
        if not self._encodings:
            return f"unverified_visitor_{uuid.uuid4().hex[:6]}", False
        distances = face_recognition.face_distance(self._encodings, encoding)
        best_idx = int(np.argmin(distances))
        if distances[best_idx] <= self._tolerance:
            return self._names[best_idx], True
        return f"unverified_visitor_{uuid.uuid4().hex[:6]}", False


# --------------------------------------------------------------------------
# JARVIS announcement hook
# --------------------------------------------------------------------------

class Announcer:
    """Speaks (or prints, if pyttsx3/no audio device is unavailable) a short
    summary once a verification record completes."""

    def __init__(self):
        self._engine = None
        if pyttsx3 is not None:
            try:
                self._engine = pyttsx3.init()
            except Exception as e:
                log.warning("pyttsx3 available but failed to initialize (%s) — falling back to text.", e)

    def announce(self, text: str) -> None:
        log.info("[JARVIS] %s", text)
        if self._engine is not None:
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception as e:
                log.warning("TTS playback failed (%s); message was: %s", e, text)


def summarize_record(record: VerificationRecord) -> str:
    label = record.identity if record.is_known else "an unverified guest"
    if record.status == VerificationStatus.FAILED:
        return f"Verification failed for {label}: {record.error}"
    profile = record.profile or {}
    clearance = profile.get("authorized_clearance", "UNKNOWN")
    status = profile.get("activity_status", "UNKNOWN")
    history_count = len(profile.get("log_history", []))
    return (
        f"I have identified {label}. Clearance level {clearance}, status {status}. "
        f"{history_count} prior access log entries on file."
    )


# --------------------------------------------------------------------------
# Access log persistence (local file, this session only — purely for the
# "manage access logs" part of the demo brief; not sent anywhere)
# --------------------------------------------------------------------------

class AccessLogger:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def log(self, record: VerificationRecord) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "identity": record.identity,
            "is_known": record.is_known,
            "status": record.status.value,
            "profile": record.profile,
            "error": record.error,
        }
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("Could not write access log entry: %s", e)


# --------------------------------------------------------------------------
# Overlay rendering helpers
# --------------------------------------------------------------------------

def draw_overlay(frame: np.ndarray, bbox: tuple[int, int, int, int], label: str, color: tuple[int, int, int]) -> None:
    x1, y1, x2, y2 = bbox
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    text_y = max(0, y1 - 10)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.rectangle(frame, (x1, text_y - th - 6), (x1 + tw + 6, text_y + 4), color, -1)
    cv2.putText(frame, label, (x1 + 3, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


def status_label(identity: str, record: Optional[VerificationRecord]) -> tuple[str, tuple[int, int, int]]:
    if record is None or record.status in (VerificationStatus.PENDING, VerificationStatus.IN_PROGRESS):
        return f"{identity} | checking...", (0, 200, 255)      # amber
    if record.status == VerificationStatus.FAILED:
        return f"{identity} | verification failed", (0, 0, 255)  # red
    profile = record.profile or {}
    clearance = profile.get("authorized_clearance", "?")
    status = profile.get("activity_status", "?")
    color = (0, 220, 0) if record.is_known else (0, 140, 255)   # green vs orange
    return f"{identity} | {clearance} / {status}", color


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

class JarvisAccessDashboard:
    def __init__(self, config: Config):
        self._config = config
        self._stream = ThreadedVideoStream(config.camera_index)
        self._detector = PersonDetector(config.yolo_model, config.detection_confidence)
        self._identifier = FaceIdentifier(config.known_faces_dir, config.face_match_tolerance)
        self._tracker = CentroidTracker(config.max_track_disappeared_frames, config.track_match_max_distance)
        self._announcer = Announcer()
        self._access_logger = AccessLogger(config.access_log_path)
        self._engine = VerificationEngine(config, on_complete=self._on_verification_complete)

        self._track_identity: dict[int, str] = {}     # track_id -> resolved identity
        self._track_is_known: dict[int, bool] = {}
        self._announced: set[str] = set()
        self._state_lock = threading.Lock()
        self._frame_count = 0

    def _on_verification_complete(self, record: VerificationRecord) -> None:
        self._access_logger.log(record)
        with self._state_lock:
            already = record.identity in self._announced
            self._announced.add(record.identity)
        if not already:
            self._announcer.announce(summarize_record(record))

    def _resolve_identities(self, frame: np.ndarray, tracks: dict[int, tuple[int, int, int, int]]) -> None:
        """Runs (expensive) face recognition and assigns results to the
        nearest still-unresolved track by centroid distance."""
        unresolved = [tid for tid in tracks if tid not in self._track_identity]
        if not unresolved:
            return

        faces = self._identifier.identify(frame)
        if not faces:
            return

        for name, is_known, (fx1, fy1, fx2, fy2) in faces:
            face_center = ((fx1 + fx2) // 2, (fy1 + fy2) // 2)
            best_tid, best_dist = None, float("inf")
            for tid in unresolved:
                x1, y1, x2, y2 = tracks[tid]
                pcx, pcy = (x1 + x2) // 2, (y1 + y2) // 2
                dist = ((face_center[0] - pcx) ** 2 + (face_center[1] - pcy) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist, best_tid = dist, tid
            if best_tid is not None and best_dist < self._config.track_match_max_distance:
                self._track_identity[best_tid] = name
                self._track_is_known[best_tid] = is_known
                unresolved.remove(best_tid)

    def _prune_stale_tracks(self, live_track_ids: set[int]) -> None:
        stale = set(self._track_identity) - live_track_ids
        for tid in stale:
            self._track_identity.pop(tid, None)
            self._track_is_known.pop(tid, None)

    def run(self) -> None:
        log.info("Starting dashboard. Press 'q' in the video window to quit.")
        try:
            while True:
                ok, frame = self._stream.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                self._frame_count += 1
                person_boxes = self._detector.detect(frame)
                tracks = self._tracker.update(person_boxes)
                self._prune_stale_tracks(set(tracks.keys()))

                if self._frame_count % self._config.face_recognition_every_n_frames == 0:
                    self._resolve_identities(frame, tracks)

                for track_id, bbox in tracks.items():
                    identity = self._track_identity.get(track_id)
                    if identity is None:
                        draw_overlay(frame, bbox, "detecting face...", (200, 200, 200))
                        continue

                    is_known = self._track_is_known.get(track_id, False)
                    record = self._engine.request_verification(identity, is_known)
                    label, color = status_label(identity, record)
                    draw_overlay(frame, bbox, label, color)

                cv2.imshow("Project JARVIS — Access Dashboard (demo)", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    log.info("'q' pressed — shutting down.")
                    break
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down.")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("Releasing camera and closing windows...")
        self._stream.stop()
        cv2.destroyAllWindows()
        log.info("Waiting for in-flight verification workers to finish...")
        self._engine.shutdown()
        log.info("Clean shutdown complete.")


def main() -> None:
    config = Config()
    try:
        dashboard = JarvisAccessDashboard(config)
    except RuntimeError as e:
        log.error("Startup failed: %s", e)
        return
    dashboard.run()


if __name__ == "__main__":
    main()
