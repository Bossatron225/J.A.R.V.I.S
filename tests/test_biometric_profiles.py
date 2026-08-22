import asyncio
import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import auth as auth_module
from actions import camera_session as camera_session_module
from actions import file_controller as file_controller_module
import main as main_module
import ui as ui_module
from ui import ManageProfilesOverlay

cv2 = pytest.importorskip("cv2")


@pytest.fixture(autouse=True)
def _isolate_camera_sessions():
    # get_camera_session() caches one CameraSession per index at module scope
    # — clear it so a fake-subprocess session from this file's tests never
    # leaks into (or is polluted by) another test file's session state.
    camera_session_module._sessions.clear()
    yield
    camera_session_module._sessions.clear()


class _FakeCameraStream:
    """Real os.pipe()-backed file object so the CameraSession reader thread's
    blocking .read(n) calls behave like a real subprocess pipe."""

    def __init__(self):
        r_fd, w_fd = os.pipe()
        self.read_file = os.fdopen(r_fd, "rb")
        self.write_file = os.fdopen(w_fd, "wb")

    def send_frame(self, jpeg_bytes: bytes) -> None:
        self.write_file.write(len(jpeg_bytes).to_bytes(4, "big"))
        self.write_file.write(jpeg_bytes)
        self.write_file.flush()

    def close_write_end(self) -> None:
        self.write_file.close()


class _FakeCameraProcess:
    def __init__(self):
        self.stdout_pipe = _FakeCameraStream()
        self.stderr_pipe = _FakeCameraStream()
        self.stdout = self.stdout_pipe.read_file
        self.stderr = self.stderr_pipe.read_file
        self._terminated = threading.Event()

    def poll(self):
        return 0 if self._terminated.is_set() else None

    def terminate(self):
        self._terminated.set()
        self.stdout_pipe.close_write_end()
        self.stderr_pipe.close_write_end()

    def kill(self):
        self._terminated.set()

    def wait(self, timeout=None):
        if not self._terminated.wait(timeout=timeout):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


def _encode_blank_frame():
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _prime_camera_session_with_a_frame(monkeypatch, camera_index: int = 0):
    """CameraSession now holds the camera open via a persistent subprocess —
    fake that subprocess and prime one frame into the session before the
    function under test does its own (synchronous, no-wait) get_frame() loop,
    since frame delivery is now asynchronous (a background reader thread)."""
    proc = _FakeCameraProcess()
    monkeypatch.setattr(camera_session_module.subprocess, "Popen", lambda *a, **k: proc)

    session = camera_session_module.get_camera_session(camera_index)
    session.get_frame()  # triggers the (fake) subprocess launch
    proc.stdout_pipe.send_frame(_encode_blank_frame())

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if session.get_frame() is not None:
            break
        time.sleep(0.02)


requires_sface = pytest.mark.skipif(
    not (auth_module._HAS_SFACE and auth_module._SFACE_MODEL_PATH.exists()),
    reason="requires opencv-contrib-python's FaceRecognizerSF plus the bundled SFace model file",
)


def _make_synthetic_embedding(seed: int = 0):
    """A fake 128-d SFace embedding standing in for one "identity" — deterministic
    per seed so distinct seeds behave like distinct people."""
    rng = np.random.default_rng(seed)
    vec = rng.normal(0, 1, size=128).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _fake_embed_bytes(base_embedding):
    """Return a monkeypatch target standing in for auth._embed_image_bytes: each
    "sample" is the same underlying identity embedding with a small deterministic
    amount of per-sample noise, mirroring real-world lighting/pose variance. Only the
    (needs a real face image) detect+align+embed step is faked — the actual
    cosine-similarity matching still runs for real via cv2.FaceRecognizerSF.match."""

    def _fake(image_bytes):
        seed = image_bytes[0] if image_bytes else 0
        noise = np.random.default_rng(seed).normal(0, 0.02, size=base_embedding.shape)
        noisy = (base_embedding + noise).astype(np.float32)
        return noisy / np.linalg.norm(noisy)

    return _fake


