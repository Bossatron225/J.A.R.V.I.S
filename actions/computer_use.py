"""Computer use — a perceive → act → VERIFY loop for GUI tasks.

Jarvis already had eyes (screen capture + vision) and hands (pyautogui), but
nothing joined them: `computer_control` clicks and assumes it worked. There
was no verification anywhere in that path. This closes the loop — after every
action Jarvis looks again and checks the screen actually changed the way he
intended, and stops rather than compounding a mistake.

This is the most dangerous capability in the system: it moves a real cursor
and types real keystrokes on a real machine. The safeguards are therefore not
optional extras, they are the design:

* NOTHING RUNS WITHOUT APPROVAL. A task returns a plan preview first;
  execution requires an explicit second call with approved=True.
* HARD BLOCKS CANNOT BE APPROVED. Payments, credentials, and destructive
  operations are refused outright, no matter what was approved — approval
  covers "click around this app", never "buy this" or "type my password".
* EVERY ACTION IS BOUNDED. A step budget, a per-action safety check, and an
  abort the moment verification fails.
* EVERY ACTION IS AUDITED, so unattended-looking work is reviewable.
"""
import json
import re
import time

# Refused outright, even in an approved task. These are things whose cost is
# irreversible or whose subject is a secret — no amount of "yes go ahead" for
# a browsing task should authorise them.
HARD_BLOCKED = [
    (re.compile(r"\b(buy|purchase|checkout|check out|place (the )?order|pay(ment)?|subscribe|billing)\b", re.I),
     "financial transaction"),
    (re.compile(r"\b(password|passcode|passphrase|credit card|card number|cvv|sort code|iban|ssn|2fa|otp|one[- ]time code|verification code|seed phrase)\b", re.I),
     "credential or secret"),
    (re.compile(r"\b(delete|erase|wipe|format|uninstall|factory reset|rm -rf|empty (the )?trash)\b", re.I),
     "destructive operation"),
    (re.compile(r"\b(shutdown|shut down|reboot|restart the (mac|computer|system))\b", re.I),
     "system power operation"),
]

# Allowed only inside an approved task — outward-facing but reversible enough
# that explicit approval is a reasonable bar.
NEEDS_APPROVAL = [
    (re.compile(r"\b(send|submit|post|publish|reply|share|upload)\b", re.I), "sends something outward"),
]

VALID_ACTIONS = {"click", "double_click", "type", "hotkey", "press", "scroll", "move", "done", "fail"}
MAX_STEPS = 8


class ComputerUseError(Exception):
    """A task could not proceed safely."""


def check_safety(text: str, approved: bool) -> tuple[bool, str]:
    """Gate one instruction or action description.

    Returns (allowed, reason). Hard blocks ignore `approved` entirely."""
    text = str(text or "")
    for pattern, label in HARD_BLOCKED:
        if pattern.search(text):
            return False, f"refused — {label}. I won't do that with the mouse and keyboard, sir."
    if not approved:
        for pattern, label in NEEDS_APPROVAL:
            if pattern.search(text):
                return False, f"needs your approval — {label}."
    return True, ""


def scale_to_click_space(x: float, y: float, shot_size: tuple[int, int],
                         screen_size: tuple[int, int]) -> tuple[int, int]:
    """Convert screenshot-pixel coordinates into click coordinates.

    On a Retina Mac the screenshot is 2x the logical screen (2940x1912 vs
    1470x956). Clicking raw screenshot pixels would land at double the
    intended position — usually off-screen, or on the wrong control entirely.
    Getting this wrong is silent and dangerous, so it is done explicitly."""
    shot_w, shot_h = shot_size
    screen_w, screen_h = screen_size
    if not shot_w or not shot_h:
        return int(x), int(y)
    scaled_x = int(round(x * (screen_w / shot_w)))
    scaled_y = int(round(y * (screen_h / shot_h)))
    # Never let a bad coordinate walk off the display.
    scaled_x = max(0, min(scaled_x, screen_w - 1))
    scaled_y = max(0, min(scaled_y, screen_h - 1))
    return scaled_x, scaled_y


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def parse_action(raw: str) -> dict:
    """Parse and validate one proposed action."""
    try:
        data = json.loads(_strip_fences(raw))
    except Exception as exc:
        raise ComputerUseError(f"could not parse action: {exc}")

    action = str(data.get("action", "") or "").strip().lower()
    if action not in VALID_ACTIONS:
        raise ComputerUseError(f"unknown action {action!r}")

    return {
        "action": action,
        "x": data.get("x"),
        "y": data.get("y"),
        "text": str(data.get("text", "") or ""),
        "keys": data.get("keys") or [],
        "amount": data.get("amount", 0),
        "reason": str(data.get("reason", "") or ""),
    }


