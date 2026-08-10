from core import tts


def test_reset_audio_output_calls_sounddevice_stop(monkeypatch):
    calls = []

    def fake_stop():
        calls.append("stop")

    monkeypatch.setattr(tts.sd, "stop", fake_stop)

    tts.reset_audio_output()

    assert calls == ["stop"]
