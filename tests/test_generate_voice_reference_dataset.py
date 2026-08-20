import wave
import io

import scripts.generate_voice_reference_dataset as gen


def test_wrap_pcm_as_wav_produces_valid_wav_header():
    pcm = (b"\x00\x01" * 100)  # 100 fake 16-bit samples
    wav_bytes = gen.wrap_pcm_as_wav(pcm, sample_rate=24000)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.readframes(wav_file.getnframes()) == pcm


def test_load_corpus_strips_blank_lines(tmp_path, monkeypatch):
    corpus_file = tmp_path / "corpus.txt"
    corpus_file.write_text("Hello there.\n\n  \nGeneral Kenobi.\n", encoding="utf-8")

    lines = gen.load_corpus(corpus_file)

    assert lines == ["Hello there.", "General Kenobi."]


def test_load_corpus_missing_file_returns_empty_list(tmp_path):
    assert gen.load_corpus(tmp_path / "does_not_exist.txt") == []


def test_generate_dataset_without_confirmation_makes_no_network_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "CORPUS_PATH", gen.CORPUS_PATH)  # real bundled corpus is fine

    calls = []
    monkeypatch.setattr(gen, "_synthesize", lambda *a, **k: calls.append(1))

    gen.generate_dataset(confirmed=False)

    assert calls == []