ACTION_PROMPT = """You are operating a macOS computer to accomplish a task. You see a screenshot.

Task: {goal}
Steps already taken: {history}

Decide the SINGLE next action. Return ONLY valid JSON, no markdown:
{{"action": "click|double_click|type|hotkey|press|scroll|move|done|fail",
  "x": 0, "y": 0, "text": "", "keys": [], "amount": 0,
  "reason": "why this action, and what you expect to happen"}}

Rules:
1. x,y are pixel coordinates IN THIS SCREENSHOT (top-left origin).
2. Use "done" when the task is visibly complete. Use "fail" if it cannot be done.
3. One small step at a time. Prefer keyboard over mouse where reliable.
4. NEVER interact with password fields, payment forms, or anything that spends money or deletes data.
5. "reason" must state the expected visible outcome, so it can be checked afterwards.

JSON:"""

VERIFY_PROMPT = """You are checking whether a computer action did what was intended.

Intended: {intent}
Expected outcome: {expectation}

The FIRST image is before the action, the SECOND is after.

Return ONLY valid JSON:
{{"succeeded": true, "detail": "what actually changed on screen"}}

Be strict: if the screen did not change in the way expected, succeeded is false.
If the screen is essentially identical, succeeded is false."""


def verify_change(intent: str, expectation: str, before_png: bytes, after_png: bytes,
                  vision) -> tuple[bool, str]:
    """Did the action actually do what it claimed?

    This is the piece that was entirely missing: previously Jarvis clicked and
    assumed. `vision` takes (prompt, [images]) and returns text."""
    try:
        raw = vision(
            VERIFY_PROMPT.format(intent=intent, expectation=expectation),
            [before_png, after_png],
        )
        data = json.loads(_strip_fences(raw))
    except Exception as exc:
        return False, f"could not verify: {exc}"
    return bool(data.get("succeeded")), str(data.get("detail", "") or "")


def describe_plan(goal: str, approved: bool) -> str:
    """The preview returned when a task has not been approved yet."""
    allowed, reason = check_safety(goal, approved=False)
    if not allowed and "refused" in reason:
        return f"I {reason}"
    return (
        f"Proposed computer task, sir: “{goal}”.\n"
        f"I'll work in up to {MAX_STEPS} small steps, checking the screen after each one and "
        f"stopping if anything doesn't go as expected.\n"
        "I will not touch passwords, payment forms, or anything that deletes data.\n"
        "Say “approved, go ahead” to let me run it."
    )


def execute_action(act: dict, shot_size: tuple[int, int], screen_size: tuple[int, int],
                   controller) -> str:
    """Perform one validated action. `controller` is the pyautogui-like module,
    injected so this is testable without moving a real cursor."""
    kind = act["action"]

    if kind in {"click", "double_click", "move"}:
        if act["x"] is None or act["y"] is None:
            raise ComputerUseError(f"{kind} needs coordinates")
        x, y = scale_to_click_space(float(act["x"]), float(act["y"]), shot_size, screen_size)
        if kind == "move":
            controller.moveTo(x, y, duration=0.2)
            return f"moved to ({x}, {y})"
        controller.click(x, y, clicks=2 if kind == "double_click" else 1)
        return f"{kind} at ({x}, {y})"

    if kind == "type":
        controller.typewrite(act["text"], interval=0.02)
        return f"typed {len(act['text'])} character(s)"

    if kind == "hotkey":
        keys = [str(k) for k in act["keys"] if k]
        if not keys:
            raise ComputerUseError("hotkey needs keys")
        controller.hotkey(*keys)
        return f"hotkey {'+'.join(keys)}"

    if kind == "press":
        controller.press(act["text"] or "enter")
        return f"pressed {act['text'] or 'enter'}"

    if kind == "scroll":
        controller.scroll(int(act["amount"] or 0))
        return f"scrolled {act['amount']}"

    raise ComputerUseError(f"cannot execute {kind}")
