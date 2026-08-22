"""Graceful degradation — keep Jarvis useful when the cloud is not.

Today Jarvis is entirely cloud-dependent: if Google is unreachable, there is
no voice, no reasoning, no tools. Nothing degrades — it all just stops, and
usually silently. That is not hypothetical:

  * Gemini returned 503 ("high demand") twice in one afternoon;
  * the live session died on a clean websocket close and never reconnected,
    dropping every dashboard command without a word;
  * the VPS needs a bespoke probe script because Google was rejecting its IP
    with "User location is not supported".

This module gives a floor beneath all of that: a connectivity check, a set of
common intents answered entirely locally, and local speech. It deliberately
adds NO dependencies — speech uses macOS's built-in `say`, and intent handling
is rule-based. Pulling a local LLM or a neural TTS stack (torch, onnxruntime)
into a working system to serve a fallback path would add more risk than the
fallback removes.

Scope is deliberately small. This is not a replacement brain — it answers the
handful of things worth answering when the network is gone, and says so
plainly the rest of the time rather than failing silently.
"""
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

# Treat the cloud as reachable for this long after a successful check, so a
# burst of commands doesn't cause a burst of probes.
_PROBE_CACHE_SECONDS = 20.0
_PROBE_TIMEOUT = 3.0
_PROBE_URL = "https://generativelanguage.googleapis.com/"

_state_lock = threading.Lock()
_last_probe_ts = 0.0
_last_probe_ok = True


def cloud_reachable(force: bool = False) -> bool:
    """Cheap, cached reachability probe. Any HTTP response — including 4xx —
    counts as reachable: we are testing the network path, not authorisation."""
    global _last_probe_ts, _last_probe_ok
    now = time.monotonic()
    with _state_lock:
        if not force and (now - _last_probe_ts) < _PROBE_CACHE_SECONDS:
            return _last_probe_ok

    # Uses requests rather than urllib: this Python install has no CA bundle
    # wired into urllib, so urlopen fails with CERTIFICATE_VERIFY_FAILED even
    # when the network is perfectly fine — which would report a healthy system
    # as permanently DEGRADED. requests bundles certifi and is already a
    # dependency here.
    ok = False
    try:
        import requests
        requests.get(_PROBE_URL, timeout=_PROBE_TIMEOUT)
        ok = True  # any HTTP response means the path is up
    except Exception:
        try:
            urllib.request.urlopen(_PROBE_URL, timeout=_PROBE_TIMEOUT)
            ok = True
        except urllib.error.HTTPError:
            ok = True
        except Exception:
            ok = False

    with _state_lock:
        _last_probe_ts = now
        _last_probe_ok = ok
    return ok


def fallback_voice_allowed() -> bool:
    """Whether Jarvis may speak in a voice OTHER than his configured one.

    Off by default and deliberately so: James's instruction is that if
    ElevenLabs is unavailable he wants NO voice at all, rather than Jarvis
    speaking in a substitute. A different voice is a different assistant, and
    silence is better than an impostor. Offline replies still appear as text.
    """
    try:
        import json
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        return bool(json.loads(cfg_path.read_text(encoding="utf-8")).get("fallback_voice_enabled", False))
    except Exception:
        return False


def speak_offline(text: str, voice: str = "Daniel") -> bool:
    """Speak without the network, ONLY if a substitute voice is permitted.

    Returns False (silently, text-only) by default — see
    fallback_voice_allowed()."""
    text = (text or "").strip()
    if not fallback_voice_allowed():
        return False
    if not text or platform.system() != "Darwin" or not shutil.which("say"):
        return False
    try:
        subprocess.Popen(
            ["say", "-v", voice, text[:600]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


# ── Local intents ──────────────────────────────────────────────────────────
# Only things answerable from the local machine or local files. Anything
# needing a model, a search, or an API belongs in the "can't do that offline"
# path, not here.

def _intent_time(_text: str) -> str | None:
    from datetime import datetime
    return f"It is {datetime.now().strftime('%H:%M on %A, %d %B')}, sir."


def _intent_system_status(_text: str) -> str | None:
    try:
        from actions.system_monitor import get_system_status
        return str(get_system_status())
    except Exception:
        return "I can't read system status right now, sir."


def _intent_memory(text: str) -> str | None:
    query = re.sub(
        r"^(what|who|where|when|do you know|tell me|remind me)\b.*?\b(about|is|are|was|were)\b",
        "", text, flags=re.IGNORECASE,
    ).strip() or text
    try:
        from memory.semantic_recall import format_recall, search_memory
        # include_conversations=False: conversation search may embed, which
        # needs the network we have just established is unavailable.
        return format_recall(query, search_memory(query, limit=4, include_conversations=False))
    except Exception:
        return None


def _intent_visitors(_text: str) -> str | None:
    try:
        from actions.visitor_log import visitor_log
        return visitor_log({"action": "recent", "limit": 5})
    except Exception:
        return None


def _intent_goals(_text: str) -> str | None:
    try:
        from actions.goals import format_goals, list_goals
        return format_goals(list_goals())
    except Exception:
        return None


def _intent_health(_text: str) -> str | None:
    return (
        "I'm running in degraded mode, sir — the cloud is unreachable, so I'm "
        "limited to local information until the connection returns."
    )


# Ordered: first match wins, so more specific patterns come first.
LOCAL_INTENTS: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"\b(what.*time|what.*date|what day)\b", re.I), _intent_time),
    (re.compile(r"\b(cpu|ram|memory usage|disk|temperature|system status)\b", re.I), _intent_system_status),
    (re.compile(r"\b(visitors?|who.*been|anyone.*seen|at the door|nanny.?cam)\b", re.I), _intent_visitors),
    (re.compile(r"\b(goals?|working on|watching for)\b", re.I), _intent_goals),
    (re.compile(r"\b(are you (ok|working|online)|status|diagnostic|offline)\b", re.I), _intent_health),
    (re.compile(r"\b(remember|recall|who is|what is|my )\b", re.I), _intent_memory),
]

OFFLINE_REFUSAL = (
    "I'm offline, sir — the cloud is unreachable, so I can't reason about that or "
    "reach any online service. I can still tell you the time, system status, recent "
    "visitors, your standing goals, and anything in memory."
)


def handle_locally(text: str) -> str:
    """Best local answer for a command, or a clear refusal.

    Always returns something: silent failure is the behaviour this exists to
    replace."""
    text = (text or "").strip()
    if not text:
        return OFFLINE_REFUSAL
    for pattern, handler in LOCAL_INTENTS:
        if pattern.search(text):
            try:
                answer = handler(text)
            except Exception:
                answer = None
            if answer:
                return answer
    return OFFLINE_REFUSAL


def degraded_status() -> str:
    if cloud_reachable():
        return "Online — full capability available."
    return "DEGRADED — cloud unreachable; local answers and local speech only."
