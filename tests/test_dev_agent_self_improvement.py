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


def test_apply_queued_self_improvement_uses_single_pending_plan_for_unknown_id(monkeypatch) -> None:
    calls: dict[str, object] = {}

    store = {
        "pending": [
            {
                "approval_id": "abc123",
                "plan": {
                    "improvements": [
                        {
                            "file": "tests/test_dev_agent_self_improvement.py",
                            "change": "Make a small change",
                            "reason": "Regression coverage",
                        }
                    ]
                },
            }
        ]
    }

    monkeypatch.setattr(dev_agent_module, "_load_approvals_store", lambda: store)
    monkeypatch.setattr(dev_agent_module, "_save_approvals_store", lambda value: None)

    def fake_generate_replacement(*args, **kwargs):
        calls["generate"] = True
        return "print('ok')"

    def fake_sandbox_test_and_apply(*args, **kwargs):
        calls["sandbox"] = True
        return True, "applied"

    monkeypatch.setattr(dev_agent_module, "_generate_replacement_for_file", fake_generate_replacement)
    monkeypatch.setattr(dev_agent_module, "_sandbox_test_and_apply", fake_sandbox_test_and_apply)

    result = dev_agent_module._apply_queued_self_improvement(
        approval_id="missing-id",
        timeout=5,
        self_reboot=False,
        speak=None,
        player=None,
    )

    assert calls["generate"] is True
    assert calls["sandbox"] is True
    assert "Applied approved self-improvement plan" in result
