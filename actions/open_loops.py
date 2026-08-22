"""Open-loop tracking — unfinished business Jarvis should follow through on.

Jarvis executes a request and then forgets it happened. Nothing notices
unfulfilled intent: a task that half-failed, a self-improvement proposal
queued days ago and never approved, a commitment mentioned in conversation
and never acted on. That gap is most of the difference between a command
runner and an assistant.

Loops are derived from records that already exist rather than a new store:
the audit trail (what was actually done), the dev-agent approval queue, and
long-term memory (what was said). Each loop carries an age so the caller can
surface the stale ones instead of reciting everything.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Only surface a loop once it has had a fair chance to resolve on its own.
DEFAULT_MIN_AGE_HOURS = 6

# Phrasing that signals a commitment rather than an observation. Kept explicit
# and conservative — a false "you said you'd do X" is worse than a miss.
_COMMITMENT_PATTERNS = [
    re.compile(r"\bi(?:'ll| will| am going to| intend to)\b", re.IGNORECASE),
    re.compile(r"\bremind me to\b", re.IGNORECASE),
    re.compile(r"\bneed to\b", re.IGNORECASE),
    re.compile(r"\bmust\b", re.IGNORECASE),
    re.compile(r"\bshould\b", re.IGNORECASE),
    re.compile(r"\bdon'?t (?:let me )?forget\b", re.IGNORECASE),
]

# Words suggesting the thing already happened, so it is not still open.
_RESOLVED_HINTS = ("done", "finished", "completed", "sorted", "cancelled", "no longer")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _age_hours(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return (_now() - ts).total_seconds() / 3600.0


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "unknown age"
    if hours < 1:
        return "less than an hour ago"
    if hours < 24:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24:.0f} days ago"


def looks_like_commitment(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < 8:
        return False
    lowered = text.lower()
    if any(hint in lowered for hint in _RESOLVED_HINTS):
        return False
    return any(pattern.search(text) for pattern in _COMMITMENT_PATTERNS)


def _pending_self_improvements() -> list[dict]:
    """dev_agent proposals queued for approval and never applied."""
    path = BASE_DIR / "memory" / "dev_agent_approvals.json"
    loops: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return loops

    for item in (data.get("pending") or []):
        if not isinstance(item, dict):
            continue
        ts = _parse_ts(item.get("created_at", ""))
        loops.append({
            "kind": "pending_self_improvement",
            "summary": str(item.get("description", ""))[:140] or "self-improvement proposal",
            "reference": str(item.get("approval_id", "")),
            "age_hours": _age_hours(ts),
        })
    return loops


def _failed_actions(limit: int = 400) -> list[dict]:
    """Actions the audit trail recorded as not completing."""
    loops: list[dict] = []
    try:
        from core.jarvis_logging import recent_actions
        entries = recent_actions(limit=limit)
    except Exception:
        return loops

    for entry in entries:
        detail = str(entry.get("detail", "")) + " " + str(entry.get("result", ""))
        lowered = detail.lower()
        if not any(m in lowered for m in ("failed", "could not", "unavailable", "timed out", "error")):
            continue
        ts = _parse_ts(entry.get("ts", ""))
        loops.append({
            "kind": "failed_action",
            "summary": f"{entry.get('action', 'action')}: {detail.strip()[:120]}",
            "reference": str(entry.get("ts", "")),
            "age_hours": _age_hours(ts),
        })
    return loops


def _memory_commitments() -> list[dict]:
    """Stated intentions recorded in long-term memory."""
    loops: list[dict] = []
    try:
        from memory.memory_manager import load_memory
        memory = load_memory()
    except Exception:
        return loops

    for category in ("wishes", "projects", "notes"):
        for key, entry in (memory.get(category) or {}).items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if not value or not looks_like_commitment(str(value)):
                continue
            updated = entry.get("updated") if isinstance(entry, dict) else ""
            ts = _parse_ts(f"{updated}T00:00:00+00:00") if updated else None
            loops.append({
                "kind": "commitment",
                "summary": f"{key.replace('_', ' ')}: {value}"[:140],
                "reference": f"{category}/{key}",
                "age_hours": _age_hours(ts),
            })
    return loops


def collect_open_loops(min_age_hours: float = DEFAULT_MIN_AGE_HOURS) -> list[dict]:
    """Everything still outstanding and old enough to be worth raising,
    oldest first."""
    loops = _pending_self_improvements() + _failed_actions() + _memory_commitments()
    ripe = [
        loop for loop in loops
        if loop["age_hours"] is None or loop["age_hours"] >= min_age_hours
    ]
    ripe.sort(key=lambda item: item["age_hours"] or 0.0, reverse=True)
    return ripe


def format_open_loops(loops: list[dict]) -> str:
    if not loops:
        return "Nothing outstanding, sir — no unfinished business I can see."
    labels = {
        "pending_self_improvement": "awaiting your approval",
        "failed_action": "did not complete",
        "commitment": "you mentioned",
    }
    lines = [f"{len(loops)} thing(s) still open, sir:"]
    for loop in loops:
        label = labels.get(loop["kind"], loop["kind"])
        lines.append(f"  - [{label}] {loop['summary']} ({_fmt_age(loop['age_hours'])})")
    return "\n".join(lines)


def open_loops(parameters: dict | None = None, response=None, player=None,
               session_memory=None, speak=None) -> str:
    p = parameters or {}
    min_age = float(p.get("min_age_hours", DEFAULT_MIN_AGE_HOURS) or DEFAULT_MIN_AGE_HOURS)
    kind = str(p.get("kind", "") or "").strip()
    loops = collect_open_loops(min_age_hours=min_age)
    if kind:
        loops = [loop for loop in loops if loop["kind"] == kind]
    return format_open_loops(loops)
