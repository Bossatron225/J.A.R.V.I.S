import json

import memory.conversation_log as conversation_log


def _isolate(tmp_path, monkeypatch):
    conv_path = tmp_path / "conversations.jsonl"
    monkeypatch.setattr(conversation_log, "DATA_DIR", tmp_path)
    monkeypatch.setattr(conversation_log, "CONV_PATH", conv_path)
    return conv_path


def test_append_turn_persists_without_network(tmp_path, monkeypatch):
    conv_path = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(conversation_log, "embed_text", lambda *a, **k: None)

    entry = conversation_log.append_turn("session-1", "mac", "user", "What's on my calendar today?")

    assert entry is not None
    assert conv_path.exists()
    lines = conv_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["session_id"] == "session-1"
    assert saved["source"] == "mac"
    assert saved["role"] == "user"
    assert saved["text"] == "What's on my calendar today?"
    assert saved["embedding"] is None


def test_append_turn_ignores_empty_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert conversation_log.append_turn("session-1", "mac", "user", "   ") is None


def test_get_session_turns_filters_by_session(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(conversation_log, "embed_text", lambda *a, **k: None)

    conversation_log.append_turn("session-1", "mac", "user", "hello from session one")
    conversation_log.append_turn("session-2", "vps", "user", "hello from session two")

    turns = conversation_log.get_session_turns("session-1")
    assert len(turns) == 1
    assert turns[0]["text"] == "hello from session one"


def test_search_conversations_falls_back_to_substring_match(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(conversation_log, "embed_text", lambda *a, **k: None)

    conversation_log.append_turn("session-1", "mac", "user", "remind me to water the plants")
    conversation_log.append_turn("session-1", "mac", "assistant", "the weather is sunny today")

    matches = conversation_log.search_conversations("plants")
    assert len(matches) == 1
    assert "plants" in matches[0]["text"]


def test_search_conversations_ranks_by_cosine_similarity(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    vectors = {
        "close match to the query": [1.0, 0.0],
        "totally unrelated text": [0.0, 1.0],
    }

    def fake_embed(text, task_type="RETRIEVAL_DOCUMENT"):
        return vectors.get(text, [0.9, 0.1])

    monkeypatch.setattr(conversation_log, "embed_text", fake_embed)

    conversation_log.append_turn("session-1", "mac", "user", "close match to the query")
    conversation_log.append_turn("session-1", "mac", "assistant", "totally unrelated text")

    matches = conversation_log.search_conversations("query about the topic", limit=2)
    assert matches[0]["text"] == "close match to the query"


def test_merge_turns_into_store_dedupes_by_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(conversation_log, "embed_text", lambda *a, **k: None)

    local_entry = conversation_log.append_turn("session-1", "mac", "user", "local turn")
    remote_turn = {
        "id": "remote-turn-id",
        "session_id": "session-2",
        "source": "vps",
        "role": "user",
        "text": "remote turn",
        "ts": "2026-01-01T00:00:00Z",
        "embedding": None,
    }

    added = conversation_log.merge_turns_into_store([remote_turn, local_entry])

    assert added == 1  # local_entry already known, only the remote turn is new
    all_turns = conversation_log._read_all_turns()
    assert {t["id"] for t in all_turns} == {local_entry["id"], "remote-turn-id"}


def test_trim_keeps_most_recent_turns(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(conversation_log, "embed_text", lambda *a, **k: None)
    monkeypatch.setattr(conversation_log, "MAX_TURNS", 3)

    for i in range(5):
        conversation_log.append_turn("session-1", "mac", "user", f"turn {i}")

    remaining = conversation_log._read_all_turns()
    assert len(remaining) == 3
    assert [t["text"] for t in remaining] == ["turn 2", "turn 3", "turn 4"]
