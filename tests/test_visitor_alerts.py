import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import visitor_alerts as va

MIDDAY = datetime(2026, 8, 22, 14, 15)
NIGHT = datetime(2026, 8, 22, 3, 15)
CFG = {}


def test_one_off_daytime_sighting_is_routine():
    assert va.classify_sighting(1, now=MIDDAY, config=CFG)["severity"] == "routine"


def test_repeat_daytime_sighting_is_notable():
    info = va.classify_sighting(5, now=MIDDAY, config=CFG)
    assert info["severity"] == "notable"
    assert info["repeat"] is True


def test_one_off_night_sighting_is_notable():
    info = va.classify_sighting(1, now=NIGHT, config=CFG)
    assert info["severity"] == "notable"
    assert info["unusual_hour"] is True


def test_repeat_night_sighting_escalates_to_high():
    """A courier at 2pm and the same face returning at 3am for the fifth time
    must not produce the same notification."""
    info = va.classify_sighting(5, now=NIGHT, config=CFG)
    assert info["severity"] == "high"
    assert len(info["reasons"]) == 2


def test_unusual_hour_window_wraps_past_midnight():
    """23:00-06:00 spans midnight; naive range logic would treat 03:00 as normal."""
    assert va._hour_in_window(3, 23, 6) is True
    assert va._hour_in_window(23, 23, 6) is True
    assert va._hour_in_window(14, 23, 6) is False


def test_alert_text_reflects_severity():
    high = va.build_alert_text(5, va.classify_sighting(5, now=NIGHT, config=CFG), now=NIGHT)
    routine = va.build_alert_text(1, va.classify_sighting(1, now=MIDDAY, config=CFG), now=MIDDAY)
    assert "⚠️" in high and "unusual hour" in high
    assert "⚠️" not in routine


def test_quiet_hours_suppress_routine_alerts():
    cfg = {"visitor_alert_quiet_hours_enabled": True, "visitor_alert_quiet_start": 23, "visitor_alert_quiet_end": 8}
    assert va.should_send("routine", now=NIGHT, config=cfg) is False


def test_quiet_hours_never_suppress_high_severity():
    """Silencing exactly the events worth waking for would defeat the purpose."""
    cfg = {"visitor_alert_quiet_hours_enabled": True, "visitor_alert_quiet_start": 23, "visitor_alert_quiet_end": 8}
    assert va.should_send("high", now=NIGHT, config=cfg) is True
    assert va.should_send("notable", now=NIGHT, config=cfg) is True


def test_quiet_hours_off_by_default():
    assert va.should_send("routine", now=NIGHT, config={}) is True


def test_thresholds_are_configurable():
    cfg = {"visitor_alert_repeat_threshold": 2}
    assert va.classify_sighting(2, now=MIDDAY, config=cfg)["repeat"] is True
    assert va.classify_sighting(2, now=MIDDAY, config={})["repeat"] is False
