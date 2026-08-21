import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import jarvis_logging as jlog


@pytest.fixture(autouse=True)
def _isolate_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(jlog, "AUDIT_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.delenv("JARVIS_LOG_LEVEL", raising=False)


# ── truncation: the embedding-flood fix ────────────────────────────────────

def test_long_float_vector_is_summarised_not_dumped():
    """A relayed tool result once dumped a full embedding into the log —
    ~300KB of floats in a 4.4MB file, burying every real event."""
    rendered = jlog.truncate([0.001 * i for i in range(768)])
    assert "768 numbers" in rendered
    assert len(rendered) < 100


def test_long_string_is_truncated_with_a_size_marker():
    rendered = jlog.truncate("x" * 5000)
    assert len(rendered) < 600
    assert "+" in rendered and "chars" in rendered


def test_dict_values_are_individually_truncated():
    rendered = jlog.truncate({"vec": [0.5] * 500, "note": "fine"})
    assert "500 numbers" in rendered
    assert "fine" in rendered


def test_short_values_pass_through_unchanged():
    assert jlog.truncate("a normal message") == "a normal message"


def test_truncate_never_raises_on_unrenderable_objects():
    class Explodes:
        def __repr__(self):
            raise ValueError("no")

    assert isinstance(jlog.truncate(Explodes()), str)


# ── leveled output ─────────────────────────────────────────────────────────

def test_log_line_has_timestamp_level_and_component(capsys):
    jlog.info("Camera", "streaming started", frames=3)
    out = capsys.readouterr().out
    assert "INFO" in out
    assert "[Camera]" in out
    assert "frames=3" in out
    assert "T" in out and "Z" in out  # ISO timestamp


def test_level_filtering_suppresses_debug_by_default(capsys):
    jlog.debug("Camera", "noisy detail")
    assert capsys.readouterr().out == ""


def test_level_filtering_can_be_raised_by_env(capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_LOG_LEVEL", "ERROR")
    jlog.info("Camera", "should be hidden")
    jlog.error("Camera", "should appear")
    out = capsys.readouterr().out
    assert "should be hidden" not in out
    assert "should appear" in out


# ── audit trail ────────────────────────────────────────────────────────────

def test_records_and_reads_back_actions():
    jlog.record_action("tool_call", "send_message", result="ok")
    jlog.record_action("unknown_visitor_seen", "visitor abc123")

    actions = jlog.recent_actions()

    assert len(actions) == 2
    assert actions[-1]["action"] == "unknown_visitor_seen"


def test_actions_can_be_filtered_by_type():
    jlog.record_action("tool_call", "a")
    jlog.record_action("unknown_visitor_seen", "b")
    jlog.record_action("tool_call", "c")

    assert len(jlog.recent_actions(action="tool_call")) == 2


def test_audit_entries_truncate_large_payloads():
    jlog.record_action("tool_call", "embed", result=[0.1] * 900)
    assert "900 numbers" in jlog.recent_actions()[-1]["result"]


def test_record_action_never_raises_on_unwritable_path(monkeypatch, tmp_path):
    """Auditing must never be able to break the action it is recording."""
    monkeypatch.setattr(jlog, "AUDIT_PATH", tmp_path / "no" / "way" / "a.jsonl")
    monkeypatch.setattr(jlog.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    jlog.record_action("tool_call", "x")  # must not raise


def test_activity_report_renders_readable_text():
    jlog.record_action("tool_call", "send_message")
    text = jlog.activity_report({})
    assert "send_message" in text


def test_activity_report_handles_empty_trail():
    assert "haven't taken any" in jlog.activity_report({})


def test_trim_caps_the_audit_trail(monkeypatch):
    monkeypatch.setattr(jlog, "MAX_AUDIT_ENTRIES", 5)
    for i in range(12):
        jlog.record_action("tool_call", f"call {i}")

    jlog.trim_audit_if_needed()

    remaining = jlog.recent_actions(limit=0)
    assert len(remaining) == 5
    assert remaining[-1]["detail"] == "call 11"  # newest kept


def test_audit_file_is_valid_jsonl():
    jlog.record_action("tool_call", "one")
    jlog.record_action("tool_call", "two")
    for line in jlog.AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        json.loads(line)
