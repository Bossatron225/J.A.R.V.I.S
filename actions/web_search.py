#web_search.py
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_SAFARI_CACHE_TTL_SECS = 600
_safari_cache: dict = {"ts": 0.0, "profile": {}}

_SEARCH_PARAM_KEYS = {
    "q", "query", "p", "text", "wd", "k", "search", "term", "keyword"
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "i", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "you", "your", "news",
    "latest", "today", "www", "http", "https", "com", "net", "org",
}

_PERSONALIZATION_KEY = "search_personalization"

# Category-level synonym groups for broad topic controls.
_TOPIC_SYNONYMS: dict[str, set[str]] = {
    "cars": {
        "car", "cars", "auto", "autos", "automotive", "vehicle", "vehicles",
        "motorsport", "f1", "formula 1", "formula1", "racing",
        "alfa", "romeo", "ferrari", "bmw", "mercedes", "audi", "tesla",
        "porsche", "honda", "toyota", "ford", "nissan", "stelvio", "quadrifoglio",
    },
    "crypto": {
        "crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth",
        "blockchain", "altcoin", "token", "tokens", "defi", "nft",
    },
    "ai": {
        "ai", "artificial intelligence", "machine learning", "ml", "llm",
        "openai", "chatgpt", "gemini", "anthropic",
    },
    "gaming": {
        "gaming", "game", "games", "xbox", "playstation", "ps5", "steam",
        "esports", "nintendo", "switch",
    },
    "finance": {
        "finance", "stocks", "stock", "market", "economy", "investing",
        "nasdaq", "dow", "s&p", "forex", "trading",
    },
    "football": {
        "football", "soccer", "premier league", "champions league", "uefa",
        "fifa", "world cup",
    },
    "basketball": {
        "basketball", "nba", "wnba", "ncaa basketball",
    },
    "music": {
        "music", "song", "songs", "album", "albums", "artist", "artists",
        "spotify", "apple music", "concert",
    },
    "movies": {
        "movie", "movies", "film", "films", "cinema", "netflix", "series",
        "tv", "tv show", "streaming",
    },
}

_TERM_TO_CANONICAL: dict[str, str] = {}
for _canon, _terms in _TOPIC_SYNONYMS.items():
    _TERM_TO_CANONICAL[_canon] = _canon
    for _t in _terms:
        _TERM_TO_CANONICAL[_t] = _canon


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _normalize_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", (topic or "").strip().lower())


def _topic_words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{1,}", (text or "").lower())


def _expand_topic(topic: str) -> set[str]:
    """Expand a user topic into matching terms using built-in category synonyms."""
    topic_n = _normalize_topic(topic)
    if not topic_n:
        return set()

    expanded = {topic_n}

    canonical = _TERM_TO_CANONICAL.get(topic_n)
    if canonical:
        expanded.add(canonical)
        expanded.update(_TOPIC_SYNONYMS.get(canonical, set()))

    for token in _topic_words(topic_n):
        can = _TERM_TO_CANONICAL.get(token)
        if can:
            expanded.add(can)
            expanded.update(_TOPIC_SYNONYMS.get(can, set()))

    # Keep only non-empty normalized terms.
    return {_normalize_topic(t) for t in expanded if _normalize_topic(t)}


def _expand_topics(topics: list[str]) -> set[str]:
    expanded: set[str] = set()
    for topic in topics:
        expanded.update(_expand_topic(topic))
    return expanded


def _text_matches_terms(text: str, terms: set[str]) -> bool:
    text_l = (text or "").lower()
    if not text_l or not terms:
        return False

    tokens = set(_topic_words(text_l))
    for term in terms:
        if not term:
            continue
        if " " in term:
            if term in text_l:
                return True
            continue
        if term in tokens:
            return True
        if len(term) >= 4 and any(term in tok for tok in tokens):
            return True
    return False


def _default_personalization_settings() -> dict:
    return {
        "use_safari_history": True,
        "allow_topics": [],
        "block_topics": [],
    }


