import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import attention as at


def _iso(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _silence_all_sources(monkeypatch):
    """Default every source to empty; each test enables just the one it needs."""
    monkeypatch.setattr(at, "SOURCES", [])


# ── ranking ────────────────────────────────────────────────────────────────

def test_urgent_items_come_first(monkeypatch):
    monkeypatch.setattr(at, "SOURCES", [
        lambda: [{"source": "a", "score": at.BACKGROUND, "text": "low"}],
        lambda: [{"source": "b", "score": at.URGENT, "text": "high"}],
    ])
    items = at.collect_attention()
    assert items[0]["text"] == "high"


def test_urgent_only_filters_lesser_items(monkeypatch):
    monkeypatch.setattr(at, "SOURCES", [
        lambda: [{"source": "a", "score": at.NOTABLE, "text": "meh"}],
        lambda: [{"source": "b", "score": at.URGENT, "text": "important"}],
    ])
    items = at.collect_attention(min_score=at.URGENT)
    assert len(items) == 1 and items[0]["text"] == "important"


def test_empty_state_says_nothing_needs_you():
    assert "Nothing needs you" in at.format_attention([])


def test_header_counts_only_urgent_items():
    items = [
        {"source": "a", "score": at.URGENT, "text": "x"},
        {"source": "b", "score": at.BACKGROUND, "text": "y"},
    ]
    assert "1 thing(s) need you" in at.format_attention(items)


def test_header_when_nothing_is_urgent():
    items = [{"source": "a", "score": at.NOTABLE, "text": "y"}]
    assert "Nothing urgent" in at.format_attention(items)


# ── fault isolation ────────────────────────────────────────────────────────

def test_one_broken_source_does_not_break_the_briefing(monkeypatch):
    """A single unavailable integration must degrade only itself."""
    def _explodes():
        raise RuntimeError("mail server down")

    monkeypatch.setattr(at, "SOURCES", [
        _explodes,
        lambda: [{"source": "ok", "score": at.URGENT, "text": "still works"}],
    ])
    items = at.collect_attention()
    assert len(items) == 1 and items[0]["text"] == "still works"


# ── unconfigured integrations must not become fake urgent items ────────────

@pytest.mark.parametrize("noise", [
    "Google Calendar is not ready yet. Create a Google Cloud OAuth credential",
    "Unknown mail action. Use read_latest, read_unread",
    "iMessage integration is only available on macOS.",
    "Could not reach the server",
])
def test_setup_errors_are_not_reported_as_attention_items(noise):
    """A setup gap surfaced as 'needs you' is exactly the noise that trains a
    user to ignore the briefing."""
    assert at._is_real_content(noise) is False


def test_genuine_content_passes_the_filter():
    assert at._is_real_content("Unread mail: Subject: Your order shipped") is True


def test_empty_source_output_is_not_content():
    assert at._is_real_content("") is False
    assert at._is_real_content("   ") is False


# ── visitor urgency ────────────────────────────────────────────────────────

def test_repeat_visitor_is_urgent(monkeypatch):
    monkeypatch.setattr("actions.visitor_log.list_recent_sightings",
                        lambda limit=20: [{"ts": _iso(1), "sighting_count_at_time": 4}])
    items = at._visitor_items()
    assert items[0]["score"] == at.URGENT
    assert "4 times" in items[0]["text"]


def test_single_visitor_is_notable_not_urgent(monkeypatch):
    monkeypatch.setattr("actions.visitor_log.list_recent_sightings",
                        lambda limit=20: [{"ts": _iso(1), "sighting_count_at_time": 1}])
    assert at._visitor_items()[0]["score"] == at.NOTABLE


def test_old_sightings_are_ignored(monkeypatch):
    monkeypatch.setattr("actions.visitor_log.list_recent_sightings",
                        lambda limit=20: [{"ts": _iso(48), "sighting_count_at_time": 5}])
    assert at._visitor_items() == []


# ── goals ──────────────────────────────────────────────────────────────────

def test_goal_with_findings_is_surfaced(monkeypatch):
    monkeypatch.setattr("actions.goals.list_goals",
                        lambda **k: [{"status": "active", "objective": "find a flat", "findings": 2}])
    items = at._goal_items()
    assert items and "2 finding" in items[0]["text"]


def test_goal_without_findings_is_not_surfaced(monkeypatch):
    monkeypatch.setattr("actions.goals.list_goals",
                        lambda **k: [{"status": "active", "objective": "x", "findings": 0}])
    assert at._goal_items() == []


def test_paused_goal_is_not_surfaced(monkeypatch):
    monkeypatch.setattr("actions.goals.list_goals",
                        lambda **k: [{"status": "paused", "objective": "x", "findings": 5}])
    assert at._goal_items() == []


# ── tool surface ───────────────────────────────────────────────────────────

def test_briefing_tool_returns_readable_text(monkeypatch):
    monkeypatch.setattr(at, "SOURCES", [
        lambda: [{"source": "a", "score": at.URGENT, "text": "the boiler is leaking"}],
    ])
    text = at.attention_briefing({})
    assert "boiler" in text and "needs you" in text
