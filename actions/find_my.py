import json
import platform
import shutil
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def _read_config() -> dict:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _open_find_my() -> str:
    candidates = [
        ["open", "-a", "Find My"],
        ["open", "-a", "FindMy"],
        ["open", "/System/Applications/FindMy.app"],
        ["open", "-b", "com.apple.findmy"],
    ]
    last_err = "Unknown error"
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        except Exception as e:
            last_err = str(e)
            continue
        if proc.returncode == 0:
            return "Opened Find My."
        last_err = (proc.stderr or proc.stdout or last_err).strip()
    return f"Could not open Find My: {last_err}"


def _open_icloud_find() -> str:
    proc = subprocess.run(["open", "https://www.icloud.com/find"], capture_output=True, text=True, timeout=12)
    if proc.returncode == 0:
        return "Opened iCloud Find in your browser."
    err = (proc.stderr or proc.stdout or "Unknown error").strip()
    return f"Could not open iCloud Find: {err}"


def _run_shortcut(name: str, input_text: str = "") -> tuple[bool, str]:
    if not name:
        return False, "Shortcut name is empty."
    if shutil.which("shortcuts") is None:
        return False, "macOS Shortcuts CLI is unavailable."

    try:
        cmd = ["shortcuts", "run", name]
        proc = subprocess.run(
            cmd,
            input=(input_text or ""),
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception as e:
        return False, f"Shortcut run failed: {e}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        msg = err or out or "Unknown shortcuts error"
        return False, msg
    return True, (out or "Shortcut completed with no text output.")


def _shortcut_key_for_scope(scope: str) -> str:
    scope = (scope or "").strip().lower()
    if scope == "people":
        return "find_my_people_shortcut"
    if scope == "devices":
        return "find_my_devices_shortcut"
    return "find_my_all_shortcut"


def find_my(parameters: dict, player=None) -> str:
    if not _is_mac():
        return "Find My integration is only available on macOS."

    params = parameters or {}
    action = str(params.get("action", "status") or "status").strip().lower()
    scope = str(params.get("scope", "all") or "all").strip().lower()
    target = str(params.get("target", "") or "").strip()

    if action == "open":
        result = _open_find_my()
        if player:
            player.write_log(f"[find_my] {result}")
        return result

    if action in {"web", "web_open", "open_web"}:
        result = _open_icloud_find()
        if player:
            player.write_log(f"[find_my] {result}")
        return result

    cfg = _read_config()
    default_shortcuts = {
        "find_my_people_shortcut": "Jarvis Find My People",
        "find_my_devices_shortcut": "Jarvis Find My Devices",
        "find_my_all_shortcut": "Jarvis Find My Snapshot",
        "find_my_locate_shortcut": "Jarvis Find My Locate",
    }
    shortcuts_cfg = {k: str(cfg.get(k, v) or "").strip() for k, v in default_shortcuts.items()}

    if action in {"setup", "help"}:
        return (
            "Find My setup on this Mac:\n"
            "1) Ensure your Apple ID has Find My enabled and any people are shared with you.\n"
            "2) If your macOS Shortcuts has Find My actions, create these shortcuts (exact names or set custom names in config/api_keys.json):\n"
            f"   - {shortcuts_cfg['find_my_people_shortcut']} (outputs shared people + locations)\n"
            f"   - {shortcuts_cfg['find_my_devices_shortcut']} (outputs your devices + locations)\n"
            f"   - {shortcuts_cfg['find_my_all_shortcut']} (outputs both people and devices)\n"
            f"   - {shortcuts_cfg['find_my_locate_shortcut']} (accepts a name via stdin and outputs best match location)\n"
            "3) If Find My actions are NOT available in Shortcuts on your macOS build, use fallback mode:\n"
            "   - action=open (opens Find My app)\n"
            "   - action=web_open (opens iCloud Find in browser)\n"
            "   - then ask Jarvis to read your screen for locations.\n"
            "4) In Shortcuts privacy prompts, allow location/data access when requested.\n"
            "5) Optional CLI test: shortcuts run \"Jarvis Find My Snapshot\""
        )

    if action in {"status", "people", "devices", "all"}:
        if action == "status":
            shortcut_name = shortcuts_cfg[_shortcut_key_for_scope(scope)]
            if scope not in {"people", "devices", "all"}:
                scope = "all"
        elif action == "people":
            scope = "people"
            shortcut_name = shortcuts_cfg["find_my_people_shortcut"]
        elif action == "devices":
            scope = "devices"
            shortcut_name = shortcuts_cfg["find_my_devices_shortcut"]
        else:
            scope = "all"
            shortcut_name = shortcuts_cfg["find_my_all_shortcut"]

        ok, out = _run_shortcut(shortcut_name)
        if not ok:
            # Common case: shortcut not created or no Find My actions available.
            fallback_open = _open_find_my()
            fallback_web = _open_icloud_find()
            return (
                f"Find My {scope} lookup failed via shortcut '{shortcut_name}'. {out} "
                f"Fallback: {fallback_open} {fallback_web} "
                "Please open Locations and then ask Jarvis to read the screen for device/person locations."
            )
        return out

    if action in {"locate", "where"}:
        if not target:
            return "Please provide target name (person or device), for example: target='James iPhone'."
        shortcut_name = shortcuts_cfg["find_my_locate_shortcut"]
        ok, out = _run_shortcut(shortcut_name, input_text=target)
        if not ok:
            fallback_open = _open_find_my()
            fallback_web = _open_icloud_find()
            return (
                f"Find My locate failed via shortcut '{shortcut_name}'. {out} "
                f"Fallback: {fallback_open} {fallback_web} "
                f"Open the target in Find My and ask Jarvis to read the screen for '{target}'."
            )
        return out

    return "Unknown find_my action. Use: open | web_open | open_and_read | status | people | devices | all | locate | setup"
