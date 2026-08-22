"""Tests for ambient screen awareness.

Overwhelmingly about the privacy gates. This feature screenshots the user's
whole desktop and sends it to a cloud vision API, so what it must REFUSE to
look at matters far more than what it notices — and the refusal has to happen
before the capture, not after.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import screen_awareness as sa


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "STATE_PATH", tmp_path / "screen_awareness.json")


@pytest.fixture
def enabled(monkeypatch):
    sa.set_enabled(True)
    monkeypatch.setattr(sa, "frontmost_context", lambda: ("Xcode", "MyProject — build succeeded"))
    monkeypatch.setattr("memory.usage_log.calls_since", lambda *a, **k: 0)


# ── off by default ─────────────────────────────────────────────────────────

def test_it_is_off_until_explicitly_enabled():
    """There must be no implicit start for whole-screen capture."""
    assert sa.is_enabled() is False
    allowed, reason = sa.should_look()
    assert allowed is False and "off" in reason


def test_enabling_and_disabling_persists():
    sa.set_enabled(True)
    assert sa.is_enabled() is True
    sa.set_enabled(False)
    assert sa.is_enabled() is False


def test_a_corrupt_state_file_leaves_it_off():
    """Failing open here would start capturing the screen after a disk glitch."""
    sa.STATE_PATH.write_text("{ broken")
    assert sa.is_enabled() is False


# ── the sensitivity screen ─────────────────────────────────────────────────

@pytest.mark.parametrize("app", [
    "1Password", "1Password 8", "Bitwarden", "LastPass", "Dashlane",
    "Keychain Access", "Authy", "Terminal", "iTerm2",
])
def test_sensitive_applications_are_never_looked_at(app):
    sensitive, reason = sa.is_sensitive(app, "some window")
    assert sensitive is True
    assert "never-look" in reason


def test_application_matching_is_case_insensitive_and_partial():
    assert sa.is_sensitive("1password 8 — Personal Vault", "x")[0] is True
    assert sa.is_sensitive("BITWARDEN", "x")[0] is True


@pytest.mark.parametrize("title", [
    "Sign in — Google Accounts",
    "Log in to your account",
    "Revolut — Personal",
    "Barclays Online Banking",
    "PayPal Checkout",
    "Enter your password",
    "Two-factor authentication",
    "Stripe Dashboard — Billing",
    "Private Browsing",
    "New Incognito Window",
    "api_key configuration",
    "project/.env",
    "Patient health record",
])
def test_sensitive_window_titles_block_capture(title):
    sensitive, reason = sa.is_sensitive("Safari", title)
    assert sensitive is True, f"{title!r} should have been blocked"
    assert reason


def test_ordinary_windows_are_allowed():
    assert sa.is_sensitive("Xcode", "MyProject — Build Succeeded")[0] is False
    assert sa.is_sensitive("Safari", "Python documentation")[0] is False


def test_an_unidentifiable_window_fails_closed():
    """If the frontmost app can't be determined it might be anything, so the
    safe default is to not look."""
    sensitive, reason = sa.is_sensitive("", "")
    assert sensitive is True
    assert "could not identify" in reason


def test_the_user_can_add_their_own_blocked_app():
    sa.block_app("Signal")
    assert sa.is_sensitive("Signal", "Chat")[0] is True


def test_blocking_the_same_app_twice_is_idempotent():
    sa.block_app("Signal")
    message = sa.block_app("Signal")
    assert "already" in message
    assert sorted(sa.user_blocked_apps()) == ["signal"]


def test_the_user_can_add_their_own_blocked_title_keyword(monkeypatch):
    monkeypatch.setattr(sa, "user_blocked_keywords", lambda: ["Divorce"])
    assert sa.is_sensitive("Pages", "Divorce paperwork draft")[0] is True


# ── the full gate ──────────────────────────────────────────────────────────

def test_the_gate_passes_on_an_ordinary_screen(enabled):
    assert sa.should_look()[0] is True


def test_the_biometric_lock_stops_it(enabled):
    """If Jarvis can't tell who is at the machine, he shouldn't narrate it."""
    allowed, reason = sa.should_look(lock_active=True)
    assert allowed is False and "biometric lock" in reason


def test_a_sensitive_window_stops_it(enabled, monkeypatch):
    monkeypatch.setattr(sa, "frontmost_context", lambda: ("1Password", "Vault"))
    assert sa.should_look()[0] is False


def test_the_hourly_cap_stops_it(enabled, monkeypatch):
    monkeypatch.setattr("memory.usage_log.calls_since", lambda *a, **k: sa.MAX_CALLS_PER_HOUR)
    allowed, reason = sa.should_look()
    assert allowed is False and "limit" in reason


def test_disabling_stops_it_even_with_everything_else_fine(enabled):
    sa.set_enabled(False)
    assert sa.should_look()[0] is False


def test_the_sensitivity_check_precedes_any_capture(monkeypatch):
    """The whole privacy model rests on this: a blocked screen must never be
    screenshotted at all, so there is no sensitive image to leak."""
    sa.set_enabled(True)
    monkeypatch.setattr(sa, "frontmost_context", lambda: ("1Password", "Vault"))
    monkeypatch.setattr(sa, "capture_screen_png",
                        lambda: pytest.fail("must not capture a blocked screen"))
    assert sa.should_look()[0] is False


# ── interval ───────────────────────────────────────────────────────────────

def test_the_interval_has_a_floor():
    """A too-short interval turns ambient awareness into constant surveillance
    and multiplies API spend."""
    sa._save_state({"enabled": True, "interval_seconds": 1})
    assert sa.interval_seconds() == sa.MIN_INTERVAL_SECONDS


def test_a_garbage_interval_falls_back_to_the_default():
    sa._save_state({"enabled": True, "interval_seconds": "soon"})
    assert sa.interval_seconds() == sa.DEFAULT_INTERVAL_SECONDS


def test_the_interval_can_be_set_in_minutes():
    sa.screen_awareness({"action": "interval", "value": "10"})
    assert sa.interval_seconds() == 600


# ── the vision call ────────────────────────────────────────────────────────

def test_no_screenshot_means_no_call(monkeypatch):
    monkeypatch.setattr(sa, "_get_api_key", lambda: pytest.fail("must not reach the API"))
    assert sa.analyse_screen(b"")["should_speak"] is False


def test_a_missing_api_key_is_handled():
    assert sa.analyse_screen(b"png", None)["should_speak"] is False


def test_api_failure_stays_silent(monkeypatch):
    """Ambient features must fail quiet, never surface an error unprompted."""
    monkeypatch.setattr(sa, "_get_api_key", lambda: "k")
    result = sa.analyse_screen(b"png")
    assert result["should_speak"] is False


def test_the_prompt_forbids_narrating_private_content():
    assert "personal, financial, medical or private" in sa._PROMPT
    assert "should_speak=false" in sa._PROMPT


def test_the_prompt_biases_toward_silence():
    assert "when in doubt" in sa._PROMPT.lower()
    assert "most glances should return false" in sa._PROMPT.lower()


# ── tool surface ───────────────────────────────────────────────────────────

def test_start_and_stop():
    out = sa.screen_awareness({"action": "start"})
    assert sa.is_enabled() is True
    assert "sir" in out
    assert "password manager" in out, "enabling should state what it won't look at"

    out = sa.screen_awareness({"action": "stop"})
    assert sa.is_enabled() is False
    assert "sir" in out


def test_status_when_off_explains_how_to_start():
    assert "off" in sa.screen_awareness({"action": "status"})
    assert "start watching my screen" in sa.screen_awareness({})


def test_status_when_on_lists_user_blocked_apps():
    sa.set_enabled(True)
    sa.block_app("Signal")
    assert "Signal".lower() in sa.screen_awareness({"action": "status"}).lower()


def test_block_without_a_name_asks():
    assert "Which application" in sa.screen_awareness({"action": "block"})


def test_block_via_the_tool():
    sa.screen_awareness({"action": "block", "app_name": "Signal"})
    assert sa.is_sensitive("Signal", "")[0] is True
