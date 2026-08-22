#computer_control.py
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except Exception:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except Exception:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _extract_text(response) -> str:
    if response is None:
        return ""

    if hasattr(response, "text"):
        text = getattr(response, "text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    parts: list[str] = []
    for attr in ("candidates", "contents"):
        items = getattr(response, attr, None)
        if not items:
            continue
        for item in items:
            content = getattr(item, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", []) or []:
                if hasattr(part, "text") and getattr(part, "text"):
                    parts.append(getattr(part, "text"))
                elif isinstance(part, str):
                    parts.append(part)
    if parts:
        return "".join(parts).strip()

    if hasattr(response, "parts"):
        for part in response.parts:
            if hasattr(part, "text") and getattr(part, "text"):
                parts.append(getattr(part, "text"))
            elif isinstance(part, str):
                parts.append(part)
    return "".join(parts).strip()


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "jarvis_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        x, y = int(x), int(y)
        pyautogui.click(x, y, button=button, clicks=clicks)
        # A click cannot be observed directly, but the pointer landing where it
        # was aimed is a necessary condition — and its absence is exactly what a
        # missing Accessibility grant looks like.
        try:
            landed = pyautogui.position()
            if abs(landed[0] - x) > 2 or abs(landed[1] - y) > 2:
                return (f"Could not click ({x}, {y}) — the pointer never moved there "
                        f"(it is at {landed[0]}, {landed[1]}). Jarvis likely lacks Accessibility "
                        f"permission (System Settings → Privacy & Security → Accessibility).")
        except Exception:
            pass
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    """Move the pointer, then CHECK it arrived.

    Reading the position back catches the failure mode that made this look
    broken: if macOS has not granted this process Accessibility permission,
    pyautogui's move is accepted and does nothing at all, and without a
    read-back there is nothing to distinguish that from success."""
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    time.sleep(0.05)
    try:
        landed = pyautogui.position()
    except Exception:
        return f"Mouse → ({x}, {y})"

    if abs(landed[0] - x) > 2 or abs(landed[1] - y) > 2:
        return (f"Could not move the mouse — asked for ({x}, {y}) but the pointer is at "
                f"({landed[0]}, {landed[1]}). Jarvis likely lacks Accessibility permission "
                f"(System Settings → Privacy & Security → Accessibility).")
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    os_name = _get_os()

    if os_name == "windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=5, **_WIN_HIDE,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        # Prefer the real app name from the window server: the user says
        # "Claude code", the process is "Code", and a `name contains` match on
        # the spoken phrase finds nothing.
        window = find_window(title)
        target = window["owner"] if window else title
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name is "{target}") to true'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip().splitlines()
                return (f"I couldn't bring '{title}' to the front, sir"
                        + (f" — {detail[-1]}" if detail else "."))
            time.sleep(0.4)
            # Verify rather than assume: this used to report success even when
            # the process did not exist.
            check = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first '
                 'application process whose frontmost is true'],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if check and check.lower() != target.lower():
                return (f"I asked for '{target}' to come forward, sir, but '{check}' is still "
                        f"in front.")
            return f"Focused window: {target}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                time.sleep(0.3)
                return f"Focused window: {title}"
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title, "windowactivate"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except FileNotFoundError:
            return "focus_window (Linux) requires wmctrl or xdotool"
        except Exception as e:
            return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"

def list_windows() -> list[dict]:
    """On-screen windows with their real geometry, via the macOS window server.

    Coordinates come back already in click space (the same 1470x956 that
    pyautogui uses), so nothing needs scaling and nothing needs guessing. This
    exists because asking a vision model for pixel coordinates is not reliable
    enough to click with: asked to locate the clock in the top-RIGHT of a
    2940px-wide screenshot, it answered x=959. The window server simply knows."""
    if _get_os() != "mac":
        return []
    try:
        import Quartz
    except Exception:
        return []

    try:
        raw = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception:
        return []

    windows: list[dict] = []
    for entry in raw or []:
        bounds = entry.get("kCGWindowBounds") or {}
        width, height = float(bounds.get("Width", 0)), float(bounds.get("Height", 0))
        # Skip menu-bar strips, tiny helper panels and other non-targets.
        if width < 120 or height < 80:
            continue
        owner = str(entry.get("kCGWindowOwnerName") or "")
        if owner in {"Window Server", "Notification Centre", "Notification Center", "Dock"}:
            continue
        windows.append({
            "owner": owner,
            "title": str(entry.get("kCGWindowName") or ""),
            "x": float(bounds.get("X", 0)), "y": float(bounds.get("Y", 0)),
            "width": width, "height": height,
        })
    return windows


