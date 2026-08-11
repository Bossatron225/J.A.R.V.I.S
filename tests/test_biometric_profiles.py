import asyncio
from types import SimpleNamespace

from actions import file_controller as file_controller_module
import main as main_module
import ui as ui_module
from ui import ManageProfilesOverlay


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
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"audio-sample", 0.001))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", True))
    monkeypatch.setattr(file_controller_module, "_verify_reference_face_match", lambda: True)

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
    monkeypatch.setattr(file_controller_module, "_record_voice_sample", lambda *args, **kwargs: (b"audio-sample", 0.002))
    monkeypatch.setattr(file_controller_module, "_capture_live_visual_frame", lambda *args, **kwargs: (b"frame", False))
    monkeypatch.setattr(file_controller_module, "_verify_reference_face_match", lambda: True)

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
