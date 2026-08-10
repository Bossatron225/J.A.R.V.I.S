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
