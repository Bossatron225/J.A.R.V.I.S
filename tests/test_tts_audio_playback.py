import numpy as np

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
