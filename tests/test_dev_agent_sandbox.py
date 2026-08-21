from pathlib import Path

from actions import dev_agent as dev_agent_module


def test_sandbox_validation_applies_code_after_success(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "demo.py"
    target.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

    success, report = dev_agent_module._sandbox_test_and_apply(
        target_path=target,
        proposed_code="def hello():\n    return 'hi!'\n",
        project_root=project_root,
    )

    assert success is True
    assert "Sandbox validation passed" in report
    assert target.read_text(encoding="utf-8") == "def hello():\n    return 'hi!'\n"


def test_sandbox_validation_blocks_invalid_code(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "demo.py"
    target.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

    success, report = dev_agent_module._sandbox_test_and_apply(
        target_path=target,
        proposed_code="def hello(:\n    return 'broken'\n",
        project_root=project_root,
    )

    assert success is False
    assert "Sandbox validation failed" in report
    assert target.read_text(encoding="utf-8") == "def hello():\n    return 'hi'\n"


def test_sandbox_reporting_notifies_player_and_speech(tmp_path: Path) -> None:
    class DummyPlayer:
        def __init__(self) -> None:
            self.content_reports: list[tuple[str, str]] = []
            self.logs: list[str] = []

        def show_content(self, title: str, text: str) -> None:
            self.content_reports.append((title, text))

        def write_log(self, text: str) -> None:
            self.logs.append(text)

    class DummySpeech:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def __call__(self, text: str) -> None:
            self.messages.append(text)

    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "demo.py"
    target.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

    player = DummyPlayer()
    speech = DummySpeech()

    success, report = dev_agent_module._sandbox_test_and_apply(
        target_path=target,
        proposed_code="def hello():\n    return 'hi!'\n",
        project_root=project_root,
        player=player,
        speak=speech,
        change_note="update the greeting",
        reason="Make the output more expressive",
        next_step="Try the updated greeting in the UI",
    )

    assert success is True
    assert "Sandbox validation passed" in report
    assert player.content_reports
    # Panel ORDER is not the contract — a "Proposed diff" panel now precedes
    # the edit report so a self-improvement is reviewable before it lands.
    # Assert the edit report exists and says the right things.
    edit_reports = [text for title, text in player.content_reports if title == "Jarvis edit"]
    assert edit_reports
    assert "update the greeting" in edit_reports[0]
    assert "Reason:" in edit_reports[0]
    assert "Next:" in edit_reports[0]
    assert len(player.content_reports) >= 2

    # The diff itself must be surfaced, not just a prose description of it.
    diff_reports = [text for title, text in player.content_reports if title == "Proposed diff"]
    assert diff_reports
    assert "+" in diff_reports[0] and "-" in diff_reports[0]
    assert any("Applied to workspace" in report_text for _, report_text in player.content_reports)
    assert speech.messages
    assert any("sandboxed update" in msg.lower() or "applying the validated change" in msg.lower() or "updated" in msg.lower() for msg in speech.messages)
