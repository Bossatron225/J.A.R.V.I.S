import json
from pathlib import Path

from actions.imessage_integration import send_imessage

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def notify_user(message: str) -> str:
    """Text the user out-of-band (via the dedicated Jarvis iMessage identity)
    about something Jarvis did or noticed. No-ops quietly if disabled or
    unconfigured — this is a best-effort side channel, not a critical path."""
    message = (message or "").strip()
    if not message:
        return "No notification message provided."

    config = _load_config()
    if not config.get("jarvis_notify_enabled", True):
        return "Notifications are disabled (jarvis_notify_enabled=false)."

    target = str(config.get("jarvis_notify_target") or "").strip()
    if not target:
        return "No jarvis_notify_target configured in config/api_keys.json."

    return send_imessage(target, message)
