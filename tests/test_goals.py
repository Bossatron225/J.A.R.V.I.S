import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import goal_runner, goals


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "GOALS_PATH", tmp_path / "goals.json")


def _now():
    return datetime.now(timezone.utc)


# ── store + safety rails ───────────────────────────────────────────────────

def test_add_and_list_a_goal():
    goals.add_goal("find a flat in Dublin 8", criteria="under 1800")
    listed = goals.list_goals()
    assert len(listed) == 1
    assert listed[0]["objective"] == "find a flat in Dublin 8"


def test_goals_are_read_only_by_default():
    """An unattended loop must not be able to send, spend or book unless the
    user explicitly opted in."""
    goal = goals.add_goal("watch flight prices")
    assert goal["allow_actions"] is False


def test_interval_is_clamped_to_a_floor():
    """Guards against a goal configured into a tight, expensive loop."""
    goal = goals.add_goal("x", interval_hours=0.01)
    assert goal["interval_hours"] == goals.MIN_INTERVAL_HOURS


def test_empty_objective_is_rejected():
    with pytest.raises(ValueError):
        goals.add_goal("   ")


def test_goal_count_is_capped(monkeypatch):
    monkeypatch.setattr(goals, "MAX_GOALS", 2)
    goals.add_goal("a")
    goals.add_goal("b")
    with pytest.raises(ValueError, match="too many"):
        goals.add_goal("c")


# ── scheduling ─────────────────────────────────────────────────────────────

def test_a_new_goal_is_due_immediately():
    goal = goals.add_goal("x")
    assert goals.is_due(goal) is True


def test_goal_is_not_due_again_before_its_interval():
    goal = goals.add_goal("x", interval_hours=6)
    goals.record_check(goal["id"])
    assert goals.is_due(goals.get_goal(goal["id"])) is False


def test_goal_becomes_due_after_its_interval():
    goal = goals.add_goal("x", interval_hours=6)
    goals.record_check(goal["id"])
    later = _now() + timedelta(hours=7)
    assert goals.is_due(goals.get_goal(goal["id"]), now=later) is True


def test_paused_and_closed_goals_are_never_due():
    goal = goals.add_goal("x")
    goals.set_status(goal["id"], "paused")
    assert goals.is_due(goals.get_goal(goal["id"])) is False
    goals.set_status(goal["id"], "closed")
    assert goals.is_due(goals.get_goal(goal["id"])) is False


def test_daily_budget_stops_further_checks():
    """The cost guard: a goal cannot keep spending once its budget is used."""
    goal = goals.add_goal("x", interval_hours=1, daily_budget=2)
    for _ in range(2):
        goals.record_check(goal["id"])
    later = _now() + timedelta(hours=5)
    assert goals.is_due(goals.get_goal(goal["id"]), now=later) is False


def test_budget_resets_on_a_new_day():
    goal = goals.add_goal("x", interval_hours=1, daily_budget=1)
    goals.record_check(goal["id"])
    tomorrow = _now() + timedelta(days=1, hours=2)
    assert goals.is_due(goals.get_goal(goal["id"]), now=tomorrow) is True


def test_closed_goals_are_hidden_unless_requested():
    goal = goals.add_goal("x")
    goals.set_status(goal["id"], "closed")
    assert goals.list_goals() == []
    assert len(goals.list_goals(include_closed=True)) == 1


# ── never repeat yourself ──────────────────────────────────────────────────

def test_surfaced_items_are_not_shown_again():
    goal = goals.add_goal("x")
    goals.record_surfaced(goal["id"], ["Flat on Clanbrassil St, 1750/mo"])
    updated = goals.get_goal(goal["id"])
    assert goals.already_seen(updated, "Flat on Clanbrassil St, 1750/mo") is True


def test_dismissed_items_are_not_shown_again():
    """Rejections are how the goal sharpens instead of repeating."""
    goal = goals.add_goal("x")
    goals.record_rejection(goal["id"], "Studio with no windows")
    updated = goals.get_goal(goal["id"])
    assert goals.already_seen(updated, "Studio with no windows") is True


def test_item_identity_ignores_whitespace_and_case():
    assert goals.item_key("A Flat  In Dublin") == goals.item_key("a flat in dublin")


def test_filter_unseen_removes_known_items():
    goal = goals.add_goal("x")
    goals.record_surfaced(goal["id"], ["seen before"])
    updated = goals.get_goal(goal["id"])
    items = [{"summary": "seen before"}, {"summary": "brand new"}]
    fresh = goal_runner.filter_unseen(updated, items)
    assert [i["summary"] for i in fresh] == ["brand new"]