def test_enroll_biometric_profile_stores_voice_and_visual_signatures(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {"primary": {"name": "James Lumsden", "voice_prints": [], "visual_signatures": [], "clearance_level": "omega"}, "authorized": {}},
    )
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", set())

    result = file_controller_module.enroll_biometric_profile(
        profile_id="james",
        name="James Lumsden",
        voice_print="Hello, this is James Lumsden",
        visual_signature="James Lumsden face",
        clearance_level="omega",
        make_primary=True,
    )

    assert "Enrolled biometric profile" in result
    primary = file_controller_module._AUTHORIZED_PROFILES["primary"]
    assert any("james" in item for item in primary["voice_prints"])
    assert any("james" in item for item in primary["visual_signatures"])


def test_verify_biometric_security_matches_enrolled_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {
            "primary": {
                "name": "James Lumsden",
                "voice_prints": ["james lumsden voice"],
                "visual_signatures": ["james lumsden face"],
                "clearance_level": "omega",
            },
            "authorized": {},
        },
    )
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    file_controller_module.verify_biometric_security.cache_clear()

    assert file_controller_module.verify_biometric_security("voice sample for james lumsden", "") is True
    assert file_controller_module.verify_biometric_security("", "visual scan of james lumsden face") is True


def test_security_biometrics_status_reports_primary_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {"primary": {"name": "James Lumsden", "voice_prints": [], "visual_signatures": [], "clearance_level": "omega"}, "authorized": {}},
    )

    result = file_controller_module.security_biometrics(parameters={"action": "status"})

    assert "James Lumsden" in result


def test_security_biometrics_detect_person_matches_enrolled_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {
            "primary": {
                "name": "James Lumsden",
                "voice_prints": [],
                "visual_signatures": ["james lumsden face"],
                "clearance_level": "omega",
            },
            "authorized": {},
        },
    )
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james lumsden"})
    file_controller_module.verify_biometric_security.cache_clear()

    result = file_controller_module.security_biometrics(parameters={
        "action": "detect_person",
        "target_identity": "james lumsden",
        "visual_signature": "james lumsden face",
    })

    assert "identified as authorized user" in result


def test_security_biometrics_is_callable_via_local_worker_relay(monkeypatch) -> None:
    """Same function, reached through the VPS-to-Mac relay path (local_worker.py)
    instead of main.py's in-process tool dispatch — regression test for the gap
    where this tool previously had no ACTION_HANDLERS entry."""
    from local_worker import LocalWorker

    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {"primary": {"name": "James Lumsden", "voice_prints": [], "visual_signatures": [], "clearance_level": "omega"}, "authorized": {}},
    )

    worker = LocalWorker()
    result = worker.execute_local_action("security_biometrics", {"action": "status"})

    assert result["status"] == "completed"
    assert "James Lumsden" in result["result"]


def test_default_profile_registry_contains_only_primary_profile() -> None:
    profiles = file_controller_module.get_authorized_profiles()

    assert profiles["primary"]["name"] == "James Lumsden"
    assert profiles["authorized"] == {}


def test_manage_profiles_overlay_uses_profile_file_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ui.PROFILES_FILE", tmp_path / "authorized_profiles.json")
    monkeypatch.setattr("ui.CONFIG_DIR", tmp_path)

    overlay = ManageProfilesOverlay(parent=None)
    profiles = overlay._get_profiles()

    assert profiles[0]["name"] == "James Lumsden"
    assert profiles[0]["id"] == "JAMES-001"


