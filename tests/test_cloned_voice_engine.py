import pytest

from core.tts import ClonedVoiceEngine


def test_cloned_voice_engine_raises_when_model_file_missing(tmp_path):
    missing_model = tmp_path / "jarvis.pth"

    with pytest.raises(ValueError, match="RVC model not found"):
        ClonedVoiceEngine(model_path=str(missing_model))


def test_cloned_voice_engine_raises_when_rvc_python_unavailable(tmp_path, monkeypatch):
    model_path = tmp_path / "jarvis.pth"
    model_path.write_bytes(b"fake model weights")

    import builtins
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "rvc_python.infer":
            raise ModuleNotFoundError("No module named 'rvc_python'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(RuntimeError, match="rvc-python is not installed"):
        ClonedVoiceEngine(model_path=str(model_path))