def _load_personalization_settings() -> dict:
    try:
        from memory.memory_manager import load_memory

        memory = load_memory()
        raw = memory.get(_PERSONALIZATION_KEY, {})
        if not isinstance(raw, dict):
            raw = {}

        settings = _default_personalization_settings()
        settings["use_safari_history"] = bool(raw.get("use_safari_history", True))
        settings["allow_topics"] = [
            _normalize_topic(x)
            for x in raw.get("allow_topics", [])
            if isinstance(x, str) and _normalize_topic(x)
        ]
        settings["block_topics"] = [
            _normalize_topic(x)
            for x in raw.get("block_topics", [])
            if isinstance(x, str) and _normalize_topic(x)
        ]
        return settings
    except Exception as e:
        print(f"[WebSearch] ⚠️ Personalization settings load failed: {e}")
        return _default_personalization_settings()


def _save_personalization_settings(settings: dict) -> None:
    from memory.memory_manager import load_memory, save_memory

    memory = load_memory()
    memory[_PERSONALIZATION_KEY] = {
        "use_safari_history": bool(settings.get("use_safari_history", True)),
        "allow_topics": [
            _normalize_topic(x)
            for x in settings.get("allow_topics", [])
            if _normalize_topic(x)
        ][:20],
        "block_topics": [
            _normalize_topic(x)
            for x in settings.get("block_topics", [])
            if _normalize_topic(x)
        ][:20],
    }
    save_memory(memory)


def _is_safari_personalization_enabled() -> bool:
    """Enable by default on macOS; disable with JARVIS_USE_SAFARI_HISTORY=0."""
    if sys.platform != "darwin":
        return False
    if os.environ.get("JARVIS_USE_SAFARI_HISTORY", "1").strip() in {"0", "false", "False"}:
        return False
    settings = _load_personalization_settings()
    return bool(settings.get("use_safari_history", True))


def _topic_is_blocked(text: str, blocked_topics: list[str]) -> bool:
    return _text_matches_terms(text, _expand_topics(blocked_topics))


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", (text or "").lower())
    out = []
    for w in words:
        if w in _STOPWORDS:
            continue
        if any(ch.isdigit() for ch in w):
            continue
        out.append(w)
    return out


def _extract_query_terms(url: str) -> list[str]:
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        return []

    terms: list[str] = []
    for key, values in q.items():
        if key.lower() not in _SEARCH_PARAM_KEYS:
            continue
        for val in values:
            decoded = unquote_plus(val or "")
            terms.extend(_tokenize(decoded))
    return terms


