import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import offline_mode as om


@pytest.fixture(autouse=True)
def _reset_probe_cache(monkeypatch):
    monkeypatch.setattr(om, "_last_probe_ts", 0.0)
    monkeypatch.setattr(om, "_last_probe_ok", True)


# ── reachability probe ─────────────────────────────────────────────────────

def test_http_error_still_counts_as_reachable(monkeypatch):
    """We are testing the network path, not authorisation — a 403 from Google
    means the connection is fine."""
    import requests

    def _raise(*a, **k):
        raise requests.exceptions.HTTPError("403")

    monkeypatch.setattr(om.requests if hasattr(om, "requests") else requests, "get", _raise, raising=False)
    monkeypatch.setattr(om.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(om.urllib.error.HTTPError("u", 403, "no", None, None)))
    assert om.cloud_reachable(force=True) is True


def test_connection_failure_reports_unreachable(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(om.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert om.cloud_reachable(force=True) is False


def test_probe_result_is_cached(monkeypatch):
    calls = {"n": 0}
    import requests

    def _count(*a, **k):
        calls["n"] += 1
        class _R: status_code = 200
        return _R()

    monkeypatch.setattr(requests, "get", _count)
    om.cloud_reachable(force=True)
    om.cloud_reachable()
    om.cloud_reachable()
    assert calls["n"] == 1  # subsequent calls served from cache


def test_degraded_status_wording(monkeypatch):
    monkeypatch.setattr(om, "cloud_reachable", lambda *a, **k: False)
    assert "DEGRADED" in om.degraded_status()
    monkeypatch.setattr(om, "cloud_reachable", lambda *a, **k: True)
    assert "Online" in om.degraded_status()


# ── local intents ──────────────────────────────────────────────────────────

def test_time_is_answered_locally():
    answer = om.handle_locally("what time is it")
    assert "sir" in answer
    assert om.OFFLINE_REFUSAL not in answer


def test_visitor_question_is_answered_locally(monkeypatch):
    monkeypatch.setattr("actions.visitor_log.visitor_log", lambda p: "2 sightings logged")
    assert "2 sightings" in om.handle_locally("any visitors today")


def test_plural_and_singular_both_match(monkeypatch):
    """'\\bvisitor\\b' silently failed to match 'visitors' — the exact phrasing
    a user is most likely to say."""
    monkeypatch.setattr("actions.visitor_log.visitor_log", lambda p: "ok")
    for phrasing in ("any visitor", "any visitors", "who's been at the door"):
        assert om.handle_locally(phrasing) != om.OFFLINE_REFUSAL


def test_goals_are_answered_locally(monkeypatch):
    monkeypatch.setattr("actions.goals.list_goals", lambda **k: [])
    monkeypatch.setattr("actions.goals.format_goals", lambda g: "no standing goals")
    assert "no standing goals" in om.handle_locally("what are you working on")


def test_unsupported_request_gets_an_honest_refusal():
    """The point of this module is that silence is replaced by a clear answer."""
    answer = om.handle_locally("book me a flight to Rome next Tuesday")
    assert answer == om.OFFLINE_REFUSAL
    assert "offline" in answer.lower()


def test_refusal_lists_what_is_still_possible():
    assert "time" in om.OFFLINE_REFUSAL and "memory" in om.OFFLINE_REFUSAL


def test_empty_input_returns_refusal_not_crash():
    assert om.handle_locally("") == om.OFFLINE_REFUSAL
    assert om.handle_locally(None) == om.OFFLINE_REFUSAL


def test_handler_exception_falls_through_to_refusal(monkeypatch):
    monkeypatch.setattr("actions.visitor_log.visitor_log",
                        lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert om.handle_locally("any visitors") == om.OFFLINE_REFUSAL


def test_handle_locally_always_returns_a_string():
    for text in ["", "   ", "gibberish zxcv", "what time is it"]:
        assert isinstance(om.handle_locally(text), str)


# ── local speech ───────────────────────────────────────────────────────────

def test_speech_is_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(om.platform, "system", lambda: "Linux")
    assert om.speak_offline("hello") is False


def test_speech_uses_the_builtin_say_binary(monkeypatch):
    calls = {}
    monkeypatch.setattr(om.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(om.shutil, "which", lambda name: "/usr/bin/say")
    monkeypatch.setattr(om.subprocess, "Popen", lambda cmd, **k: calls.setdefault("cmd", cmd))

    assert om.speak_offline("all systems nominal") is True
    assert calls["cmd"][0] == "say"
    assert "all systems nominal" in calls["cmd"]


def test_speech_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(om.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(om.shutil, "which", lambda name: "/usr/bin/say")
    monkeypatch.setattr(om.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert om.speak_offline("hello") is False


def test_empty_text_is_not_spoken():
    assert om.speak_offline("") is False