def test_biometric_lock_overlay_does_not_clear_on_failed_verification(monkeypatch) -> None:
    monkeypatch.setattr(file_controller_module, "_SECURITY_ENABLED", True)
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PROFILES", {
        "primary": {
            "name": "James Lumsden",
            "voice_prints": ["james lumsden"],
            "visual_signatures": ["james lumsden"],
            "clearance_level": "omega",
        },
        "authorized": {},
    })
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    file_controller_module.verify_biometric_security.cache_clear()

    app = ui_module._ensure_qapplication()
    parent = ui_module.QWidget()
    overlay = ui_module.BiometricLockOverlay(parent=parent)
    if not getattr(overlay, "_qt_ready", False):
        return

    overlay._status_lbl.setText("STATUS: READY")
    overlay._voice_chk.setText("🎙️ Voice Recognition: PENDING")
    overlay._visual_chk.setText("👁️ Visual Person Detection: PENDING")

    monkeypatch.setattr(ui_module, "verify_biometric_security", lambda voice, visual: False)
    overlay._step_voice()
    overlay._step_visual()

    assert overlay._status_lbl.text().startswith("STATUS: PROFILE NOT VERIFIED")
    assert overlay._scan_btn.isEnabled() is True
    assert overlay._scan_btn.text() == "RETRY BIOMETRIC SCAN"


def test_live_biometric_helper_requires_audio_and_face(monkeypatch) -> None:
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PROFILES", {
        "primary": {
            "name": "James Lumsden",
            "voice_prints": ["james lumsden"],
            "visual_signatures": ["james lumsden"],
            "clearance_level": "omega",
        },
        "authorized": {},
    })
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"\x00", 0.0))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", False))

    granted, details = file_controller_module.evaluate_live_biometric_security("James Lumsden")

    assert granted is False
    assert details["voice_detected"] is False
    assert details["visual_detected"] is False


def test_live_biometric_helper_accepts_live_capture_for_matching_identity(monkeypatch) -> None:
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PROFILES", {
        "primary": {
            "name": "James Lumsden",
            "voice_prints": ["james lumsden"],
            "visual_signatures": ["james lumsden"],
            "clearance_level": "omega",
        },
        "authorized": {},
    })
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"audio-sample", 0.05))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", True))
    monkeypatch.setattr(file_controller_module, "_verify_live_voice_with_gemini", lambda *args, **kwargs: True)
    monkeypatch.setattr(file_controller_module, "_verify_reference_face_match", lambda: (True, "ok"))

    granted, details = file_controller_module.evaluate_live_biometric_security("James Lumsden")

    assert granted is True
    assert details["voice_detected"] is True
    assert details["visual_detected"] is True


def test_live_biometric_helper_rejects_when_no_live_face(monkeypatch) -> None:
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PROFILES", {
        "primary": {
            "name": "James Lumsden",
            "voice_prints": ["james lumsden"],
            "visual_signatures": ["james lumsden"],
            "clearance_level": "omega",
        },
        "authorized": {},
    })
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"audio-sample", 0.05))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", False))
    monkeypatch.setattr(file_controller_module, "_verify_live_voice_with_gemini", lambda *args, **kwargs: True)
    monkeypatch.setattr(file_controller_module, "_verify_reference_face_match", lambda: (False, "no-match"))

    granted, details = file_controller_module.evaluate_live_biometric_security("James Lumsden")

    assert granted is False
    assert details["voice_detected"] is True
    assert details["visual_detected"] is False


def test_record_voice_sample_uses_speech_recognition_fallback(monkeypatch) -> None:
    class FakeAudioData:
        def get_raw_data(self, convert_rate=16000, convert_width=2):
            return b"\x00\x00\x00\x00"

    class FakeRecognizer:
        def __init__(self, *args, **kwargs):
            pass

        def listen(self, mic, timeout=None, phrase_time_limit=None):
            return FakeAudioData()

    class FakeMicrophone:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSpeechModule:
        Recognizer = FakeRecognizer
        Microphone = FakeMicrophone

    monkeypatch.setattr(file_controller_module, "sd", None)
    monkeypatch.setattr(file_controller_module, "sr", FakeSpeechModule())

    audio_bytes, rms = file_controller_module._record_voice_sample(duration_seconds=1.2)

    assert audio_bytes == b"\x00\x00\x00\x00"
    assert rms >= 0.0


def test_manage_profiles_overlay_supports_capture_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ui.PROFILES_FILE", tmp_path / "authorized_profiles.json")
    monkeypatch.setattr("ui.CONFIG_DIR", tmp_path)

    monkeypatch.setattr("ui.evaluate_live_biometric_security", lambda identity: (True, {"voice_detected": True, "visual_detected": True}))

    overlay = ManageProfilesOverlay(parent=None)
    overlay._set_capture_state("recording")
    assert "SPEAK NOW" in overlay._capture_state_text

    overlay._show_capture_confirmation("Baseline ready")
    assert overlay._confirm_btn is not None
    assert overlay._capture_state_text == "● READY"
    assert overlay._capture_state_text == "● READY"