def _safari_history_profile(max_rows: int = 800) -> dict:
    """
    Build a compact interest profile from recent Safari history.
    Returns only aggregated topics/domains (not raw browsing history).
    """
    if not _is_safari_personalization_enabled():
        return {"available": False, "reason": "disabled"}

    db_path = Path.home() / "Library" / "Safari" / "History.db"
    if not db_path.exists():
        return {"available": False, "reason": "missing_db"}

    tmp_db = None
    try:
        with tempfile.NamedTemporaryFile(suffix="_safari_history.db", delete=False) as f:
            tmp_db = Path(f.name)
        tmp_db.write_bytes(db_path.read_bytes())

        conn = sqlite3.connect(str(tmp_db))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT hi.url, COALESCE(hv.title, '')
            FROM history_visits hv
            JOIN history_items hi ON hi.id = hv.history_item
            WHERE hv.load_successful = 1
            ORDER BY hv.visit_time DESC
            LIMIT ?
            """,
            (max_rows,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"available": False, "reason": f"read_error:{e}"}
    finally:
        if tmp_db:
            try:
                tmp_db.unlink(missing_ok=True)
            except Exception:
                pass

    term_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    settings = _load_personalization_settings()
    allow_topics = settings.get("allow_topics", [])
    block_topics = settings.get("block_topics", [])
    allow_terms = _expand_topics(allow_topics)
    block_terms = _expand_topics(block_topics)

    for url, title in rows:
        u = str(url or "")
        t = str(title or "")

        try:
            host = urlparse(u).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                if _text_matches_terms(host, block_terms):
                    continue
                domain_counter[host] += 1
        except Exception:
            pass

        raw_terms = _extract_query_terms(u) + _tokenize(t)
        for term in raw_terms:
            if _text_matches_terms(term, block_terms):
                continue
            term_counter[term] += 1

    top_terms = [k for k, _ in term_counter.most_common(8)]
    if allow_terms:
        boosted = [t for t in top_terms if _text_matches_terms(t, allow_terms)]
        others = [t for t in top_terms if t not in boosted]
        top_terms = boosted + others

    # Keep explicit allow-list topics at the front so personalization can be guided.
    for topic in reversed(allow_topics):
        if topic in top_terms:
            top_terms.remove(topic)
        top_terms.insert(0, topic)
    top_terms = top_terms[:8]

    top_domains = [k for k, _ in domain_counter.most_common(6)]

    if not top_terms and not top_domains:
        return {"available": False, "reason": "no_signal"}

    return {
        "available": True,
        "top_terms": top_terms,
        "top_domains": top_domains,
    }


def _get_safari_profile_cached() -> dict:
    now = time.time()
    if now - _safari_cache["ts"] < _SAFARI_CACHE_TTL_SECS and _safari_cache["profile"]:
        return _safari_cache["profile"]

    profile = _safari_history_profile()
    _safari_cache["ts"] = now
    _safari_cache["profile"] = profile
    return profile


def _reset_safari_profile_cache() -> None:
    _safari_cache["ts"] = 0.0
    _safari_cache["profile"] = {}


def _is_generic_news_query(query: str) -> bool:
    q = (query or "").strip().lower()
    return q in {"", "news", "latest news", "world news", "headlines", "top news"}


def _personalization_context() -> str:
    profile = _get_safari_profile_cached()
    if not profile.get("available"):
        return ""

    terms = ", ".join(profile.get("top_terms", [])[:5])
    domains = ", ".join(profile.get("top_domains", [])[:3])
    parts = []
    if terms:
        parts.append(f"recent interest topics: {terms}")
    if domains:
        parts.append(f"frequent sites: {domains}")
    if not parts:
        return ""
    return "; ".join(parts)


def _format_interest_profile(profile: dict, settings: dict) -> str:
    lines = ["Personalized search profile:"]
    enabled = "on" if settings.get("use_safari_history", True) else "off"
    lines.append(f"- Safari history personalization: {enabled}")

    allow_topics = settings.get("allow_topics", [])
    block_topics = settings.get("block_topics", [])
    lines.append("- Allow topics: " + (", ".join(allow_topics) if allow_topics else "(none)"))
    lines.append("- Block topics: " + (", ".join(block_topics) if block_topics else "(none)"))

    def _preview(topics: list[str]) -> str:
        if not topics:
            return "(none)"
        chunks = []
        for topic in topics[:3]:
            ex = sorted(_expand_topic(topic))
            ex = [x for x in ex if x != topic][:4]
            if ex:
                chunks.append(f"{topic} -> {', '.join(ex)}")
            else:
                chunks.append(f"{topic} -> (exact)")
        return " | ".join(chunks)

    lines.append("- Allow expansion preview: " + _preview(allow_topics))
    lines.append("- Block expansion preview: " + _preview(block_topics))

    if profile.get("available"):
        top_terms = profile.get("top_terms", [])
        top_domains = profile.get("top_domains", [])
        lines.append("- Inferred topics: " + (", ".join(top_terms[:8]) if top_terms else "(none)"))
        lines.append("- Frequent domains: " + (", ".join(top_domains[:6]) if top_domains else "(none)"))
    else:
        lines.append(f"- Inferred topics: unavailable ({profile.get('reason', 'unknown')})")

    return "\n".join(lines)


def manage_interest_profile(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Manage Safari-derived personalization profile and topic allow/block lists.

    Actions:
      - show (default)
      - allow_add / allow_remove / allow_clear
      - block_add / block_remove / block_clear
      - enable / disable
      - refresh
    """
    params = parameters or {}
    action = str(params.get("action", "show")).strip().lower()
    topic = _normalize_topic(params.get("topic", ""))

    settings = _load_personalization_settings()

    if action == "allow_add":
        if not topic:
            return "Please provide a topic."
        if topic not in settings["allow_topics"]:
            settings["allow_topics"].append(topic)
        if topic in settings["block_topics"]:
            settings["block_topics"].remove(topic)
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return f"Added to allow topics: {topic}"

    if action == "allow_remove":
        if not topic:
            return "Please provide a topic."
        if topic in settings["allow_topics"]:
            settings["allow_topics"].remove(topic)
            _save_personalization_settings(settings)
            _reset_safari_profile_cache()
            return f"Removed from allow topics: {topic}"
        return f"Topic not in allow list: {topic}"

    if action == "allow_clear":
        settings["allow_topics"] = []
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return "Cleared allow topics."

    if action == "block_add":
        if not topic:
            return "Please provide a topic."
        if topic not in settings["block_topics"]:
            settings["block_topics"].append(topic)
        if topic in settings["allow_topics"]:
            settings["allow_topics"].remove(topic)
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return f"Added to block topics: {topic}"

    if action == "block_remove":
        if not topic:
            return "Please provide a topic."
        if topic in settings["block_topics"]:
            settings["block_topics"].remove(topic)
            _save_personalization_settings(settings)
            _reset_safari_profile_cache()
            return f"Removed from block topics: {topic}"
        return f"Topic not in block list: {topic}"

    if action == "block_clear":
        settings["block_topics"] = []
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return "Cleared block topics."

    if action == "enable":
        settings["use_safari_history"] = True
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return "Safari history personalization enabled."

    if action == "disable":
        settings["use_safari_history"] = False
        _save_personalization_settings(settings)
        _reset_safari_profile_cache()
        return "Safari history personalization disabled."

    if action == "refresh":
        _reset_safari_profile_cache()

    profile = _get_safari_profile_cached()
    return _format_interest_profile(profile, settings)


