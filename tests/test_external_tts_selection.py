import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main


def test_external_tts_enabled_includes_elevenlabs():
    assert main.JarvisLive._external_tts_enabled({"tts_engine": "elevenlabs"}) is True


def test_external_tts_label_uses_elevenlabs_name():
    assert main.JarvisLive._external_tts_label({"tts_engine": "elevenlabs"}) == "ElevenLabs"
