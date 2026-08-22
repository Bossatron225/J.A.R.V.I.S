import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import open_loops as ol


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ol, "BASE_DIR", tmp_path)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.jarvis_logging.recent_actions", lambda **k: [])
    monkeypatch.setattr("memory.memory_manager.load_memory", lambda: {})


# ── commitment detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I'll call the landlord tomorrow",
    "I will sort the insurance",
    "remind me to book the flights",
    "I need to renew the passport",
    "don't forget the dentist",
])
def test_recognises_commitments(text):
    assert ol.looks_like_commitment(text) is True


@pytest.mark.parametrize("text", [
    "the weather is nice",
    "she lives in Dublin",
    "python instead of vscode",
])
def test_ignores_plain_observations(text):
    """A false 'you said you'd do X' is worse than missing one."""
    assert ol.looks_like_commitment(text) is False


def test_ignores_commitments_already_resolved():
    assert ol.looks_like_commitment("I'll call the landlord — done") is False
    assert ol.looks_like_commitment("I need to book flights, cancelled now") is False


def test_ignores_trivially_short_text():
    assert ol.looks_like_commitment("I'll") is False


# ── sources ────────────────────────────────────────────────────────────────

def test_surfaces_pending_self_improvement(tmp_path):
    (tmp_path / "memory" / "dev_agent_approvals.json").write_text(json.dumps({
        "pending": [{"approval_id": "abc123", "created_at": _iso(3), "description": "tidy the logging"}]
    }), encoding="utf-8")

    loops = ol.collect_open_loops(min_age_hours=0)

    assert any(l["kind"] == "pending_self_improvement" and "tidy the logging" in l["summary"] for l in loops)


def test_surfaces_failed_actions(monkeypatch):
    monkeypatch.setattr("core.jarvis_logging.recent_actions", lambda **k: [
        {"ts": _iso(1), "action": "tool_call", "detail": "flight_finder", "result": "could not reach the service"},
        {"ts": _iso(1), "action": "tool_call", "detail": "weather_report", "result": "18C and clear"},
    ])

    loops = ol.collect_open_loops(min_age_hours=0)
    failed = [l for l in loops if l["kind"] == "failed_action"]

    assert len(failed) == 1
    assert "flight_finder" in failed[0]["summary"]


def test_surfaces_memory_commitments(monkeypatch):
    monkeypatch.setattr("memory.memory_manager.load_memory", lambda: {
        "wishes": {"landlord": {"value": "I'll call the landlord about the boiler", "updated": "2026-08-01"}},
        "notes": {"weather": {"value": "it was sunny", "updated": "2026-08-01"}},
    })

    loops = ol.collect_open_loops(min_age_hours=0)
    commitments = [l for l in loops if l["kind"] == "commitment"]

    assert len(commitments) == 1
    assert "landlord" in commitments[0]["summary"]


# ── surfacing policy ───────────────────────────────────────────────────────

def test_recent_loops_are_not_surfaced_immediately(monkeypatch):
    """Give things a chance to resolve on their own before nagging."""
    monkeypatch.setattr("core.jarvis_logging.recent_actions", lambda **k: [
        {"ts": _iso(0), "action": "tool_call", "detail": "x", "result": "failed"},
    ])
    assert ol.collect_open_loops(min_age_hours=6) == []


def test_oldest_loops_come_first(monkeypatch):
    monkeypatch.setattr("core.jarvis_logging.recent_actions", lambda **k: [
        {"ts": _iso(1), "action": "tool_call", "detail": "recent", "result": "failed"},
        {"ts": _iso(9), "action": "tool_call", "detail": "ancient", "result": "failed"},
    ])
    loops = ol.collect_open_loops(min_age_hours=0)
    assert "ancient" in loops[0]["summary"]


def test_empty_state_reports_nothing_outstanding():
    assert "Nothing outstanding" in ol.format_open_loops([])


def test_report_is_readable(tmp_path):
    (tmp_path / "memory" / "dev_agent_approvals.json").write_text(json.dumps({
        "pending": [{"approval_id": "a", "created_at": _iso(2), "description": "do a thing"}]
    }), encoding="utf-8")

    text = ol.open_loops({"min_age_hours": 0})

    assert "awaiting your approval" in text
    assert "do a thing" in text


def test_kind_filter_narrows_results(tmp_path, monkeypatch):
    (tmp_path / "memory" / "dev_agent_approvals.json").write_text(json.dumps({
        "pending": [{"approval_id": "a", "created_at": _iso(2), "description": "proposal"}]
    }), encoding="utf-8")
    monkeypatch.setattr("core.jarvis_logging.recent_actions", lambda **k: [
        {"ts": _iso(2), "action": "tool_call", "detail": "x", "result": "failed"},
    ])

    text = ol.open_loops({"min_age_hours": 0, "kind": "failed_action"})

    assert "did not complete" in text
    assert "proposal" not in text


def test_corrupt_sources_do_not_break_collection(tmp_path):
    (tmp_path / "memory" / "dev_agent_approvals.json").write_text("{not json", encoding="utf-8")
    assert ol.collect_open_loops(min_age_hours=0) == []
