"""Unified attention — "what actually needs me right now?"

Jarvis can read mail, iMessages, the calendar, the visitor log, standing goals
and open loops. He does all of it in separate silos, only when asked, and
never joins them up. So the question a person actually has several times a day
— not "read my emails" but "what matters this morning?" — has no answer.

This is the synthesis layer. It pulls from every source that already exists,
scores each item for genuine urgency, and returns a short ranked briefing.

Two design rules:

* Every source is independently fault-isolated. One unavailable integration
  (mail down, VPS unreachable) must degrade that source only, never take the
  whole briefing with it.
* Scoring is transparent and local — no model call. A briefing that is slow or
  costs money is one nobody asks for, and a ranking nobody can inspect is one
  nobody trusts.
"""
from datetime import datetime, timedelta, timezone

# Urgency bands. Kept coarse on purpose: fine-grained scores imply a precision
# this genuinely does not have.
URGENT = 3
NOTABLE = 2
BACKGROUND = 1

_LABELS = {URGENT: "needs you", NOTABLE: "worth knowing", BACKGROUND: "background"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(fn, default):
    """Run one source, never letting its failure break the briefing."""
    try:
        return fn()
    except Exception:
        return default


# An integration that is unconfigured or misused answers with a help/error
# string rather than raising. Surfacing that as "needs you" turns a setup gap
# into a fake urgent item — precisely the noise this feature must avoid.
_NON_CONTENT_MARKERS = (
    "not ready yet",
    "unknown ",
    "not configured",
    "unavailable",
    "is only available on",
    "please specify",
    "could not",
    "no credential",
    "error",
)


def _is_real_content(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return not any(marker in lowered for marker in _NON_CONTENT_MARKERS)


# ── sources ────────────────────────────────────────────────────────────────

def _calendar_items() -> list[dict]:
    from actions.google_calendar import google_calendar
    raw = google_calendar({"action": "list", "max_results": 5})
    text = str(raw or "").strip()
    if not _is_real_content(text) or "no upcoming" in text.lower():
        return []
    return [{
        "source": "calendar",
        "score": URGENT,
        "text": f"Calendar: {text[:200]}",
    }]


def _visitor_items() -> list[dict]:
    from actions.visitor_log import list_recent_sightings
    cutoff = (_now() - timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    recent = [s for s in list_recent_sightings(limit=20) if str(s.get("ts", "")) >= cutoff]
    if not recent:
        return []

    # A face seen repeatedly is the security-relevant signal, not raw count.
    repeats = max((int(s.get("sighting_count_at_time", 1)) for s in recent), default=1)
    score = URGENT if repeats >= 3 else NOTABLE
    return [{
        "source": "visitors",
        "score": score,
        "text": (
            f"{len(recent)} unrecognised visitor sighting(s) in the last 12h"
            + (f"; one face seen {repeats} times" if repeats > 1 else "")
        ),
    }]


def _goal_items() -> list[dict]:
    from actions.goals import list_goals
    items = []
    for goal in list_goals():
        if goal.get("status") != "active":
            continue
        findings = int(goal.get("findings", 0))
        if findings:
            items.append({
                "source": "goals",
                "score": NOTABLE,
                "text": f"Goal “{goal.get('objective', '')[:70]}” has {findings} finding(s)",
            })
    return items


def _open_loop_items() -> list[dict]:
    from actions.open_loops import collect_open_loops
    loops = collect_open_loops(min_age_hours=24)
    if not loops:
        return []
    return [{
        "source": "open_loops",
        "score": BACKGROUND,
        "text": f"{len(loops)} unfinished item(s), oldest: {loops[0]['summary'][:90]}",
    }]


def _message_items() -> list[dict]:
    from actions.imessage_integration import imessage_control
    raw = imessage_control({"action": "read_unread", "limit": 5})
    text = str(raw or "").strip()
    if not _is_real_content(text) or "no unread" in text.lower():
        return []
    return [{"source": "messages", "score": URGENT, "text": f"Messages: {text[:200]}"}]


def _mail_items() -> list[dict]:
    from actions.mail_integration import mail_control
    raw = mail_control({"action": "read_unread", "limit": 5})
    text = str(raw or "").strip()
    if not _is_real_content(text) or "no unread" in text.lower():
        return []
    return [{"source": "mail", "score": NOTABLE, "text": f"Mail: {text[:200]}"}]


SOURCES = [
    _calendar_items,
    _message_items,
    _visitor_items,
    _mail_items,
    _goal_items,
    _open_loop_items,
]


def collect_attention(min_score: int = BACKGROUND) -> list[dict]:
    """Gather and rank across every source, most urgent first."""
    items: list[dict] = []
    for source in SOURCES:
        items.extend(_safe(source, []))
    items = [i for i in items if i.get("score", 0) >= min_score]
    items.sort(key=lambda i: i.get("score", 0), reverse=True)
    return items


def format_attention(items: list[dict]) -> str:
    if not items:
        return "Nothing needs you right now, sir."
    urgent = [i for i in items if i["score"] == URGENT]
    lead = (
        f"{len(urgent)} thing(s) need you, sir."
        if urgent else "Nothing urgent, sir — a few things worth knowing."
    )
    lines = [lead]
    for item in items:
        lines.append(f"  [{_LABELS.get(item['score'], '?')}] {item['text']}")
    return "\n".join(lines)


def attention_briefing(parameters: dict | None = None, response=None, player=None,
                       session_memory=None, speak=None) -> str:
    p = parameters or {}
    urgent_only = bool(p.get("urgent_only", False))
    return format_attention(collect_attention(min_score=URGENT if urgent_only else BACKGROUND))
