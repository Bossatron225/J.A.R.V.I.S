import json

import numpy as np
import pytest

import auth as auth_module
import main as main_module
from actions import camera_session as camera_session_module
from actions import visitor_log as visitor_log_module

requires_sface = pytest.mark.skipif(
    not (auth_module._HAS_SFACE and auth_module._SFACE_MODEL_PATH.exists()),
    reason="requires opencv-contrib-python's FaceRecognizerSF plus the bundled SFace model file",
)


def _make_synthetic_embedding(seed: int = 0):
    rng = np.random.default_rng(seed)
    vec = rng.normal(0, 1, size=128).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _noisy(base_embedding, noise_seed: int):
    noise = np.random.default_rng(noise_seed).normal(0, 0.02, size=base_embedding.shape)
    noisy = (base_embedding + noise).astype(np.float32)
    return noisy / np.linalg.norm(noisy)


@pytest.fixture(autouse=True)
def _isolate_visitor_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(visitor_log_module, "LOG_PATH", tmp_path / "visitor_log.jsonl")
    monkeypatch.setattr(visitor_log_module, "CLUSTERS_PATH", tmp_path / "visitor_clusters.json")
    monkeypatch.setattr(visitor_log_module, "SNAPSHOTS_DIR", tmp_path / "visitor_snapshots")
    # CameraSession instances are cached per camera index at module scope — clear
    # that cache so each test gets a fresh session instead of reusing whatever a
    # prior test's fake capture left behind.
    monkeypatch.setattr(camera_session_module, "_sessions", {})


# ── auth.identify_face ───────────────────────────────────────────────────────

@requires_sface
def test_identify_face_matches_enrolled_profile(monkeypatch) -> None:
    profile_a = _make_synthetic_embedding(seed=1)
    profile_b = _make_synthetic_embedding(seed=2)
    models = {"primary": np.stack([profile_a]), "guest": np.stack([profile_b])}
    monkeypatch.setattr(auth_module, "load_face_model", lambda key: models.get(key))

    live = _noisy(profile_b, noise_seed=5)
    matched_key, score = auth_module.identify_face(live, ["primary", "guest"])

    assert matched_key == "guest"
    assert score >= auth_module._sface_threshold()


@requires_sface
def test_identify_face_returns_none_for_unrecognized_face(monkeypatch) -> None:
    profile_a = _make_synthetic_embedding(seed=1)
    models = {"primary": np.stack([profile_a])}
    monkeypatch.setattr(auth_module, "load_face_model", lambda key: models.get(key))

    stranger = _make_synthetic_embedding(seed=99)
    matched_key, score = auth_module.identify_face(stranger, ["primary"])

    assert matched_key is None


# ── auth.capture_unknown_visitor_check ──────────────────────────────────────

class _FakeCapture:
    def __init__(self, frame):
        self._frame = frame
        self._served = False

    def isOpened(self):
        return True

    def read(self):
        if self._served:
            return False, None
        self._served = True
        return True, self._frame

    def release(self):
        pass


@requires_sface
def test_capture_unknown_visitor_check_flags_unrecognized_face(monkeypatch) -> None:
    profile_a = _make_synthetic_embedding(seed=1)
    models = {"primary": np.stack([profile_a])}
    monkeypatch.setattr(auth_module, "load_face_model", lambda key: models.get(key))
    monkeypatch.setattr(auth_module.cv2, "VideoCapture", lambda idx: _FakeCapture(np.zeros((4, 4, 3), dtype=np.uint8)))

    stranger = _make_synthetic_embedding(seed=99)
    monkeypatch.setattr(auth_module, "_detect_and_embed", lambda frame: stranger)

    result = auth_module.capture_unknown_visitor_check(["primary"], num_frames=1)

    assert result is not None
    assert result["status"] == "unknown"


