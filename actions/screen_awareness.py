"""Ambient screen awareness — notice the user is stuck, and offer.

`visual_context` gives Jarvis ambient awareness through the CAMERA (what the
user is physically doing). For the SCREEN he is blind unless asked directly
(`screen_process`) or pointed at one target (`visual_watch`). Nothing watched
the desktop as a whole, so he could never say "that build failed twenty minutes
ago, want me to look at it?".

PRIVACY — read this before changing anything here.

This is continuous capture of everything on the user's screen, sent to a cloud
vision API. That is categorically more sensitive than the camera feed, because
a screen contains credentials, banking, private messages and other people's
information. The design is therefore restrictive by default:

  * OFF unless explicitly enabled. There is no implicit start.
  * The sensitivity check runs BEFORE the screenshot is taken, never after. A
    banking page or password manager is never captured at all, so there is no
    sensitive image to leak, redact, or accidentally cache.
  * It stays silent while the biometric lock is engaged — if Jarvis cannot tell
    who is at the machine, he should not be narrating the screen.
  * The user can add their own blocked apps and title keywords.
  * Every capture decision is logged, so what was and wasn't looked at is
    auditable after the fact.

The bar for speaking is deliberately high: an assistant that comments on your
screen every minute is spyware with a personality. It speaks only when it can
offer something concrete.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
STATE_PATH = DATA_DIR / "screen_awareness.json"

_MODEL = "models/gemini-flash-latest"

# Expected steady state is one look every few minutes. This ceiling only trips
# if scheduling has gone wrong — the same runaway guard visual_context uses,
# set lower because screenshots are larger and this runs against a screen.
MAX_CALLS_PER_HOUR = 60

DEFAULT_INTERVAL_SECONDS = 240.0
MIN_INTERVAL_SECONDS = 60.0

# Applications never captured, whatever is on screen. Matched case-insensitively
# against the frontmost application name.
BLOCKED_APPS = {
    "1password", "1password 7", "1password 8", "bitwarden", "lastpass", "dashlane",
    "keeper", "enpass", "nordpass", "proton pass", "strongbox",
    "keychain access", "passwords", "system settings", "system preferences",
    "authy", "google authenticator", "duo mobile",
    "banking", "gnucash", "quicken",
    "terminal", "iterm", "iterm2",  # secrets are routinely pasted into shells
}

# Window/tab title keywords that block capture. Deliberately broad: a false
# skip costs nothing, a false capture is a leaked secret.
BLOCKED_TITLE_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bpassword", r"\bpasscode", r"\bpassphrase", r"\bcredential",
        r"\bsign[- ]?in\b", r"\blog[- ]?in\b", r"\blogon\b", r"\bauth\w*",
        r"\b2fa\b", r"\bmfa\b", r"\botp\b", r"\bverification code",
        r"\bbank\w*", r"\bbanking", r"\brevolut", r"\bmonzo", r"\bpaypal",
        r"\bstripe\b", r"\bcheckout\b", r"\bbilling\b", r"\bpayment",
        r"\bcredit card", r"\biban\b", r"\bsort code",
        r"\bprivate browsing", r"\bincognito",
        r"\bkeychain", r"\bsecret", r"\bapi[ _-]?key", r"\bwallet\b",
        r"\.env\b", r"\bssh\b", r"\bgpg\b",
        r"\bmedical\b", r"\bhealth record", r"\btherap\w*",
    )
]


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def is_enabled() -> bool:
    """Off unless explicitly turned on. Never defaults to True."""
    return bool(_load_state().get("enabled", False))


def set_enabled(enabled: bool) -> None:
    state = _load_state()
    state["enabled"] = bool(enabled)
    state["changed_at"] = time.time()
    _save_state(state)


def interval_seconds() -> float:
    raw = _load_state().get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        return max(MIN_INTERVAL_SECONDS, float(raw))
    except Exception:
        return DEFAULT_INTERVAL_SECONDS


def user_blocked_apps() -> set[str]:
    return {str(a).strip().lower() for a in _load_state().get("blocked_apps", []) if str(a).strip()}


def user_blocked_keywords() -> list[str]:
    return [str(k).strip() for k in _load_state().get("blocked_keywords", []) if str(k).strip()]


def block_app(name: str) -> str:
    state = _load_state()
    apps = [str(a) for a in state.get("blocked_apps", [])]
    if name.strip().lower() in {a.lower() for a in apps}:
        return f"I already never look at {name}, sir."
    apps.append(name.strip())
    state["blocked_apps"] = apps
    _save_state(state)
    return f"Understood, sir — I'll never look at the screen while {name} is in front."


def frontmost_context() -> tuple[str, str]:
    """(application name, front window title). Empty strings if unavailable.

    Deliberately fails CLOSED at the call site: when this cannot be determined,
    `is_sensitive` treats it as sensitive, because an unknown window might be
    anything."""
    try:
        script = (
            'tell application "System Events" to set p to first application process '
            'whose frontmost is true\n'
            'set n to name of p\n'
            'try\n'
            '  set w to name of front window of p\n'
            'on error\n'
            '  set w to ""\n'
            'end try\n'
            'return n & "||" & w'
        )
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        app, _, title = out.partition("||")
        return app.strip(), title.strip()
    except Exception:
        return "", ""


def is_sensitive(app: str, title: str) -> tuple[bool, str]:
    """Should this screen be left alone? Returns (sensitive, reason).

    Fails CLOSED: an unidentifiable frontmost window counts as sensitive."""
    app_l = str(app or "").strip().lower()
    title_s = str(title or "").strip()

    if not app_l:
        return True, "could not identify the frontmost application"

    if app_l in BLOCKED_APPS or app_l in user_blocked_apps():
        return True, f"{app} is on the never-look list"

    # Substring pass, so "1Password 8 — Personal" is caught alongside "1Password".
    for blocked in BLOCKED_APPS | user_blocked_apps():
        if blocked in app_l:
            return True, f"{app} is on the never-look list"

    for pattern in BLOCKED_TITLE_PATTERNS:
        if pattern.search(title_s):
            return True, "the window title suggests credentials or financial content"

    for keyword in user_blocked_keywords():
        if keyword.lower() in title_s.lower():
            return True, f"the window title contains “{keyword}”"

    return False, ""


def should_look(lock_active: bool = False) -> tuple[bool, str]:
    """Every gate, in order, before anything is captured."""
    if not is_enabled():
        return False, "screen awareness is off"
    if lock_active:
        return False, "the biometric lock is engaged"

    app, title = frontmost_context()
    sensitive, reason = is_sensitive(app, title)
    if sensitive:
        return False, reason

    try:
        from memory.usage_log import calls_since
        if calls_since("screen_awareness", 3600) >= MAX_CALLS_PER_HOUR:
            return False, "hourly look limit reached"
    except Exception:
        pass

    return True, f"{app}: {title}" if title else app


_PROMPT = """You are JARVIS, glancing once at James's screen to see whether you can genuinely HELP right now.

