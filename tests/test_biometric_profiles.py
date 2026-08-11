from actions import file_controller as file_controller_module
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