def test_jarvis_live_blocks_text_commands_during_biometric_lock(monkeypatch) -> None:
    class DummyUI:
        def __init__(self) -> None:
            self.muted = False
            self._biometric_lock_active = True

        def set_state(self, *_args, **_kwargs) -> None:
            return None

        def write_log(self, *_args, **_kwargs) -> None:
            return None

        def is_biometric_lock_active(self) -> bool:
            return self._biometric_lock_active

    ui = DummyUI()
    live = main_module.JarvisLive(ui)
    live._predictive_daemon = SimpleNamespace(record_text_command=lambda *_args, **_kwargs: None)
    live._loop = object()
    live.session = object()
    sent = []

    monkeypatch.setattr(main_module.asyncio, "run_coroutine_threadsafe", lambda *args, **kwargs: sent.append((args, kwargs)))
    monkeypatch.setattr(live, "_maybe_handle_remote_url_request", lambda *_args, **_kwargs: False)

    live._on_text_command("hello")

    assert sent == []


def test_jarvis_live_handles_biometric_failure(monkeypatch) -> None:
    class DummyUI:
        def __init__(self) -> None:
            self.muted = False

        def set_state(self, *_args, **_kwargs) -> None:
            return None

        def write_log(self, *_args, **_kwargs) -> None:
            return None

    ui = DummyUI()
    live = main_module.JarvisLive(ui)
    shutdown_reasons = []
    monkeypatch.setattr(live, "_schedule_shutdown", lambda reason: shutdown_reasons.append(reason) or True)

    live._handle_biometric_failure()

    assert shutdown_reasons == ["biometric verification failed"]


def test_schedule_shutdown_without_running_loop(monkeypatch) -> None:
    class DummyUI:
        def __init__(self) -> None:
            self.muted = False
            self.logs = []

        def set_state(self, *_args, **_kwargs) -> None:
            return None

        def write_log(self, message: str, *_args, **_kwargs) -> None:
            self.logs.append(message)

    ui = DummyUI()
    live = main_module.JarvisLive(ui)
    called = []

    def fake_run(coro):
        called.append(coro)
        return None

    monkeypatch.setattr(main_module.asyncio, "run", fake_run)

    result = live._schedule_shutdown("test reason")

    assert result is True
    assert called


def test_audio_callbacks_are_blocked_during_biometric_lock() -> None:
    class DummyUI:
        def __init__(self) -> None:
            self.muted = False
            self._biometric_lock_active = True

        def set_state(self, *_args, **_kwargs) -> None:
            return None

        def write_log(self, *_args, **_kwargs) -> None:
            return None

        def is_biometric_lock_active(self) -> bool:
            return self._biometric_lock_active

    ui = DummyUI()
    live = main_module.JarvisLive(ui)
    live.out_queue = asyncio.Queue()
    live.audio_in_queue = asyncio.Queue()

    live._enqueue_outgoing_audio(b"abc")
    asyncio.run(live._enqueue_incoming_audio(b"def"))

    assert live.out_queue.empty()
    assert live.audio_in_queue.empty()


@requires_sface
def test_train_face_model_learns_from_multiple_angle_samples(monkeypatch, tmp_path) -> None:
    base_embedding = _make_synthetic_embedding(seed=1)
    monkeypatch.setattr(auth_module, "_embed_image_bytes", _fake_embed_bytes(base_embedding))

    samples = [bytes([i]) for i in range(6)]
    ok, model_bytes, message = auth_module.train_face_model(samples)

    assert ok is True
    assert model_bytes
    assert "6 face sample" in message

    saved_path = auth_module.save_face_model("test_profile", model_bytes)
    try:
        assert saved_path.exists()
        loaded = auth_module.load_face_model("test_profile")
        assert loaded is not None
        assert loaded.shape == (6, 128)

        sface = auth_module._load_sface_recognizer()
        matching_embedding = _fake_embed_bytes(base_embedding)(bytes([99]))
        matching_score = auth_module._best_cosine_match(sface, matching_embedding, loaded)

        different_embedding = _make_synthetic_embedding(seed=42)
        different_score = auth_module._best_cosine_match(sface, different_embedding, loaded)

        assert matching_score >= auth_module._SFACE_DEFAULT_THRESHOLD
        assert different_score < matching_score
    finally:
        saved_path.unlink(missing_ok=True)