def find_window(query: str) -> dict | None:
    """Best on-screen window matching an app name or window title."""
    query = str(query or "").strip().lower()
    if not query:
        return None

    windows = list_windows()
    if not windows:
        return None

    # Most specific first: exact app name, then app-name substring, then title.
    for match in (
        lambda w: w["owner"].lower() == query,
        lambda w: query in w["owner"].lower(),
        lambda w: w["owner"].lower() in query,
        lambda w: query in w["title"].lower(),
    ):
        found = [w for w in windows if match(w)]
        if found:
            # Largest match — the main window rather than a palette.
            return max(found, key=lambda w: w["width"] * w["height"])
    return None


def window_centre(window: dict) -> tuple[int, int]:
    return (int(window["x"] + window["width"] / 2),
            int(window["y"] + window["height"] / 2))


def _resolve_point(description: str) -> tuple[tuple[int, int] | None, str]:
    """Where is the thing the user described? Returns (point, how_it_was_found).

    Real window geometry is tried first because it is exact; the vision search
    is only a fallback for things that are not windows (a button, an icon), and
    is inherently approximate."""
    window = find_window(description)
    if window:
        label = window["title"] or window["owner"]
        return window_centre(window), f"the {label} window"
    point = _screen_find(description)
    if point:
        return point, f"'{description}' (located visually — approximate)"
    return None, ""


def _screen_find(description: str) -> tuple[int, int] | None:
    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None

    try:
        from google import genai
        from google.genai import types as gtypes

        _require_pyautogui()
        img   = pyautogui.screenshot()
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        client = genai.Client(api_key=api_key)
        # The prompt MUST describe the image actually sent. On a Retina Mac the
        # screenshot is 2940x1912 while pyautogui.size() reports 1470x956; this
        # used to quote the latter while sending the former, so the coordinates
        # coming back were meaningless — which is why clicks landed nowhere near
        # the thing they were aimed at.
        iw, ih = img.size
        prompt = (
            f"This is a screenshot measuring {iw}×{ih} pixels. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"in pixels of THIS image, with 0,0 at the top-left. "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        response = client.models.generate_content(
            model="models/gemini-flash-lite-latest",
            contents=[
                gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
        )

        text = _extract_text(response)
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            # Screenshot pixels are not click coordinates on a Retina display.
            # Clicking raw image pixels lands at double the intended position —
            # silently, and usually on the wrong control entirely.
            from actions.computer_use import scale_to_click_space
            return scale_to_click_space(
                int(match.group(1)), int(match.group(2)),
                shot_size=img.size, screen_size=tuple(pyautogui.size()),
            )

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None

def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            # Never default missing coordinates to (0, 0). "Move the mouse to
            # the Claude window" arrives with no x/y, and silently parking the
            # pointer in the top-left corner while reporting "Mouse → (0, 0)"
            # is worse than saying what is missing. If a description is given,
            # find it on screen the same way screen_click does.
            x_raw, y_raw = params.get("x"), params.get("y")
            if x_raw is None or y_raw is None:
                desc = str(params.get("description") or params.get("target")
                           or params.get("title") or "").strip()
                if not desc:
                    return ("I need either coordinates or a description of what to move to, sir — "
                            "no x/y was given.")
                coords, how = _resolve_point(desc)
                if not coords:
                    return f"I couldn't find '{desc}' on the screen, sir, so I haven't moved the mouse."
                outcome = _move(coords[0], coords[1])
                return outcome if outcome.startswith("Could not") else f"{outcome} — {how}"
            return _move(int(x_raw), int(y_raw))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc = str(params.get("description") or params.get("target") or "").strip()
            coords, how = _resolve_point(desc)
            if not coords:
                return f"I couldn't find '{desc}' on the screen, sir, so I haven't clicked anything."
            time.sleep(0.2)
            # Report what _click actually observed rather than assuming.
            outcome = _click(x=coords[0], y=coords[1])
            if outcome.startswith("Could not"):
                return outcome
            return f"Clicked {how} at {coords}"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"