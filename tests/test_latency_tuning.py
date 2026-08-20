import json

from google.genai import types

import main as main_module
from main import JarvisLive


def test_load_turn_detection_config_defaults_when_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", tmp_path / "missing.json")

    cfg = JarvisLive._load_turn_detection_config()

    assert cfg["silence_duration_ms"] == 500
    assert cfg["prefix_padding_ms"] == 150


def test_load_turn_detection_config_reads_and_clamps_overrides(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps({
        "vad_silence_duration_ms": 50,   # below the 100ms floor
        "vad_prefix_padding_ms": 5000,   # above the 1000ms ceiling
    }), encoding="utf-8")
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", config_path)

    cfg = JarvisLive._load_turn_detection_config()

    assert cfg["silence_duration_ms"] == 100
    assert cfg["prefix_padding_ms"] == 1000


def test_responsive_audio_profile_never_drops_speaker_audio() -> None:
    # The whole point of "responsive" vs "aggressive" is the same low-latency
    # sizing without the risk of dropped/cut-off speech under buffer pressure.
    assert main_module.AUDIO_TUNING_PROFILES["responsive"]["speaker_drop_policy"] == "preserve"
    assert (
        main_module.AUDIO_TUNING_PROFILES["responsive"]["mic_chunk_size"]
        == main_module.AUDIO_TUNING_PROFILES["aggressive"]["mic_chunk_size"]
    )


def test_load_audio_tuning_config_accepts_responsive_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps({"audio_latency_profile": "responsive"}), encoding="utf-8")
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", config_path)

    name, cfg = JarvisLive._load_audio_tuning_config()

    assert name == "responsive"
    assert cfg["speaker_drop_policy"] == "preserve"


def test_build_config_sets_low_latency_turn_detection(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps({
        "gemini_api_key": "test-key",
        "vad_silence_duration_ms": 400,
        "vad_prefix_padding_ms": 100,
    }), encoding="utf-8")
    monkeypatch.setattr(main_module, "API_CONFIG_PATH", config_path)
    monkeypatch.setattr(main_module, "load_memory_with_vps_sync", lambda memory, url: memory)
    monkeypatch.setattr(main_module, "load_memory", lambda: {})
    monkeypatch.setattr(main_module, "recall_user_profile", lambda: "")
    monkeypatch.setattr(main_module, "build_personal_memory_context", lambda memory, obsidian: "")
    monkeypatch.setattr(main_module, "_load_system_prompt", lambda: "test prompt")

    live = JarvisLive.__new__(JarvisLive)
    live._use_external_tts = True
    live._current_time_text = lambda: "Monday, January 1, 2026 at 12:00:00 PM (UTC)"

    config = JarvisLive._build_config(live)

    detection = config.realtime_input_config.automatic_activity_detection
    assert detection.silence_duration_ms == 400
    assert detection.prefix_padding_ms == 100
    assert detection.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_HIGH
    assert detection.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_HIGH