def test_train_face_model_rejects_too_few_samples(monkeypatch) -> None:
    monkeypatch.setattr(auth_module, "_embed_image_bytes", lambda image_bytes: np.zeros(128, dtype=np.float32))

    ok, model_bytes, message = auth_module.train_face_model([b"only-one"])

    assert ok is False
    assert model_bytes is None
    assert "need at least" in message


@requires_sface
def test_verify_face_against_model_matches_and_rejects(monkeypatch) -> None:
    base_embedding = _make_synthetic_embedding(seed=2)
    monkeypatch.setattr(auth_module, "_embed_image_bytes", _fake_embed_bytes(base_embedding))

    samples = [bytes([i]) for i in range(5)]
    ok, model_bytes, _ = auth_module.train_face_model(samples)
    assert ok is True
    stored = auth_module.np.load(auth_module.io.BytesIO(model_bytes), allow_pickle=False)

    _prime_camera_session_with_a_frame(monkeypatch)

    matching_embedding = _fake_embed_bytes(base_embedding)(bytes([7]))
    monkeypatch.setattr(auth_module, "_detect_and_embed", lambda frame: matching_embedding)
    matched, reason = auth_module.verify_face_against_model(stored, num_frames=1)
    assert matched is True, reason

    different_embedding = _make_synthetic_embedding(seed=99)
    monkeypatch.setattr(auth_module, "_detect_and_embed", lambda frame: different_embedding)
    rejected, reason2 = auth_module.verify_face_against_model(stored, num_frames=1)
    assert rejected is False, reason2


def _setup_lbph_evaluation(monkeypatch, model_match: bool, model_reason: str) -> None:
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PROFILES", {
        "primary": {
            "name": "James Lumsden",
            "voice_prints": ["james lumsden"],
            "visual_signatures": ["james lumsden"],
            "clearance_level": "omega",
        },
        "authorized": {},
    })
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", {"james", "james lumsden"})
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"audio-sample", 0.05))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", True))
    monkeypatch.setattr(file_controller_module, "_verify_live_voice_with_gemini", lambda *args, **kwargs: True)
    # Legacy/network signals all say "yes" — the trained model must be authoritative regardless.
    monkeypatch.setattr(file_controller_module, "_verify_reference_face_match", lambda: (True, "ok"))
    monkeypatch.setattr(file_controller_module, "_load_primary_face_model", lambda: object())
    monkeypatch.setattr(file_controller_module, "_verify_live_face_with_trained_model", lambda model: (model_match, model_reason))


def test_evaluate_live_biometric_security_lbph_rejects_despite_legacy_yes(monkeypatch) -> None:
    _setup_lbph_evaluation(monkeypatch, model_match=False, model_reason="Face did not match trained model (confidence=120.0, threshold=75.0)")

    granted, details = file_controller_module.evaluate_live_biometric_security("James Lumsden")

    assert granted is False
    assert details["visual_detected"] is False
    assert details["visual_engine"] == "lbph"


def test_evaluate_live_biometric_security_lbph_accepts_trained_match(monkeypatch) -> None:
    _setup_lbph_evaluation(monkeypatch, model_match=True, model_reason="Face matched trained model (confidence=30.0, threshold=75.0)")

    granted, details = file_controller_module.evaluate_live_biometric_security("James Lumsden")

    assert granted is True
    assert details["visual_detected"] is True
    assert details["visual_engine"] == "lbph"