You are looking for a specific, actionable situation where an offer of help would be welcome — for example:
an error or failed build left on screen, a test suite showing failures, a form or document left half-finished,
an obvious stuck state, something clearly waiting on a decision.

You are NOT a narrator. Do not comment on what he is reading, browsing, watching, or writing just because it
is visible. Ordinary productive work is not something to interrupt.
{last_context}
Be strict: if there is nothing concrete you could actually DO or usefully point out, say should_speak=false.
When in doubt, say false. Most glances should return false.

Never mention or describe any personal, financial, medical or private content you happen to see. If the screen
contains anything of that kind, return should_speak=false and nothing else.

Return ONLY valid JSON, no markdown, exactly:
{{"should_speak": true or false, "observation": "one short sentence naming what you noticed", "offer": "one short sentence offering a specific next step"}}
"""


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _get_api_key() -> str:
    try:
        cfg = BASE_DIR / "config" / "api_keys.json"
        if cfg.exists():
            return str(json.loads(cfg.read_text(encoding="utf-8")).get("gemini_api_key", "") or "").strip()
    except Exception:
        pass
    return ""


def analyse_screen(png_bytes: bytes, last_observation: str | None = None) -> dict:
    """One vision call over a screenshot. Returns {"should_speak": bool, ...};
    always returns that key, so callers never need to catch."""
    if not png_bytes:
        return {"should_speak": False}

    api_key = _get_api_key()
    if not api_key:
        return {"should_speak": False}

    record_usage = None
    try:
        from memory.usage_log import record_usage as _record
        record_usage = _record
    except Exception:
        pass

    try:
        from google import genai
        from google.genai import types as gtypes

        last_context = (
            f'\nYour last observation was: "{last_observation}" — do not repeat it or say something '
            f'too similar. If the situation is unchanged, say should_speak=false.\n'
            if last_observation else ""
        )
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_MODEL,
            contents=[gtypes.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                      _PROMPT.format(last_context=last_context)],
        )
        if record_usage:
            record_usage("screen_awareness", "gemini_image", 1)
        parsed = json.loads(_strip_fences(getattr(response, "text", "") or ""))
        return {
            "should_speak": bool(parsed.get("should_speak", False)),
            "observation": str(parsed.get("observation", "") or "").strip(),
            "offer": str(parsed.get("offer", "") or "").strip(),
        }
    except Exception:
        return {"should_speak": False}


def capture_screen_png() -> bytes:
    """Screenshot the main display. Returns b"" on failure."""
    try:
        from actions.screen_processor import _capture_screen
        data, _fmt = _capture_screen()
        return data or b""
    except Exception:
        return b""


def status_text() -> str:
    state = _load_state()
    if not state.get("enabled"):
        return ("Ambient screen awareness is off, sir. Say “start watching my screen” to enable it — "
                "I'll look every few minutes and only speak when I can actually help, and I'll never "
                "look at password managers, banking, or anything that looks like a login.")
    extra = ""
    blocked = sorted(user_blocked_apps())
    if blocked:
        extra = f" You've also told me never to look at: {', '.join(blocked)}."
    return (f"Ambient screen awareness is on, sir — I glance about every "
            f"{interval_seconds() / 60:.0f} minutes and stay quiet unless there's something "
            f"I can help with.{extra}")


def screen_awareness(parameters: dict | None = None, response=None, player=None,
                     session_memory=None, speak=None, jarvis=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "") or "").strip().lower()
    value = str(params.get("value", "") or params.get("app_name", "") or "").strip()

    if action in ("start", "enable", "on", "engage"):
        set_enabled(True)
        return ("Ambient screen awareness engaged, sir. I'll glance at your screen every "
                f"{interval_seconds() / 60:.0f} minutes and only speak up if I can help. "
                "I won't look while a password manager, banking page or login screen is in front, "
                "nor while the biometric lock is engaged.")

    if action in ("stop", "disable", "off", "disengage"):
        set_enabled(False)
        return "Ambient screen awareness disengaged, sir. I've stopped looking at your screen."

    if action in ("block", "never_look", "exclude"):
        if not value:
            return "Which application should I never look at, sir?"
        return block_app(value)

    if action in ("interval", "set_interval"):
        try:
            seconds = max(MIN_INTERVAL_SECONDS, float(value) * 60)
        except Exception:
            return "How many minutes between glances, sir?"
        state = _load_state()
        state["interval_seconds"] = seconds
        _save_state(state)
        return f"I'll glance every {seconds / 60:.0f} minutes, sir."

    return status_text()
