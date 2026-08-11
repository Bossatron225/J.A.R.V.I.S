import pathlib
import struct
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import tts


def test_play_np_mixes_stereo_to_mono(monkeypatch):
    captured = {}

    def fake_play(data, sample_rate):
        captured["data"] = data
        captured["sample_rate"] = sample_rate

    monkeypatch.setattr(tts.sd, "play", fake_play)
    monkeypatch.setattr(tts.sd, "wait", lambda: None)

    stereo = np.array([[0.1, -0.1], [0.2, -0.2]], dtype=np.float32)
    tts._play_np(stereo, 22050)

    assert captured["sample_rate"] == 22050
    data = np.asarray(captured["data"], dtype=np.float32)
    assert data.ndim == 1
    np.testing.assert_allclose(data, np.array([0.0, 0.0], dtype=np.float32))


def test_play_audio_bytes_flattens_numpy_samples_before_sounddevice(monkeypatch):
    class FakeDecoded:
        samples = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
        sample_rate = 22050

    class FakeMiniaudioModule:
        class SampleFormat:
            FLOAT32 = "float32"

        def decode(self, audio_bytes, output_format, nchannels):
            assert audio_bytes == b"abc"
            assert output_format == self.SampleFormat.FLOAT32
            assert nchannels == 1
            return FakeDecoded()

    captured = {}

    def fake_play(data, sample_rate):
        captured["data"] = data
        captured["sample_rate"] = sample_rate

    monkeypatch.setitem(sys.modules, "miniaudio", FakeMiniaudioModule())
    monkeypatch.setattr(tts.sd, "play", fake_play)
    monkeypatch.setattr(tts.sd, "wait", lambda: None)

    tts._play_audio_bytes(b"abc")

    assert captured["sample_rate"] == 22050
    assert isinstance(captured["data"], list)
    np.testing.assert_allclose(captured["data"], [0.15, 0.35])


def test_elevenlabs_falls_back_to_default_voice(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, content=b"audio"):
            self.status_code = status_code
            self.content = content
            self.text = ""
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise FakeHTTPError(self)

    class FakeHTTPError(Exception):
        def __init__(self, response):
            self.response = response
            super().__init__(str(response.status_code))

    class FakeRequestsModule:
        def __init__(self):
            self.calls = []

        def post(self, url, json, headers, timeout):
            self.calls.append((url, json, headers, timeout))
            if len(self.calls) == 1:
                raise FakeHTTPError(FakeResponse(400))
            return FakeResponse(200, b"audio")

    captured = {}

    monkeypatch.setitem(sys.modules, "requests", FakeRequestsModule())
    monkeypatch.setattr(tts, "_play_audio_bytes", lambda payload, sample_rate=None: captured.setdefault("payload", payload))

    engine = tts.ElevenLabsTTSEngine(api_key="abc", voice_id="invalid-voice")
    engine.speak("hello")

    assert len(sys.modules["requests"].calls) == 2
    assert sys.modules["requests"].calls[0][0].endswith("invalid-voice")
    assert sys.modules["requests"].calls[1][0].endswith("pNInz6obpgDQGcFmaJgB")
    assert captured["payload"] == b"audio"


def test_play_audio_bytes_handles_raw_pcm_bytes(monkeypatch):
    pcm_bytes = struct.pack("<4h", 0, 16384, -16384, 0)
    captured = {}

    def fake_play(data, sample_rate):
        captured["data"] = data
        captured["sample_rate"] = sample_rate

    monkeypatch.setattr(tts.sd, "play", fake_play)
    monkeypatch.setattr(tts.sd, "wait", lambda: None)

    tts._play_audio_bytes(pcm_bytes, sample_rate=16000)

    assert captured["sample_rate"] == 16000
    np.testing.assert_allclose(captured["data"], [0.0, 0.5, -0.5, 0.0], atol=1e-6)


def test_play_audio_bytes_ignores_sounddevice_errors(monkeypatch):
    monkeypatch.setattr(tts.sd, "play", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("AUHAL")))
    monkeypatch.setattr(tts.sd, "wait", lambda: (_ for _ in ()).throw(RuntimeError("AUHAL")))

    tts._play_audio_bytes(b"abc", sample_rate=16000)
