import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import usage_log


@pytest.fixture(autouse=True)
def _isolate_usage_log(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_log, "USAGE_PATH", tmp_path / "usage_log.jsonl")


def test_summary_is_empty_before_any_usage():
    summary = usage_log.summarize()
    assert summary["total_calls"] == 0
    assert "No API usage" in usage_log.format_summary(summary)


def test_records_and_aggregates_calls_by_feature():
    usage_log.record_usage("visual_context", "gemini_image", 1)
    usage_log.record_usage("visual_context", "gemini_image", 1)
    usage_log.record_usage("dev_agent", "gemini_text", 1)

    summary = usage_log.summarize()

    assert summary["total_calls"] == 3
    assert summary["by_feature"]["visual_context"]["calls"] == 2
    assert summary["by_feature"]["dev_agent"]["calls"] == 1


def test_character_billed_cost_scales_with_units():
    usage_log.record_usage("elevenlabs_tts", "elevenlabs_tts", 1000)
    summary = usage_log.summarize()
    expected = usage_log.RATES["elevenlabs_tts"]["per_1k_chars"]
    assert summary["by_feature"]["elevenlabs_tts"]["est_cost_usd"] == pytest.approx(expected, rel=1e-6)


def test_unknown_kind_costs_nothing_rather_than_raising():
    usage_log.record_usage("mystery", "not_a_real_rate", 500)
    assert usage_log.summarize()["by_feature"]["mystery"]["est_cost_usd"] == 0.0


def test_calls_since_counts_only_the_named_feature():
    for _ in range(3):
        usage_log.record_usage("visual_context", "gemini_image", 1)
    usage_log.record_usage("dev_agent", "gemini_text", 1)

    assert usage_log.calls_since("visual_context", 3600) == 3
    assert usage_log.calls_since("dev_agent", 3600) == 1


def test_calls_since_is_the_runaway_guard_signal():
    """visual_context caps itself on this; steady state is ~60/hour, so the
    count must reflect real call volume."""
    for _ in range(10):
        usage_log.record_usage("visual_context", "gemini_image", 1)
    assert usage_log.calls_since("visual_context", 3600) == 10


def test_summary_for_a_day_with_no_usage_is_empty():
    usage_log.record_usage("visual_context", "gemini_image", 1)
    assert usage_log.summarize("1999-01-01")["total_calls"] == 0


def test_record_usage_never_raises_even_on_unwritable_path(monkeypatch, tmp_path):
    """Usage accounting must never be able to break the feature it measures."""
    monkeypatch.setattr(usage_log, "USAGE_PATH", tmp_path / "no" / "such" / "dir" / "x.jsonl")
    monkeypatch.setattr(usage_log.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    usage_log.record_usage("visual_context", "gemini_image", 1)  # must not raise


def test_usage_report_tool_renders_readable_text():
    usage_log.record_usage("visual_context", "gemini_image", 1)
    text = usage_log.usage_report({})
    assert "visual_context" in text
    assert "estimate" in text.lower()


def test_trim_caps_the_log(monkeypatch):
    monkeypatch.setattr(usage_log, "MAX_ENTRIES", 5)
    for _ in range(12):
        usage_log.record_usage("visual_context", "gemini_image", 1)

    usage_log.trim_if_needed()

    assert len(usage_log._read_entries()) == 5
