import pathlib
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