def _personalize_gemini_query(query: str, mode: str) -> str:
    context = _personalization_context()
    if not context:
        return query

    return (
        f"{query}\n\n"
        "Personalization context from the user's recent Safari browsing activity "
        f"(aggregated): {context}. "
        "Use this only to prioritize relevance; do not mention private browsing data."
    )


def _personalize_ddg_query(query: str, mode: str) -> str:
    profile = _get_safari_profile_cached()
    if not profile.get("available"):
        return query

    terms = profile.get("top_terms", [])
    if not terms:
        return query

    if mode == "news" and _is_generic_news_query(query):
        # Make generic news requests more tailored.
        return f"{query or 'world news today'} {terms[0]} {terms[1] if len(terms) > 1 else ''}".strip()
    return query


def _gemini_search_direct(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="models/gemini-flash-lite-latest",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _gemini_search(query: str) -> str:
    """Search grounding is geo-restricted for this VPS's datacenter region ("User
    location is not supported for the API use") but works fine from the Mac — see
    core/local_relay.py. Route there when running headless on the VPS; call Gemini
    directly otherwise (this is also what runs *inside* the relay call on the Mac
    itself, since local_relay.is_available() is only ever true on the VPS)."""
    from core import local_relay
    if local_relay.is_available():
        result = local_relay.call("gemini_relay", {"kind": "search", "query": query}, timeout=25.0)
        payload = result.get("result") if isinstance(result, dict) else None
        if isinstance(payload, dict) and payload.get("ok") and payload.get("text"):
            return payload["text"]
        raise RuntimeError(
            (isinstance(payload, dict) and payload.get("error"))
            or result.get("message")
            or "local Gemini relay failed"
        )
    return _gemini_search_direct(query)


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re

    raw = _gemini_search(f"Current world news: {n} headlines. Numbered list, titles only.")

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback."""
    gemini_query = _personalize_gemini_query(query, mode="search")
    try:
        return _gemini_search(gemini_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini failed ({e}) — trying DDG...")
        ddg_query = _personalize_ddg_query(query, mode="search")
        results = _ddg_search(ddg_query)
        return _format_ddg(query, results)


def _news(query: str) -> str:
    """
    Runs Gemini grounded search AND DDG news in parallel.
    Returns whichever delivers a valid result first; cancels the other.
    """
    import threading

    news_query   = query if query else "world news today"
    gemini_query = _personalize_gemini_query(
        f"latest news today: {news_query}",
        mode="news",
    )
    ddg_query    = _personalize_ddg_query(news_query, mode="news")

    result_box  = [None]   # first valid result lands here
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(r: str) -> None:
        if r and len(r) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini news failed ({e})")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()

    done_evt.wait(timeout=10.0)
    return result_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    research_query = _personalize_gemini_query(research_query, mode="research")
    try:
        return _gemini_search(research_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Research Gemini failed ({e}) — DDG fallback...")
        results = _ddg_search(query, max_results=10)
        return _format_ddg(query, results)


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Price Gemini failed ({e}) — DDG fallback...")
        results = _ddg_search(f"{query} price buy", max_results=6)
        return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"
