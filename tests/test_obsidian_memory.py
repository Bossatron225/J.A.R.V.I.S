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


def test_build_personal_memory_context_includes_json_and_obsidian(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_OBSIDIAN_VAULT_PATH", str(tmp_path))
    importlib.reload(obsidian_memory)

    obsidian_memory.remember_user_fact("Preferences", "Prefers tea over coffee")
    context = obsidian_memory.build_personal_memory_context(
        {"preferences": {"favorite_drink": {"value": "coffee"}}},
        obsidian_memory.recall_user_profile(),
    )

    assert "WHAT YOU KNOW ABOUT THIS PERSON" in context
    assert "Favorite Drink" in context
    assert "LOCAL OBSIDIAN MEMORY" in context
    assert "Prefers tea over coffee" in context


def test_recall_personal_memory_searches_both_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_OBSIDIAN_VAULT_PATH", str(tmp_path))
    importlib.reload(obsidian_memory)

    obsidian_memory.remember_user_fact("Preferences", "Prefers tea over coffee")
    result = obsidian_memory.recall_personal_memory("tea")

    assert "tea" in result.lower()
    assert "obsidian" in result.lower() or "json" in result.lower()
