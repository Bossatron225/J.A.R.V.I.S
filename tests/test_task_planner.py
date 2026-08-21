import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import task_planner as tp

TOOLS = ["web_search", "google_calendar", "send_message", "weather_report"]


def _gen(payload):
    return lambda prompt: payload if isinstance(payload, str) else json.dumps(payload)


# ── verification: the honesty core ─────────────────────────────────────────

def test_plain_success_output_is_accepted():
    ok, _ = tp.verify_step_output("Flight booked for 09:40 Tuesday.")
    assert ok is True


def test_tool_that_reports_failure_in_a_string_is_not_trusted():
    """Several tools in this codebase return a STRING describing a failure.
    Treating any returned string as success is how 'it said it worked' hides a
    broken step."""
    ok, detail = tp.verify_step_output("screen_process is unavailable on the headless VPS.")
    assert ok is False
    assert "unavailable" in detail


def test_mac_offline_result_is_treated_as_failure():
    ok, _ = tp.verify_step_output({"status": "mac_offline", "message": "The Mac worker did not respond."})
    assert ok is False


def test_empty_and_none_output_fail():
    assert tp.verify_step_output(None)[0] is False
    assert tp.verify_step_output("   ")[0] is False


@pytest.mark.parametrize("text", [
    "Error: could not reach the service",
    "No results found",
    "Request timed out",
    "Access denied",
])
def test_common_failure_phrasings_are_caught(text):
    assert tp.verify_step_output(text)[0] is False


# ── planning ───────────────────────────────────────────────────────────────

def test_builds_ordered_steps_with_dependencies():
    steps = tp.build_plan("check weather then text John", TOOLS, _gen({"steps": [
        {"tool": "weather_report", "arguments": {}, "intent": "get weather", "depends_on": []},
        {"tool": "send_message", "arguments": {"to": "John"}, "intent": "text John", "depends_on": [0]},
    ]}))
    assert len(steps) == 2
    assert steps[1]["depends_on"] == [0]


def test_rejects_a_plan_using_an_unknown_tool():
    """A hallucinated tool name must fail loudly rather than be dispatched."""
    with pytest.raises(tp.PlanError, match="unknown tool"):
        tp.build_plan("do it", TOOLS, _gen({"steps": [
            {"tool": "hack_the_mainframe", "arguments": {}, "intent": "x", "depends_on": []}
        ]}))


def test_rejects_malformed_json():
    with pytest.raises(tp.PlanError, match="valid JSON"):
        tp.build_plan("do it", TOOLS, _gen("not json at all"))


def test_empty_request_is_rejected():
    with pytest.raises(tp.PlanError, match="empty request"):
        tp.build_plan("   ", TOOLS, _gen({"steps": []}))


def test_forward_dependencies_are_discarded():
    """depends_on may only reference EARLIER steps, or execution order breaks."""
    steps = tp.build_plan("x", TOOLS, _gen({"steps": [
        {"tool": "web_search", "arguments": {}, "intent": "a", "depends_on": [5]},
    ]}))
    assert steps[0]["depends_on"] == []


def test_step_count_is_capped():
    many = [{"tool": "web_search", "arguments": {}, "intent": f"s{i}", "depends_on": []} for i in range(30)]
    steps = tp.build_plan("x", TOOLS, _gen({"steps": many}))
    assert len(steps) <= tp.MAX_STEPS


def test_no_workable_plan_is_reported_clearly():
    steps = tp.build_plan("teleport me", TOOLS, _gen({"steps": []}))
    assert steps == []
    assert "can't accomplish" in tp.format_plan("teleport me", steps)


# ── execution ──────────────────────────────────────────────────────────────

def _steps():
    return [
        {"tool": "weather_report", "arguments": {}, "intent": "get weather", "depends_on": []},
        {"tool": "send_message", "arguments": {}, "intent": "text John", "depends_on": [0]},
    ]


def test_all_steps_succeed():
    report = tp.execute_plan(_steps(), lambda t, a: f"{t} completed successfully")
    assert report["ok"] is True
    assert report["completed"] == 2


def test_dependent_step_is_skipped_when_prerequisite_fails():
    """Running a dependent step on a broken prerequisite is exactly how a
    'success' gets reported for work that never happened."""
    def run(tool, args):
        return "could not fetch weather" if tool == "weather_report" else "sent!"

    report = tp.execute_plan(_steps(), run)

    assert report["ok"] is False
    assert report["results"][0]["status"] == "failed"
    assert report["results"][1]["status"] == "skipped"
    assert report["completed"] == 0


def test_raising_tool_is_recorded_not_propagated():
    def run(tool, args):
        raise RuntimeError("boom")

    report = tp.execute_plan(_steps(), run)
    assert report["ok"] is False
    assert "boom" in report["results"][0]["detail"]


def test_independent_steps_still_run_after_an_unrelated_failure():
    steps = [
        {"tool": "web_search", "arguments": {}, "intent": "a", "depends_on": []},
        {"tool": "weather_report", "arguments": {}, "intent": "b", "depends_on": []},
    ]
    report = tp.execute_plan(steps, lambda t, a: "failed" if t == "web_search" else "fine")
    assert report["results"][0]["status"] == "failed"
    assert report["results"][1]["status"] == "done"


def test_execution_summary_is_readable():
    report = tp.execute_plan(_steps(), lambda t, a: "done nicely")
    text = tp.format_execution(report)
    assert "All 2 step(s) completed" in text
    assert "[OK]" in text