@requires_sface
def test_capture_unknown_visitor_check_recognizes_known_profile(monkeypatch) -> None:
    profile_a = _make_synthetic_embedding(seed=1)
    models = {"primary": np.stack([profile_a])}
    monkeypatch.setattr(auth_module, "load_face_model", lambda key: models.get(key))
    monkeypatch.setattr(auth_module.cv2, "VideoCapture", lambda idx: _FakeCapture(np.zeros((4, 4, 3), dtype=np.uint8)))

    matching = _noisy(profile_a, noise_seed=3)
    monkeypatch.setattr(auth_module, "_detect_and_embed", lambda frame: matching)

    result = auth_module.capture_unknown_visitor_check(["primary"], num_frames=1)

    assert result is not None
    assert result["status"] == "known"
    assert result["profile_key"] == "primary"


# ── actions/visitor_log.py ──────────────────────────────────────────────────

def test_record_unknown_sighting_clusters_same_stranger() -> None:
    stranger = _make_synthetic_embedding(seed=7)

    first = visitor_log_module.record_unknown_sighting(_noisy(stranger, 1))
    second = visitor_log_module.record_unknown_sighting(_noisy(stranger, 2))

    assert first["visitor_id"] == second["visitor_id"]
    assert second["sighting_count_at_time"] == 2


def test_record_unknown_sighting_treats_different_face_as_new_visitor() -> None:
    stranger_a = _make_synthetic_embedding(seed=7)
    stranger_b = _make_synthetic_embedding(seed=42)

    first = visitor_log_module.record_unknown_sighting(stranger_a)
    second = visitor_log_module.record_unknown_sighting(stranger_b)

    assert first["visitor_id"] != second["visitor_id"]
    assert second["sighting_count_at_time"] == 1


def test_list_recent_sightings_respects_limit_and_order() -> None:
    for seed in range(5):
        visitor_log_module.record_unknown_sighting(_make_synthetic_embedding(seed=seed))

    recent = visitor_log_module.list_recent_sightings(limit=3)

    assert len(recent) == 3
    timestamps = [r["ts"] for r in recent]
    assert timestamps == sorted(timestamps)


def test_visitor_log_tool_reports_no_visitors_when_empty() -> None:
    result = visitor_log_module.visitor_log({"action": "recent"})
    assert "no unrecognized visitors" in result.lower()


def test_visitor_log_tool_summarizes_sightings() -> None:
    visitor_log_module.record_unknown_sighting(_make_synthetic_embedding(seed=1))

    result = visitor_log_module.visitor_log({"action": "recent"})

    assert "unrecognized visitor" in result.lower()


# ── main._should_alert_visitor ──────────────────────────────────────────────

def test_should_alert_visitor_first_sighting_alerts() -> None:
    assert main_module._should_alert_visitor("abc123", {}, cooldown_seconds=1800, now=1000.0) is True


def test_should_alert_visitor_within_cooldown_suppressed() -> None:
    last_alert = {"abc123": 1000.0}
    assert main_module._should_alert_visitor("abc123", last_alert, cooldown_seconds=1800, now=1500.0) is False


def test_should_alert_visitor_after_cooldown_alerts_again() -> None:
    last_alert = {"abc123": 1000.0}
    assert main_module._should_alert_visitor("abc123", last_alert, cooldown_seconds=1800, now=3000.0) is True


def test_should_alert_visitor_different_visitor_not_gated_by_another() -> None:
    last_alert = {"abc123": 1000.0}
    assert main_module._should_alert_visitor("xyz789", last_alert, cooldown_seconds=1800, now=1001.0) is True


# ── main.JarvisLive._load_visitor_watch_config ──────────────────────────────

def test_load_visitor_watch_config_defaults_when_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", tmp_path / "missing.json")

    cfg = main_module.JarvisLive._load_visitor_watch_config()

    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == 45


def test_load_visitor_watch_config_reads_and_clamps_overrides(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps({
        "visitor_watch_enabled": False,
        "visitor_watch_interval_seconds": 5,  # below the 15s floor
        "visitor_watch_camera_index": 1,
        "visitor_watch_realert_cooldown_seconds": 999999,  # above the 86400s ceiling
    }), encoding="utf-8")
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", config_path)

    cfg = main_module.JarvisLive._load_visitor_watch_config()

    assert cfg["enabled"] is False
    assert cfg["interval_seconds"] == 15
    assert cfg["camera_index"] == 1
    assert cfg["realert_cooldown_seconds"] == 86400
