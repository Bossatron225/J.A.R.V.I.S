"""Structured, leveled logging plus an audit trail of consequential actions.

Written after repeatedly misdiagnosing live failures because the log could not
be trusted:

  * stdout was block-buffered, so a perfectly healthy process looked hung —
    its startup lines simply had not been flushed yet;
  * raw Gemini embedding vectors were printed in full, burying real events
    under ~300KB of floats in a 4.4MB file;
  * every line was a bare print() with no level, timestamp, or component, so
    the log could not be filtered or searched.

The result was diagnosing outages by attaching a debugger to running
processes. This module fixes the substance of that: line-buffered output,
levels, timestamps, component tags, and hard truncation of large payloads.

It also records an audit trail (memory/audit_log.jsonl) of actions with real
world effects — messages sent, code modified, people detected, tools invoked
— so "what did you do while I was out?" has a truthful answer. Jarvis can send
messages, rewrite its own source, and watch a camera; that deserves a record.
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
AUDIT_PATH = DATA_DIR / "audit_log.jsonl"

# Any single logged value longer than this is truncated. Embedding vectors are
# the reason: one relayed tool result could otherwise dump thousands of floats.
MAX_VALUE_CHARS = 400
MAX_AUDIT_ENTRIES = 20000

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

_lock = threading.Lock()
_audit_lock = threading.Lock()


def _min_level() -> int:
    return LEVELS.get(str(os.getenv("JARVIS_LOG_LEVEL", "INFO")).upper(), 20)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def truncate(value, limit: int = MAX_VALUE_CHARS) -> str:
    """Render any value for logging, collapsing anything oversized.

    Long numeric sequences are summarised rather than printed: a 768-float
    embedding says nothing useful in a log but destroys its readability."""
    try:
        if isinstance(value, (list, tuple)) and len(value) > 12:
            if all(isinstance(x, (int, float)) for x in value[:12]):
                return f"<{len(value)} numbers: [{value[0]:.4g}, {value[1]:.4g}, …]>"
            return f"<{len(value)} items>"
        if isinstance(value, dict):
            rendered = {k: truncate(v, 120) for k, v in list(value.items())[:12]}
            text = json.dumps(rendered, default=str)
        else:
            text = value if isinstance(value, str) else str(value)
    except Exception:
        text = "<unrenderable>"
    if len(text) > limit:
        return text[:limit] + f"…<+{len(text) - limit} chars>"
    return text


def log(level: str, component: str, message, **fields) -> None:
    """Emit one structured line. Always flushed — unflushed output is what made
    a running process look dead."""
    level = level.upper()
    if LEVELS.get(level, 20) < _min_level():
        return
    parts = [f"{_now()}", f"{level:<5}", f"[{component}]", truncate(message, 1000)]
    for key, value in fields.items():
        parts.append(f"{key}={truncate(value, 200)}")
    line = " ".join(parts)
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def debug(component: str, message, **f) -> None: log("DEBUG", component, message, **f)
def info(component: str, message, **f) -> None: log("INFO", component, message, **f)
def warn(component: str, message, **f) -> None: log("WARN", component, message, **f)
def error(component: str, message, **f) -> None: log("ERROR", component, message, **f)


# ── Audit trail ────────────────────────────────────────────────────────────

def record_action(action: str, detail: str = "", **fields) -> None:
    """Append one consequential action to the audit trail. Best-effort and
    never raises — auditing must not be able to break the action itself."""
    try:
        entry = {
            "ts": _now(),
            "action": action,
            "detail": truncate(detail, 500),
            **{k: truncate(v, 200) for k, v in fields.items()},
        }
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_audit() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    entries: list[dict] = []
    with _audit_lock:
        try:
            with AUDIT_PATH.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
    return entries


def trim_audit_if_needed() -> None:
    entries = _read_audit()
    if len(entries) <= MAX_AUDIT_ENTRIES:
        return
    keep = entries[-MAX_AUDIT_ENTRIES:]
    with _audit_lock:
        with AUDIT_PATH.open("w", encoding="utf-8") as handle:
            for entry in keep:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_actions(limit: int = 20, since: str | None = None, action: str | None = None) -> list[dict]:
    entries = _read_audit()
    if since:
        entries = [e for e in entries if str(e.get("ts", "")) >= since]
    if action:
        entries = [e for e in entries if str(e.get("action", "")) == action]
    entries.sort(key=lambda e: str(e.get("ts", "")))
    return entries[-limit:] if limit else entries


def format_actions(entries: list[dict]) -> str:
    if not entries:
        return "I haven't taken any recorded actions in that period, sir."
    lines = [f"{len(entries)} recorded action(s), most recent last:"]
    for entry in entries:
        ts = str(entry.get("ts", ""))
        line = f"  [{ts}] {entry.get('action', '?')}"
        if entry.get("detail"):
            line += f" — {entry['detail']}"
        lines.append(line)
    return "\n".join(lines)


def activity_report(parameters: dict | None = None, response=None, player=None,
                    session_memory=None, speak=None) -> str:
    p = parameters or {}
    limit = int(p.get("limit", 20) or 20)
    return format_actions(recent_actions(limit=limit, action=str(p.get("action") or "") or None))
