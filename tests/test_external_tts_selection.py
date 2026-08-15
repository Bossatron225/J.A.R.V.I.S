import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main


def test_external_tts_enabled_includes_elevenlabs():
    assert main.JarvisLive._external_tts_enabled({"tts_engine": "elevenlabs"}) is True


def test_external_tts_label_uses_elevenlabs_name():
    assert main.JarvisLive._external_tts_label({"tts_engine": "elevenlabs"}) == "ElevenLabs"


def test_current_time_uses_configured_live_timezone(monkeypatch):
    monkeypatch.setenv("JARVIS_TIMEZONE", "Europe/Dublin")

    text = main.JarvisLive._current_time_text()
    expected_hour = datetime.now(ZoneInfo("Europe/Dublin")).strftime("%I:%M")

    assert expected_hour in text
    assert "Europe/Dublin" in text
