import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import dev_agent as da


# ── _apply_edits: the safety core ──────────────────────────────────────────

def test_applies_a_single_unique_edit():
    updated, applied = da._apply_edits("a\nTARGET\nb\n", [{"old": "TARGET", "new": "FIXED"}])
    assert "FIXED" in updated and "TARGET" not in updated
    assert len(applied) == 1


def test_applies_multiple_edits_in_order():
    updated, applied = da._apply_edits(
        "one\ntwo\nthree\n",
        [{"old": "one", "new": "1"}, {"old": "three", "new": "3"}],
    )
    assert updated == "1\ntwo\n3\n"
    assert len(applied) == 2


def test_rejects_snippet_that_does_not_exist():
    """A hallucinated target must fail loudly, never be guessed at."""
    with pytest.raises(da.PatchError, match="not found"):
        da._apply_edits("real content\n", [{"old": "imaginary", "new": "x"}])


def test_rejects_ambiguous_snippet():
    """Two matches means the edit could land in the wrong place."""
    with pytest.raises(da.PatchError, match="ambiguous"):
        da._apply_edits("dup\ndup\n", [{"old": "dup", "new": "x"}])


def test_rejects_empty_old_snippet():
    with pytest.raises(da.PatchError, match="empty"):
        da._apply_edits("content\n", [{"old": "", "new": "x"}])


def test_rejects_no_op_edit():
    with pytest.raises(da.PatchError, match="no change"):
        da._apply_edits("same\n", [{"old": "same", "new": "same"}])


def test_indentation_is_preserved_exactly():
    src = "def f():\n    return 1\n"
    updated, _ = da._apply_edits(src, [{"old": "    return 1", "new": "    return 2"}])
    assert updated == "def f():\n    return 2\n"


# ── diff rendering ─────────────────────────────────────────────────────────

def test_diff_reports_added_and_removed_counts():
    text = da.summarize_diff("a\nb\n", "a\nc\n", "x.py")
    assert "+1" in text and "-1" in text
    assert "x.py" in text


def test_diff_is_truncated_for_huge_changes():
    before = "\n".join(f"line {i}" for i in range(2000))
    after = "\n".join(f"changed {i}" for i in range(2000))
    text = da.summarize_diff(before, after, "big.py")
    assert "more diff lines" in text


# ── whole-file vs surgical routing ─────────────────────────────────────────

class _FakeModel:
    def __init__(self, payload: str):
        self._payload = payload
        self.prompts: list[str] = []

    def generate_content(self, prompt):
        self.prompts.append(prompt)
        class _R:
            text = self._payload
        return _R()


def _install(monkeypatch, payload: str) -> _FakeModel:
    model = _FakeModel(payload)
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    return model


def test_large_file_uses_surgical_edits_not_a_rewrite(tmp_path, monkeypatch):
    """The core bug: main.py is ~6,000 lines, and asking a model to reproduce
    it in full truncates and silently deletes code."""
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line_{i} = {i}" for i in range(1000)), encoding="utf-8")
    monkeypatch.setattr(da, "BASE_DIR", tmp_path)

    model = _install(monkeypatch, json.dumps(
        {"edits": [{"intent": "bump", "old": "line_500 = 500", "new": "line_500 = 999"}]}
    ))

    result = da._generate_replacement_for_file(big, "bump line 500", "python")

    assert "line_500 = 999" in result
    assert "line_0 = 0" in result and "line_999 = 999" in result  # nothing lost
    assert "surgical" in model.prompts[0].lower() or "snippet" in model.prompts[0].lower()


def test_small_file_still_uses_whole_file_rewrite(tmp_path, monkeypatch):
    small = tmp_path / "small.py"
    small.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(da, "BASE_DIR", tmp_path)

    model = _install(monkeypatch, "x = 2\n")

    result = da._generate_replacement_for_file(small, "change x", "python")

    assert result == "x = 2"
    assert "complete replacement file contents" in model.prompts[0]


def test_large_file_edit_failure_raises_rather_than_corrupting(tmp_path, monkeypatch):
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line_{i} = {i}" for i in range(1000)), encoding="utf-8")
    monkeypatch.setattr(da, "BASE_DIR", tmp_path)
    _install(monkeypatch, json.dumps({"edits": [{"old": "not_present_anywhere", "new": "x"}]}))

    with pytest.raises(da.PatchError):
        da._generate_replacement_for_file(big, "do something", "python")


def test_malformed_edit_json_raises_patch_error(tmp_path, monkeypatch):
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line_{i} = {i}" for i in range(1000)), encoding="utf-8")
    monkeypatch.setattr(da, "BASE_DIR", tmp_path)
    _install(monkeypatch, "this is not json")

    with pytest.raises(da.PatchError, match="valid edit JSON"):
        da._generate_replacement_for_file(big, "do something", "python")