def test_enroll_biometric_profile_trains_model_from_visual_samples(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module,
        "_AUTHORIZED_PROFILES",
        {"primary": {"name": "James Lumsden", "voice_prints": [], "visual_signatures": [], "clearance_level": "omega"}, "authorized": {}},
    )
    monkeypatch.setattr(file_controller_module, "_AUTHORIZED_PERSONNEL", set())
    monkeypatch.setattr(
        file_controller_module,
        "_train_and_save_face_model",
        lambda profile_key, visual_samples: f"model trained: {len(visual_samples)} sample(s)",
    )

    result = file_controller_module.enroll_biometric_profile(
        profile_id="james",
        name="James Lumsden",
        voice_print="James Lumsden",
        visual_signature="James Lumsden",
        clearance_level="omega",
        make_primary=True,
        visual_samples=[b"sample-1", b"sample-2", b"sample-3"],
    )

    assert "model trained: 3 sample(s)" in result
    primary = file_controller_module._AUTHORIZED_PROFILES["primary"]
    assert len(primary["visual_samples"]) == 3


def test_is_worker_mode_requires_explicit_worker_env(monkeypatch) -> None:
    """Regression: MainWindow.__init__ used to gate the face/voice scan UI on whether
    JARVIS_VPS_URL was set, not on actual worker mode. run_local.sh (interactive,
    attended) sets JARVIS_VPS_URL too, so the scan UI never showed on the desktop
    app — it just sat locked with no way to clear it short of the override code."""
    monkeypatch.delenv("JARVIS_MODE", raising=False)
    assert ui_module._is_worker_mode() is False

    monkeypatch.setenv("JARVIS_VPS_URL", "http://vps.example.com")
    assert ui_module._is_worker_mode() is False  # VPS URL alone must not imply worker mode

    monkeypatch.setenv("JARVIS_MODE", "interactive")
    assert ui_module._is_worker_mode() is False

    monkeypatch.setenv("JARVIS_MODE", "worker")
    assert ui_module._is_worker_mode() is True


def test_jarvis_ui_forwards_biometric_lock_state() -> None:
    """Regression: JarvisUI is a hand-written forwarding wrapper around MainWindow
    with no __getattr__ fallback. is_biometric_lock_active/unlock_via_override were
    missing from that forwarding surface, so every `getattr(self.ui, "is_biometric_lock_active",
    lambda: False)()` check in main.py silently evaluated to "unlocked" on the real
    desktop app — the lock UI showed, but nothing was actually enforced."""

    class StubWindow:
        def __init__(self) -> None:
            self.locked = True

        def is_biometric_lock_active(self) -> bool:
            return self.locked

        def unlock_via_override(self, code: str):
            return (code == "correct-code", "unlock message")

    wrapper = ui_module.JarvisUI.__new__(ui_module.JarvisUI)
    wrapper._window_alive = True
    wrapper._win = StubWindow()

    assert wrapper.is_biometric_lock_active() is True
    wrapper._win.locked = False
    assert wrapper.is_biometric_lock_active() is False
    assert wrapper.unlock_via_override("correct-code") == (True, "unlock message")


def test_jarvis_ui_fails_closed_when_window_gone() -> None:
    wrapper = ui_module.JarvisUI.__new__(ui_module.JarvisUI)
    wrapper._window_alive = False
    wrapper._win = None

    assert wrapper.is_biometric_lock_active() is True
    ok, _ = wrapper.unlock_via_override("anything")
    assert ok is False


def test_local_speech_gate_active_through_real_jarvis_ui_wrapper() -> None:
    """End-to-end version of the regression above, through JarvisLive itself."""

    class StubWindow:
        def is_biometric_lock_active(self) -> bool:
            return True

    wrapper = ui_module.JarvisUI.__new__(ui_module.JarvisUI)
    wrapper._window_alive = True
    wrapper._win = StubWindow()

    live = main_module.JarvisLive(wrapper)

    assert live._local_speech_gate_active() is True


def test_local_speech_gate_active_when_desktop_locked() -> None:
    class DummyDesktopUI:
        def is_biometric_lock_active(self) -> bool:
            return True

    live = main_module.JarvisLive(DummyDesktopUI())

    assert live._local_speech_gate_active() is True


