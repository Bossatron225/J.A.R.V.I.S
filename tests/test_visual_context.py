import json
import sys
import types

import numpy as np
import pytest

from actions import visual_context as visual_context_module

cv2 = pytest.importorskip("cv2")


def _blank_frame():
    return np.zeros((4, 4, 3), dtype=np.uint8)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FakeModels:
    def __init__(self, response_text: str, captured: dict):
        self._response_text = response_text
        self._captured = captured

    def generate_content(self, model, contents):
        self._captured["model"] = model
        self._captured["contents"] = contents
        return _FakeResponse(self._response_text)


class _FakeClient:
    def __init__(self, response_text: str, captured: dict):
        self.models = _FakeModels(response_text, captured)


class _FakePart:
    @staticmethod
    def from_bytes(data, mime_type):
        return f"<part mime_type={mime_type} bytes={len(data)}>"


def _install_fake_genai(monkeypatch, response_text: str) -> dict:
    captured: dict = {}
    fake_types_module = types.SimpleNamespace(Part=_FakePart)
    fake_genai_module = types.SimpleNamespace(
        Client=lambda api_key: _FakeClient(response_text, captured),
        types=fake_types_module,
    )
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)
    monkeypatch.setitem(sys.modules, "google", types.SimpleNamespace(genai=fake_genai_module))
    return captured


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_context_module, "_get_gemini_api_key", lambda: "test-key")
    # Isolate the usage log. Without this these tests read the REAL production
    # log, so once live usage crossed the hourly runaway cap the tests started
    # failing for reasons entirely unrelated to the code under test.
    from memory import usage_log
    monkeypatch.setattr(usage_log, "USAGE_PATH", tmp_path / "usage_log.jsonl")


def test_describe_scene_returns_no_comment_without_frame():
    assert visual_context_module.describe_scene(None) == {"should_comment": False}


def test_describe_scene_returns_no_comment_without_api_key(monkeypatch):
    monkeypatch.setattr(visual_context_module, "_get_gemini_api_key", lambda: "")
    result = visual_context_module.describe_scene(_blank_frame())
    assert result == {"should_comment": False}


def test_describe_scene_parses_should_comment_true(monkeypatch):
    _install_fake_genai(monkeypatch, json.dumps({
        "should_comment": True,
        "comment": "You appear to be soldering a circuit board.",
    }))

    result = visual_context_module.describe_scene(_blank_frame())

    assert result["should_comment"] is True
    assert "circuit board" in result["comment"]


def test_describe_scene_parses_should_comment_false(monkeypatch):
    _install_fake_genai(monkeypatch, json.dumps({"should_comment": False, "comment": ""}))

    result = visual_context_module.describe_scene(_blank_frame())

    assert result["should_comment"] is False


def test_describe_scene_strips_markdown_fences(monkeypatch):
    _install_fake_genai(monkeypatch, "```json\n" + json.dumps({"should_comment": False, "comment": ""}) + "\n```")

    result = visual_context_module.describe_scene(_blank_frame())

    assert result["should_comment"] is False


def test_describe_scene_handles_malformed_json_gracefully(monkeypatch):
    _install_fake_genai(monkeypatch, "not valid json at all")

    result = visual_context_module.describe_scene(_blank_frame())

    assert result == {"should_comment": False}


def test_describe_scene_includes_last_comment_in_prompt_to_avoid_repetition(monkeypatch):
    captured = _install_fake_genai(monkeypatch, json.dumps({"should_comment": False, "comment": ""}))

    visual_context_module.describe_scene(_blank_frame(), last_comment="You're working on a circuit board.")

    prompt_text = captured["contents"][1]
    assert "circuit board" in prompt_text
    assert "do not repeat" in prompt_text.lower()
