"""Semantic search across every memory store Jarvis has.

Replaces substring matching over a truncated view. The previous
recall_personal_memory() searched format_memory_for_prompt() output — which is
itself capped at PROMPT_MAX_CHARS — so anything beyond that cap was
unreachable, and matching was literal: asking "who is my partner" could never
surface a fact stored under `girlfriend_name`.

This searches the FULL stores (structured JSON facts, the Obsidian profile,
and past conversation turns) by meaning, using the same Gemini embedding
helper the conversation log already relies on. It degrades to substring
matching whenever embeddings are unavailable (offline, no API key, tests), so
recall never simply stops working.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
EMBED_CACHE_PATH = DATA_DIR / "memory_embed_cache.json"

MAX_CACHE_ENTRIES = 5000

_cache_lock = threading.Lock()
_cache: dict[str, list[float]] | None = None


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        if EMBED_CACHE_PATH.exists():
            data = json.loads(EMBED_CACHE_PATH.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        else:
            _cache = {}
    except Exception:
        _cache = {}
    return _cache


def _save_cache() -> None:
    cache = _load_cache()
    try:
        if len(cache) > MAX_CACHE_ENTRIES:
            # Cheap bound: keep an arbitrary recent slice rather than growing
            # without limit. Entries are pure derived data, so dropping any is
            # only ever a cache miss.
            cache = dict(list(cache.items())[-MAX_CACHE_ENTRIES:])
            globals()["_cache"] = cache
        EMBED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EMBED_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _embed_cached(text: str, embed_text) -> list[float] | None:
    """Embed a stored memory once and reuse it.

    Without this, every recall re-embedded every candidate — one API call per
    memory entry per question (~20+ calls, several seconds, and real cost) for
    text that almost never changes. Stored facts are keyed by content hash, so
    an edited fact simply misses and is re-embedded."""
    key = _cache_key(text)
    cache = _load_cache()
    with _cache_lock:
        hit = cache.get(key)
    if hit is not None:
        return hit

    vector = embed_text(text, task_type="RETRIEVAL_DOCUMENT")
    if vector:
        with _cache_lock:
            cache[key] = list(vector)
        _save_cache()
    return vector


def _embedding_helpers():
    """Late import so this module stays importable without the genai stack."""
    try:
        from memory.conversation_log import _cosine, embed_text
        return embed_text, _cosine
    except Exception:
        return None, None


def _iter_json_facts(memory: dict):
    """Yield (label, text) for every structured fact, including ones that never
    make it into the prompt."""
    for category, items in (memory or {}).items():
        if category == "sessions" or not isinstance(items, dict):
            continue
        for key, entry in items.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if not value:
                continue
            readable = key.replace("_", " ")
            yield (f"{category}/{key}", f"{readable}: {value}")

    for item in (memory or {}).get("sessions", []) or []:
        if isinstance(item, dict) and item.get("summary"):
            yield (f"session/{item.get('date', '')}", str(item["summary"]))


def _iter_obsidian_lines(profile_text: str):
    for raw in (profile_text or "").splitlines():
        line = raw.strip().lstrip("-*# ").strip()
        if len(line) > 3:
            yield ("obsidian", line)


def _score_substring(query: str, text: str) -> float:
    q = query.lower().strip()
    t = text.lower()
    if not q:
        return 0.0
    if q in t:
        return 1.0
    # Partial credit for overlapping words, so multi-word questions still rank
    # sensibly when the exact phrase is absent.
    words = [w for w in q.split() if len(w) > 2]
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in t)
    return hits / len(words) * 0.9


def search_memory(query: str, limit: int = 6, include_conversations: bool = True) -> list[dict]:
    """Rank every stored memory against the query. Returns
    [{"source", "text", "score"}] best-first."""
    query = (query or "").strip()
    if not query:
        return []

    from memory.memory_manager import load_memory
    from memory.obsidian_memory import recall_user_profile

    candidates: list[tuple[str, str]] = []
    try:
        candidates.extend(_iter_json_facts(load_memory()))
    except Exception:
        pass
    try:
        candidates.extend(_iter_obsidian_lines(recall_user_profile()))
    except Exception:
        pass

    if include_conversations:
        try:
            from memory.conversation_log import search_conversations
            for turn in search_conversations(query, limit=limit) or []:
                text = str(turn.get("text", "")).strip()
                if text:
                    candidates.append((f"conversation/{turn.get('role', '')}", text))
        except Exception:
            pass

    if not candidates:
        return []

    embed_text, cosine = _embedding_helpers()
    query_vector = embed_text(query, task_type="RETRIEVAL_QUERY") if embed_text else None

    scored: list[dict] = []
    for source, text in candidates:
        score = 0.0
        if query_vector and embed_text and cosine:
            vector = _embed_cached(text, embed_text)
            if vector:
                score = cosine(query_vector, vector)
        if not score:
            score = _score_substring(query, text)
        if score > 0:
            scored.append({"source": source, "text": text, "score": round(float(score), 4)})

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def format_recall(query: str, results: list[dict]) -> str:
    if not results:
        return f"I have nothing stored about '{query}', sir."
    lines = [f"Here is what I know relating to '{query}', sir:"]
    for item in results:
        lines.append(f"  - {item['text']}  [{item['source']}]")
    return "\n".join(lines)
