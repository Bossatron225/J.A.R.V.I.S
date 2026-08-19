import json
import math
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
DATA_DIR        = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
CONV_PATH       = DATA_DIR / "conversations.jsonl"
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

EMBED_MODEL          = "gemini-embedding-001"
EMBED_DIMENSIONALITY = 256
MAX_TURNS            = 4000
TEXT_MAX_CHARS       = 4000

_lock = Lock()


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_all_turns() -> list[dict]:
    if not CONV_PATH.exists():
        return []
    turns: list[dict] = []
    with _lock:
        try:
            with CONV_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(entry, dict) and entry.get("id"):
                        turns.append(entry)
        except Exception as e:
            print(f"[ConversationLog] ⚠️ Read error: {e}")
            return []
    return turns


def _write_all_turns(turns: list[dict]) -> None:
    CONV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with CONV_PATH.open("w", encoding="utf-8") as f:
            for entry in turns:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_line(entry: dict) -> None:
    CONV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with CONV_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _trim_if_needed() -> None:
    turns = _read_all_turns()
    if len(turns) <= MAX_TURNS:
        return
    turns.sort(key=lambda t: str(t.get("ts", "")))
    _write_all_turns(turns[-MAX_TURNS:])


def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float] | None:
    """One Gemini embedding call. Returns None on any failure — callers must
    tolerate turns without an embedding and fall back to substring matching."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from google import genai as _genai
        from google.genai import types as _types

        client = _genai.Client(api_key=_get_api_key())
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text[:TEXT_MAX_CHARS],
            config=_types.EmbedContentConfig(
                output_dimensionality=EMBED_DIMENSIONALITY,
                task_type=task_type,
            ),
        )
        if resp.embeddings:
            return list(resp.embeddings[0].values)
    except Exception as e:
        print(f"[ConversationLog] ⚠️ Embedding failed: {e}")
    return None


def append_turn(session_id: str, source: str, role: str, text: str) -> dict | None:
    """Persist one conversation turn and kick off embedding in the background.
    Non-blocking on the caller — safe to call from a live session loop."""
    text = (text or "").strip()
    if not text:
        return None
    entry = {
        "id":         uuid.uuid4().hex,
        "session_id": session_id,
        "source":     source,
        "role":       role,
        "text":       text[:TEXT_MAX_CHARS],
        "ts":         _now_iso(),
        "embedding":  None,
    }
    _append_line(entry)
    _trim_if_needed()

    def _embed_and_store() -> None:
        vector = embed_text(entry["text"], task_type="RETRIEVAL_DOCUMENT")
        if not vector:
            return
        turns = _read_all_turns()
        for t in turns:
            if t.get("id") == entry["id"]:
                t["embedding"] = vector
                break
        _write_all_turns(turns)

    threading.Thread(target=_embed_and_store, daemon=True).start()
    return entry


def get_session_turns(session_id: str) -> list[dict]:
    return [t for t in _read_all_turns() if t.get("session_id") == session_id]


def list_recent_turns(limit: int = 200, since: str | None = None) -> list[dict]:
    turns = _read_all_turns()
    if since:
        turns = [t for t in turns if str(t.get("ts", "")) > since]
    turns.sort(key=lambda t: str(t.get("ts", "")))
    return turns[-limit:] if limit else turns


def merge_turns_into_store(remote_turns: list[dict]) -> int:
    """Merge incoming turns into the local store by id, persist, return count added."""
    if not remote_turns:
        return 0
    local = _read_all_turns()
    known_ids = {t.get("id") for t in local}
    added = 0
    for turn in remote_turns:
        if not isinstance(turn, dict) or not turn.get("id"):
            continue
        if turn["id"] in known_ids:
            continue
        local.append(turn)
        known_ids.add(turn["id"])
        added += 1
    if added:
        local.sort(key=lambda t: str(t.get("ts", "")))
        _write_all_turns(local)
        _trim_if_needed()
    return added


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def search_conversations(query: str, limit: int = 6) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    turns = _read_all_turns()
    if not turns:
        return []

    query_vector = embed_text(query, task_type="RETRIEVAL_QUERY")
    query_lower  = query.lower()

    scored: list[tuple[float, dict]] = []
    for turn in turns:
        text = str(turn.get("text", ""))
        if not text:
            continue
        vector = turn.get("embedding")
        if query_vector and isinstance(vector, list) and vector:
            score = _cosine(query_vector, vector)
        else:
            score = 1.0 if query_lower in text.lower() else 0.0
        if score > 0:
            scored.append((score, turn))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for score, turn in scored[:limit]:
        results.append({
            "source":     turn.get("source"),
            "role":       turn.get("role"),
            "text":       turn.get("text"),
            "ts":         turn.get("ts"),
            "session_id": turn.get("session_id"),
            "score":      round(score, 4),
        })
    return results


def recall_with_vps_sync(query: str, base_url: str | None, limit: int = 6) -> list[dict]:
    """Pull recent remote turns down, merge+persist them locally, then search
    the merged store. Mirrors memory.remote_sync.load_memory_with_vps_sync."""
    base_url = (base_url or "").strip()
    if base_url:
        try:
            from memory.remote_sync import fetch_remote_conversations
            remote_turns = fetch_remote_conversations(base_url)
            merge_turns_into_store(remote_turns)
        except Exception as e:
            print(f"[ConversationLog] ⚠️ VPS conversation fetch failed: {e}")
    return search_conversations(query, limit=limit)
