from actions import file_controller as file_controller_module
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


def test_manage_profiles_overlay_supports_capture_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ui.PROFILES_FILE", tmp_path / "authorized_profiles.json")
    monkeypatch.setattr("ui.CONFIG_DIR", tmp_path)

    parent = ui_module.QWidget()
    overlay = ManageProfilesOverlay(parent=parent)
    overlay._set_capture_state("recording")
    assert "SPEAK NOW" in overlay._capture_state_text

    overlay._show_capture_confirmation("Baseline ready")
    assert overlay._confirm_btn.isVisible() is True
    assert overlay._confirm_btn.text() == "CONFIRM BASELINE"