# ── tool permissions ───────────────────────────────────────────────────────

ALL_TOOLS = ["web_search", "send_message", "dev_agent", "computer_control",
             "weather_report", "shutdown_jarvis", "flight_finder", "multi_step_task"]


def test_read_only_goal_cannot_use_acting_tools():
    allowed = goal_runner.allowed_tools(ALL_TOOLS, allow_actions=False)
    assert "web_search" in allowed and "flight_finder" in allowed
    for blocked in ("send_message", "computer_control", "dev_agent", "shutdown_jarvis"):
        assert blocked not in allowed


def test_acting_goal_still_cannot_modify_jarvis_or_shut_him_down():
    """Even an opted-in goal must not rewrite the codebase or kill the process
    unattended."""
    allowed = goal_runner.allowed_tools(ALL_TOOLS, allow_actions=True)
    assert "send_message" in allowed
    for blocked in ("dev_agent", "shutdown_jarvis", "multi_step_task"):
        assert blocked not in allowed


# ── finding evaluation ─────────────────────────────────────────────────────

def _gen(payload):
    return lambda prompt: payload if isinstance(payload, str) else json.dumps(payload)


def test_qualifying_finding_is_returned():
    items = goal_runner.evaluate_findings(
        "find a flat", "under 1800", "Flat listed at 1750",
        _gen({"items": [{"summary": "Flat at 1750", "why": "under budget", "confidence": 0.9}]}),
    )
    assert len(items) == 1
    assert items[0]["summary"] == "Flat at 1750"


def test_low_confidence_findings_are_filtered_out():
    """Better silent than interrupting with a near-miss."""
    items = goal_runner.evaluate_findings(
        "find a flat", "under 1800", "Maybe something",
        _gen({"items": [{"summary": "possible flat", "confidence": 0.4}]}),
    )
    assert items == []


def test_empty_result_is_a_normal_answer():
    assert goal_runner.evaluate_findings("x", "y", "nothing relevant", _gen({"items": []})) == []


def test_no_output_means_no_findings():
    assert goal_runner.evaluate_findings("x", "y", "   ", _gen({"items": []})) == []


def test_malformed_model_output_yields_nothing_rather_than_raising():
    assert goal_runner.evaluate_findings("x", "y", "data", _gen("not json")) == []


def test_previous_rejections_are_passed_to_the_filter():
    captured = {}

    def _capture(prompt):
        captured["prompt"] = prompt
        return json.dumps({"items": []})

    goal_runner.evaluate_findings("x", "y", "data", _capture,
                                  already_rejected_samples=["windowless studio"])
    assert "windowless studio" in captured["prompt"]


def test_findings_message_tells_the_user_how_to_dismiss():
    goal = {"id": "abc123", "objective": "find a flat"}
    text = goal_runner.format_findings(goal, [{"summary": "Flat at 1750", "why": "under budget"}])
    assert "Flat at 1750" in text
    assert "dismiss" in text.lower()
    assert "abc123" in text


def test_no_findings_produces_no_message():
    assert goal_runner.format_findings({"id": "x", "objective": "y"}, []) == ""


# ── tool surface ───────────────────────────────────────────────────────────

def test_tool_adds_and_reports_a_goal():
    out = goals.goals_tool({"action": "add", "objective": "watch flight prices"})
    assert "set" in out.lower()
    assert "only look things up" in out


def test_tool_pause_resume_close_roundtrip():
    goal = goals.add_goal("x")
    assert "paused" in goals.goals_tool({"action": "pause", "goal_id": goal["id"]})
    assert "active" in goals.goals_tool({"action": "resume", "goal_id": goal["id"]})
    assert "closed" in goals.goals_tool({"action": "close", "goal_id": goal["id"]})


def test_tool_reports_unknown_goal_id():
    assert "No goal with id" in goals.goals_tool({"action": "pause", "goal_id": "nope"})


def test_tool_lists_nothing_when_empty():
    assert "no standing goals" in goals.goals_tool({"action": "list"}).lower()


def test_tool_records_a_dismissal():
    goal = goals.add_goal("x")
    out = goals.goals_tool({"action": "reject", "goal_id": goal["id"], "text": "bad listing"})
    assert "won't raise that one again" in out
    assert goals.already_seen(goals.get_goal(goal["id"]), "bad listing") is True


def test_corrupt_store_does_not_crash():
    goals.GOALS_PATH.write_text("{not json", encoding="utf-8")
    assert goals.list_goals() == []
