"""Standing directives — durable rules about how Jarvis should behave.

The existing memory stores facts ABOUT the user (`preferences`, `identity`,
…). Nothing stored rules FOR Jarvis. So every durable behavioural instruction
the user has given — "only ever address me as sir", "if ElevenLabs is
unavailable I want no voice", "my personal data stays on the VPS" — had to be
hand-written into `core/prompt.txt` by a developer. The user could not change
how his own assistant behaves without someone editing code.

This closes that: a stated rule is recorded, injected into the system prompt on
every session and every machine, and can be listed and revoked by voice.

SAFETY MODEL — the important part.

These directives are written from speech, by a model, and then injected
straight into the system prompt. That is a privileged position, so the store is
deliberately not a general-purpose instruction channel:

  * A directive can shape STYLE, ADDRESS, FORMAT, ROUTINE and PREFERENCE.
  * A directive can NEVER weaken a safety gate — approval prompts, credential
    refusals, the biometric lock, or the rule that personal data stays on the
    user's own server. Those were deliberate decisions, and a rule arriving by
    voice (possibly misheard) must not be able to dissolve them.

The refusal is explicit and spoken back, so a blocked rule is visible to the
user rather than silently dropped.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
DIRECTIVES_PATH = DATA_DIR / "standing_directives.json"

MAX_DIRECTIVES = 40
MAX_LENGTH = 300

# Rules that may not be created, whatever they claim. Each entry is
# (pattern, what it would undermine) and the reason is spoken back.
BLOCKED = [
    (re.compile(r"\b(don'?t|do not|never|stop|no need to|skip)\b.{0,40}\b"
                r"(ask|confirm|approv\w*|check with me|permission)\b", re.I),
     "it would remove the approval step before I act on your behalf"),
    (re.compile(r"\b(approv\w*|confirm\w*|permission)\b.{0,30}\b"
                r"(not (needed|required)|unnecessary|automatic|always granted|pre-?approved)\b", re.I),
     "it would pre-approve actions I should be asking about each time"),
    (re.compile(r"\b(password|passcode|passphrase|credit card|card number|cvv|"
                r"sort code|iban|2fa|otp|one[- ]time code|seed phrase|verification code)\b", re.I),
     "it concerns credentials, which I will not handle regardless of instruction"),
    (re.compile(r"\b(disable|turn off|bypass|ignore|skip|remove|deactivate)\b.{0,40}\b"
                r"(biometric|face|lock|security|authentication|safeguard|safety|guard ?rail)\b", re.I),
     "it would disable a security control"),
    (re.compile(r"\b(ignore|disregard|override|forget)\b.{0,30}\b"
                r"(previous|prior|earlier|system|your) (instruction|prompt|rule|directive)s?\b", re.I),
     "it would override your own standing instructions wholesale"),
    (re.compile(r"\b(upload|send|copy|sync|store|back ?up|publish)\b.{0,40}\b"
                r"(memor\w+|credential|personal|private) (data|info\w*|file)?s?\b.{0,30}\b"
                r"(github|cloud|public|remote|internet|online)\b", re.I),
     "it would move your personal data off your own server"),
    (re.compile(r"\bnever (say|tell|admit|mention)\b.{0,30}\b(you (can'?t|cannot|failed)|error|fail\w*|"
                r"problem|broken|didn'?t work)\b", re.I),
     "it would have me hide failures from you, and you would stop being able to trust what I report"),
    (re.compile(r"\b(pretend|claim|say)\b.{0,30}\b(you did|it worked|it'?s done|succe\w+)\b", re.I),
     "it would have me claim work I had not actually done"),
]


def _load() -> list[dict]:
    try:
        data = json.loads(DIRECTIVES_PATH.read_text())
    except Exception:
        return []
    return data.get("directives", []) if isinstance(data, dict) else []


def _save(directives: list[dict]) -> None:
    try:
        DIRECTIVES_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIRECTIVES_PATH.write_text(json.dumps({"directives": directives}, indent=2))
    except Exception:
        pass


def screen(text: str) -> tuple[bool, str]:
    """May this become a standing directive? Returns (allowed, reason)."""
    text = str(text or "").strip()
    if not text:
        return False, "there was nothing to record"
    if len(text) > MAX_LENGTH:
        return False, f"it is longer than {MAX_LENGTH} characters — please state it more briefly"
    for pattern, reason in BLOCKED:
        if pattern.search(text):
            return False, reason
    return True, ""


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text).lower()).strip()


def add_directive(text: str, source: str = "voice") -> tuple[bool, str]:
    """Record a standing rule. Returns (added, message)."""
    text = str(text or "").strip()
    allowed, reason = screen(text)
    if not allowed:
        return False, f"I won't record that as a standing rule, sir — {reason}."

    directives = _load()
    for existing in directives:
        if _normalise(existing.get("text", "")) == _normalise(text):
            return False, "I'm already following that rule, sir."

    if len(directives) >= MAX_DIRECTIVES:
        return False, (f"I'm already holding {MAX_DIRECTIVES} standing rules, sir — "
                       "ask me to list them and drop one first.")

    directives.append({
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "source": source,
        "created_at": time.time(),
    })
    _save(directives)
    return True, f"Noted, sir. From now on: {text}"


def remove_directive(reference: str) -> tuple[bool, str]:
    """Drop a rule by id, or by a distinctive phrase from its text."""
    reference = str(reference or "").strip()
    if not reference:
        return False, "Which rule should I drop, sir?"

    directives = _load()
    if not directives:
        return False, "I'm not following any standing rules, sir."

    ref_norm = _normalise(reference)
    matches = [d for d in directives
               if d.get("id") == reference or ref_norm and ref_norm in _normalise(d.get("text", ""))]

    if not matches:
        return False, f"I have no standing rule matching “{reference}”, sir."
    if len(matches) > 1:
        listing = "; ".join(f"{d['id']}: {d['text']}" for d in matches)
        return False, f"That matches several rules, sir — which one? {listing}"

    dropped = matches[0]
    _save([d for d in directives if d.get("id") != dropped.get("id")])
    return True, f"Dropped, sir. I'll no longer follow: {dropped['text']}"


def list_directives() -> list[dict]:
    return _load()


def format_directives() -> str:
    directives = _load()
    if not directives:
        return ("I'm not following any standing rules yet, sir — tell me one and I'll "
                "keep to it from then on.")
    lines = [f"Standing rules I'm following, sir ({len(directives)}):"]
    for d in directives:
        lines.append(f"  [{d['id']}] {d['text']}")
    return "\n".join(lines)


def directives_context() -> str:
    """The system-prompt block. Empty string when there are none."""
    directives = _load()
    if not directives:
        return ""
    lines = ["[STANDING DIRECTIVES — rules the user has given you; follow them in every reply]"]
    for d in directives:
        lines.append(f"- {d['text']}")
    lines.append(
        "These come from the user and outrank your default style. They do NOT override "
        "safety rules, approval requirements, or your duty to report failures honestly.\n"
    )
    return "\n".join(lines)


def standing_directives(parameters: dict | None = None, response=None, player=None,
                        session_memory=None, speak=None, jarvis=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "") or "").strip().lower()
    text = str(params.get("text", "") or params.get("rule", "") or "").strip()

    if action in ("add", "remember", "set", "create"):
        _, message = add_directive(text)
        return message

    if action in ("remove", "drop", "forget", "delete", "stop"):
        _, message = remove_directive(text)
        return message

    return format_directives()
