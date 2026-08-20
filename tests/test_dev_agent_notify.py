from actions import dev_agent as dev_agent_module


def test_record_change_notifies_user(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_agent_module, "CHANGELOG_PATH", tmp_path / "dev_agent_changelog.json")

    calls = []
    monkeypatch.setattr("actions.notify.notify_user", lambda message: calls.append(message))

    dev_agent_module._record_change("main.py", "fixed a bug in the tool handler")

    assert len(calls) == 1
    assert "main.py" in calls[0]
    assert "fixed a bug in the tool handler" in calls[0]


def test_record_change_still_saves_changelog_entry(tmp_path, monkeypatch):
    changelog_path = tmp_path / "dev_agent_changelog.json"
    monkeypatch.setattr(dev_agent_module, "CHANGELOG_PATH", changelog_path)
    monkeypatch.setattr("actions.notify.notify_user", lambda message: None)

    dev_agent_module._record_change("core/tts.py", "added a new engine")

    store = dev_agent_module._load_changelog_store()
    assert store["entries"][-1]["file"] == "core/tts.py"
    assert store["entries"][-1]["change"] == "added a new engine"
