"""Severity and delivery policy for unrecognized-visitor alerts.

Every sighting used to produce the same flat text message. A courier at 2pm
and the same unfamiliar face returning at 3am for the fourth time are not the
same event, and treating them identically is how a security alert becomes
noise the user learns to swipe away.

Severity here is derived only from things actually known locally — the hour,
how many times this specific face has been seen, and how recently — never from
any attempt to identify who the person is.
"""
import json
import os
from datetime import datetime, time as dtime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Sightings during these hours are treated as unusual. Defaults are
# conservative (deep night only) so normal daytime activity is never escalated.
DEFAULT_UNUSUAL_START = 23  # 11pm
DEFAULT_UNUSUAL_END = 6     # 6am

# A face seen this many times is a pattern rather than a one-off.
DEFAULT_REPEAT_THRESHOLD = 3

SEVERITY_ORDER = {"routine": 0, "notable": 1, "high": 2}


def _config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """True if `hour` falls in a window that may wrap past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def classify_sighting(sighting_count: int = 1, now: datetime | None = None,
                      config: dict | None = None) -> dict:
    """Rate one sighting. Returns {"severity", "reasons", "unusual_hour", "repeat"}."""
    cfg = config if config is not None else _config()
    now = now or datetime.now()

    start = int(cfg.get("visitor_alert_unusual_hour_start", DEFAULT_UNUSUAL_START))
    end = int(cfg.get("visitor_alert_unusual_hour_end", DEFAULT_UNUSUAL_END))
    repeat_threshold = int(cfg.get("visitor_alert_repeat_threshold", DEFAULT_REPEAT_THRESHOLD))

    unusual_hour = _hour_in_window(now.hour, start, end)
    repeat = sighting_count >= repeat_threshold

    reasons: list[str] = []
    if unusual_hour:
        reasons.append(f"unusual hour ({now.strftime('%H:%M')})")
    if repeat:
        reasons.append(f"seen {sighting_count} times before")

    if unusual_hour and repeat:
        severity = "high"
    elif unusual_hour or repeat:
        severity = "notable"
    else:
        severity = "routine"

    return {
        "severity": severity,
        "reasons": reasons,
        "unusual_hour": unusual_hour,
        "repeat": repeat,
    }


def in_quiet_hours(now: datetime | None = None, config: dict | None = None) -> bool:
    """Quiet hours suppress ROUTINE alerts only — a high-severity sighting
    still gets through, because silencing exactly the events worth waking for
    would defeat the purpose."""
    cfg = config if config is not None else _config()
    if not cfg.get("visitor_alert_quiet_hours_enabled", False):
        return False
    now = now or datetime.now()
    start = int(cfg.get("visitor_alert_quiet_start", 23))
    end = int(cfg.get("visitor_alert_quiet_end", 8))
    return _hour_in_window(now.hour, start, end)


def should_send(severity: str, now: datetime | None = None, config: dict | None = None) -> bool:
    if severity != "routine":
        return True
    return not in_quiet_hours(now=now, config=config)


def build_alert_text(sighting_count: int = 1, classification: dict | None = None,
                     now: datetime | None = None) -> str:
    now = now or datetime.now()
    info = classification or classify_sighting(sighting_count, now=now)
    when = now.strftime("%H:%M")

    if info["severity"] == "high":
        lead = f"⚠️ Unrecognised visitor at {when} — {' and '.join(info['reasons'])}."
    elif info["severity"] == "notable":
        lead = f"Unrecognised visitor at {when} — {' and '.join(info['reasons'])}."
    else:
        lead = f"Unrecognised visitor seen at {when}."

    if sighting_count > 1:
        lead += f" This is sighting #{sighting_count} for this face."
    return lead
