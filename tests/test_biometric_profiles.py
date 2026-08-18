import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import auth as auth_module
from actions import file_controller as file_controller_module
import main as main_module
import ui as ui_module
from ui import ManageProfilesOverlay

requires_face_module = pytest.mark.skipif(
    not auth_module._HAS_FACE_MODULE, reason="cv2.face requires opencv-contrib-python"
)


def _make_synthetic_face(freq_x: float = 1.0, freq_y: float = 1.0, phase: float = 0.0):
    """LBPH compares *texture*, so a flat/random image has no structure for it to key
    off of — use a smooth 2D sinusoid instead, which behaves like real face texture
    for testing purposes (distinct identities => distinct frequencies/phase)."""
    xx, yy = np.meshgrid(np.linspace(0, 4 * np.pi, 200), np.linspace(0, 4 * np.pi, 200))
    pattern = 128 + 100 * np.sin(xx * freq_x + phase) * np.cos(yy * freq_y)
    return np.clip(pattern, 0, 255).astype(np.uint8)


def _fake_extract_for_training(base_face):
    """Return a monkeypatch target standing in for auth._extract_face_for_training:
    each "sample" is the same underlying face with a small deterministic amount of
    per-sample noise, mirroring the lighting/pose variance real enrollment frames have."""

    def _fake(image_bytes):
        seed = image_bytes[0] if image_bytes else 0
        noise = np.random.default_rng(seed).integers(-8, 8, size=base_face.shape)
        return np.clip(base_face.astype(int) + noise, 0, 255).astype(np.uint8)

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


@requires_face_module
def test_train_face_model_learns_from_multiple_angle_samples(monkeypatch, tmp_path) -> None:
    base_face = _make_synthetic_face()
    monkeypatch.setattr(auth_module, "_extract_face_for_training", _fake_extract_for_training(base_face))

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

        matching_gray = np.clip(
            base_face.astype(int) + np.random.default_rng(99).integers(-8, 8, size=base_face.shape), 0, 255
        ).astype(np.uint8)
        _, matching_confidence = loaded.predict(matching_gray)

        different_gray = _make_synthetic_face(freq_x=0.7, freq_y=2.1, phase=1.3)
        _, different_confidence = loaded.predict(different_gray)

        assert matching_confidence <= auth_module._LBPH_DEFAULT_THRESHOLD
        assert different_confidence > matching_confidence
    finally:
        saved_path.unlink(missing_ok=True)


def test_train_face_model_rejects_too_few_samples(monkeypatch) -> None:
    monkeypatch.setattr(auth_module, "_extract_face_for_training", lambda image_bytes: np.zeros((200, 200), dtype=np.uint8))

    ok, model_bytes, message = auth_module.train_face_model([b"only-one"])

    assert ok is False
    assert model_bytes is None
    assert "need at least" in message


@requires_face_module
def test_verify_face_against_model_matches_and_rejects(monkeypatch) -> None:
    base_face = _make_synthetic_face()
    monkeypatch.setattr(auth_module, "_extract_face_for_training", _fake_extract_for_training(base_face))

    samples = [bytes([i]) for i in range(5)]
    ok, model_bytes, _ = auth_module.train_face_model(samples)
    assert ok is True

    recognizer = auth_module.cv2.face.LBPHFaceRecognizer_create()
    with tempfile.NamedTemporaryFile(suffix=".yml") as tmp:
        Path(tmp.name).write_bytes(model_bytes)
        recognizer.read(tmp.name)

    monkeypatch.setattr(auth_module, "_extract_primary_face", lambda gray, detector: gray)

    class FakeCapture:
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

    matching_gray = np.clip(
        base_face.astype(int) + np.random.default_rng(3).integers(-8, 8, size=base_face.shape), 0, 255
    ).astype(np.uint8)
    matching_frame = np.stack([matching_gray] * 3, axis=-1).astype(np.uint8)
    monkeypatch.setattr(auth_module.cv2, "VideoCapture", lambda idx: FakeCapture(matching_frame))
    matched, reason = auth_module.verify_face_against_model(recognizer, num_frames=1)
    assert matched is True, reason

    different_gray = _make_synthetic_face(freq_x=0.7, freq_y=2.1, phase=1.3)
    different_frame = np.stack([different_gray] * 3, axis=-1).astype(np.uint8)
    monkeypatch.setattr(auth_module.cv2, "VideoCapture", lambda idx: FakeCapture(different_frame))
    rejected, reason2 = auth_module.verify_face_against_model(recognizer, num_frames=1)
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
    monkeypatch.setattr(file_controller_module, "_verify_live_face_with_gemini", lambda *args, **kwargs: True)
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