def test_local_speech_gate_inactive_when_desktop_unlocked() -> None:
    class DummyDesktopUI:
        def is_biometric_lock_active(self) -> bool:
            return False

    live = main_module.JarvisLive(DummyDesktopUI())

    assert live._local_speech_gate_active() is False


def test_local_speech_gate_never_active_on_headless_vps(monkeypatch) -> None:
    headless = main_module._HeadlessUI.__new__(main_module._HeadlessUI)
    headless._biometric_locked = True
    live = main_module.JarvisLive(headless)

    # Even though the VPS/headless instance itself is locked, the local-only speech
    # gate must not apply to it — it has no mic/speaker in the room to clash with,
    # and (unlike the desktop) starts locked with no automatic unlock path.
    assert live._local_speech_gate_active() is False


def test_enqueue_tts_sentence_drops_while_locally_locked() -> None:
    class DummyDesktopUI:
        def is_biometric_lock_active(self) -> bool:
            return True

    live = main_module.JarvisLive(DummyDesktopUI())
    live._tts_sentence_queue = asyncio.Queue()

    live._enqueue_tts_sentence("hello there")

    assert live._tts_sentence_queue.empty()


def test_speak_external_tts_drops_while_locally_locked() -> None:
    class DummyDesktopUI:
        def is_biometric_lock_active(self) -> bool:
            return True

    class DummyTTSPlayer:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def speak(self, text: str) -> None:
            self.spoken.append(text)

    live = main_module.JarvisLive(DummyDesktopUI())
    player = DummyTTSPlayer()
    live._tts_player = player

    asyncio.run(live._speak_external_tts("hello there"))

    assert player.spoken == []


def test_voice_enroll_now_captures_face_samples(monkeypatch) -> None:
    """Regression: security_biometrics(action='enroll') used to register a
    profile with only TEXT signatures and never pass visual_samples, so no face
    model was ever trained. config/biometric_models/ stayed empty while the
    call reported success — and the enrolled user was then logged as an unknown
    visitor by their own Nanny-cam."""
    captured = {}

    monkeypatch.setattr(
        file_controller_module, "capture_enrollment_samples",
        lambda *a, **k: [b"s1", b"s2", b"s3", b"s4"],
    )

    def _fake_baseline(name, visual_samples=None):
        captured["name"] = name
        captured["samples"] = visual_samples
        return True, "Baseline established."

    monkeypatch.setattr(file_controller_module, "establish_biometric_baseline", _fake_baseline)
    monkeypatch.setattr("auth.has_face_model", lambda key: True)

    result = file_controller_module.security_biometrics(
        {"action": "enroll", "name": "James Lumsden"}
    )

    assert captured["samples"] == [b"s1", b"s2", b"s3", b"s4"]
    assert "Face model trained: yes" in result


def test_voice_enroll_aborts_rather_than_training_on_too_few_samples(monkeypatch) -> None:
    monkeypatch.setattr(
        file_controller_module, "capture_enrollment_samples", lambda *a, **k: [b"only-one"]
    )
    called = {"baseline": False}
    monkeypatch.setattr(
        file_controller_module, "establish_biometric_baseline",
        lambda **k: called.__setitem__("baseline", True) or (True, "x"),
    )

    result = file_controller_module.security_biometrics({"action": "enroll"})

    assert "aborted" in result.lower()
    assert called["baseline"] is False


def test_capture_enrollment_samples_skips_frames_without_a_face(monkeypatch) -> None:
    """A dark room must yield few samples, not a model trained on empty frames."""
    import actions.camera_session as cs

    class _Session:
        def get_frame(self):
            return np.zeros((8, 8, 3), dtype=np.uint8)

    monkeypatch.setattr(cs, "get_camera_session", lambda idx=0: _Session())
    monkeypatch.setattr("auth._detect_and_embed", lambda frame: None)  # no face ever

    samples = file_controller_module.capture_enrollment_samples(
        target_count=5, timeout_seconds=0.8
    )

    assert samples == []
