import importlib
import os

import memory.obsidian_memory as obsidian_memory


def test_remember_and_recall_user_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_OBSIDIAN_VAULT_PATH", str(tmp_path))
    importlib.reload(obsidian_memory)

    result = obsidian_memory.remember_user_fact("Preferences", "Prefers tea over coffee")

    assert "Successfully saved" in result
    assert (tmp_path / "USER_PROFILE.md").exists()
    assert "Prefers tea over coffee" in obsidian_memory.recall_user_profile()
