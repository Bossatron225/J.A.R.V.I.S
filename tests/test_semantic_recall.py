import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import memory_manager, semantic_recall


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", tmp_path / "long_term.json")
    # No embeddings in tests — exercises the substring fallback path, which is
    # what runs whenever the API is unreachable.
    monkeypatch.setattr(semantic_recall, "_embedding_helpers", lambda: (None, None))
    monkeypatch.setattr("memory.obsidian_memory.recall_user_profile", lambda: "")


def _seed(memory: dict):
    memory_manager.save_memory(memory)


def test_storage_cap_is_decoupled_from_prompt_cap():
    """The bug this fixes: one 2200-char number capped BOTH how much Jarvis
    could remember and how much fit in the prompt, so facts were deleted to fit
    a prompt budget."""
    assert memory_manager.MEMORY_MAX_CHARS > memory_manager.PROMPT_MAX_CHARS * 50
    assert memory_manager.PROMPT_MAX_CHARS == 2000


def test_memory_well_past_the_old_cap_is_not_trimmed():
    memory = memory_manager._empty_memory()
    for i in range(200):
        memory["notes"][f"note_{i}"] = {"value": f"a fact worth keeping number {i}", "updated": "2026-08-01"}
    _seed(memory)

    reloaded = memory_manager.load_memory()

    # Far beyond the old 2200-char ceiling, which would have deleted most of these.
    assert len(json.dumps(reloaded)) > 2200
    assert len(reloaded["notes"]) == 200


def test_trimming_sacrifices_notes_before_identity(monkeypatch):
    monkeypatch.setattr(memory_manager, "MEMORY_MAX_CHARS", 900)
    memory = memory_manager._empty_memory()
    memory["identity"]["name"] = {"value": "James Lumsden", "updated": "2020-01-01"}
    memory["relationships"]["partner"] = {"value": "Nevaeh", "updated": "2020-01-01"}
    for i in range(60):
        memory["notes"][f"n{i}"] = {"value": f"disposable note {i}", "updated": "2026-08-01"}

    _seed(memory)
    reloaded = memory_manager.load_memory()

    # Durable categories survive even though they are the OLDEST entries —
    # the old oldest-first rule would have deleted them first.
    assert "name" in reloaded["identity"]
    assert "partner" in reloaded["relationships"]
    assert len(reloaded["notes"]) < 60


def test_search_finds_fact_whose_key_differs_from_the_question():
    """The concrete failure of the old substring search: asking about a
    'partner' could never match a fact stored as `girlfriend_name`."""
    memory = memory_manager._empty_memory()
    memory["relationships"]["girlfriend_name"] = {"value": "Nevaeh", "updated": "2026-08-06"}
    _seed(memory)

    results = semantic_recall.search_memory("girlfriend", include_conversations=False)

    assert results
    assert any("Nevaeh" in r["text"] for r in results)


def test_search_reaches_facts_beyond_the_prompt_cap():
    """Anything past PROMPT_MAX_CHARS is absent from the injected prompt; recall
    must still find it, which the old prompt-text search could not."""
    memory = memory_manager._empty_memory()
    for i in range(300):
        memory["notes"][f"filler_{i}"] = {"value": f"filler text {i}", "updated": "2026-08-01"}
    memory["notes"]["buried_fact"] = {"value": "the spare key is under the blue plant pot", "updated": "2026-08-01"}
    _seed(memory)

    prompt_text = memory_manager.format_memory_for_prompt(memory_manager.load_memory())
    assert "blue plant pot" not in prompt_text  # genuinely not in the prompt

    results = semantic_recall.search_memory("where is the spare key", include_conversations=False)
    assert any("blue plant pot" in r["text"] for r in results)


def test_search_returns_empty_for_blank_query():
    assert semantic_recall.search_memory("") == []


def test_results_are_ranked_best_first():
    memory = memory_manager._empty_memory()
    memory["notes"]["exact"] = {"value": "the garage door code", "updated": "2026-08-01"}
    memory["notes"]["partial"] = {"value": "the garage is cold", "updated": "2026-08-01"}
    _seed(memory)

    results = semantic_recall.search_memory("garage door code", include_conversations=False)

    assert results
    assert results[0]["score"] >= results[-1]["score"]
    assert "door code" in results[0]["text"]


def test_format_recall_handles_no_results():
    assert "nothing stored" in semantic_recall.format_recall("anything", [])


def test_recall_personal_memory_uses_semantic_search():
    from memory.obsidian_memory import recall_personal_memory

    memory = memory_manager._empty_memory()
    memory["relationships"]["girlfriend_name"] = {"value": "Nevaeh", "updated": "2026-08-06"}
    _seed(memory)

    assert "Nevaeh" in recall_personal_memory("girlfriend")


def test_recall_never_hard_fails(monkeypatch):
    """Recall degrading is acceptable; recall raising is not."""
    monkeypatch.setattr(
        semantic_recall, "search_memory",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    from memory.obsidian_memory import recall_personal_memory

    result = recall_personal_memory("anything")  # must not raise
    assert isinstance(result, str)
