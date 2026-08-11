import json

from actions import dev_agent as dev_agent_module


def test_parse_self_improvement_plan_extracts_improvements_and_suggestions() -> None:
    raw = json.dumps(
        {
            "improvements": [
                {
                    "file": "actions/dev_agent.py",
                    "change": "Add a broader repo scan for self-improvements",
                    "reason": "Improve coverage",
                }
            ],
            "feature_suggestions": [
                {
                    "name": "Improvement report",
                    "description": "Summarize what was improved and what to try next.",
                }
            ],
        }
    )

    parsed = dev_agent_module._parse_self_improvement_plan(raw)

    assert parsed["improvements"][0]["file"] == "actions/dev_agent.py"
    assert parsed["feature_suggestions"][0]["name"] == "Improvement report"


def test_dev_agent_honors_approval_action_even_without_self_improve_keyword(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_apply_queued_self_improvement(**kwargs):
        calls["called"] = True
        return "approved"

    monkeypatch.setattr(dev_agent_module, "_apply_queued_self_improvement", fake_apply_queued_self_improvement)
    monkeypatch.setattr(dev_agent_module, "_build_project", lambda *args, **kwargs: "fallback")

    result = dev_agent_module.dev_agent(
        {
            "description": "Make a small change",
            "approval_action": "apply",
            "approval_id": "abc123",
        }
    )

    assert result == "approved"
    assert calls["called"] is True
