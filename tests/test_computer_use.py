"""Tests for the computer-use loop.

Weighted heavily toward the safeguards rather than the vision quality: this is
the one capability that moves a real cursor and types real keystrokes, so the
things that must never happen matter more than the things that usually work.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import computer_use as cu


# ── hard blocks: refused even when approved ────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("click the buy now button", "financial"),
    ("complete the checkout", "financial"),
    ("place the order", "financial"),
    ("type my password into the field", "credential"),
    ("enter the verification code", "credential"),
    ("fill in the credit card number", "credential"),
    ("delete all the files", "destructive"),
    ("empty the trash", "destructive"),
    ("uninstall the app", "destructive"),
    ("shut down the computer", "power"),
])
def test_dangerous_actions_are_refused_even_with_approval(text, label):
    """Approval covers 'click around this app' — never spending money, typing
    a secret, or destroying data."""
    allowed, reason = cu.check_safety(text, approved=True)
    assert allowed is False
    assert "refused" in reason


def test_outward_actions_require_approval_but_are_permitted_with_it():
    allowed, reason = cu.check_safety("send the message", approved=False)
    assert allowed is False and "approval" in reason

    allowed, _ = cu.check_safety("send the message", approved=True)
    assert allowed is True


def test_ordinary_actions_pass():
    allowed, _ = cu.check_safety("click the Settings tab", approved=True)
    assert allowed is True


def test_safety_is_case_insensitive():
    assert cu.check_safety("CLICK BUY NOW", approved=True)[0] is False


# ── Retina coordinate scaling ──────────────────────────────────────────────

def test_screenshot_pixels_are_scaled_to_click_points():
    """Screenshots on this Mac are 2940x1912 while clicks use 1470x956. Passing
    raw screenshot pixels through would double every coordinate and click the
    wrong thing — silently."""
    x, y = cu.scale_to_click_space(1470, 956, shot_size=(2940, 1912), screen_size=(1470, 956))
    assert (x, y) == (735, 478)


def test_scaling_is_identity_when_sizes_match():
    x, y = cu.scale_to_click_space(100, 200, shot_size=(1470, 956), screen_size=(1470, 956))
    assert (x, y) == (100, 200)


def test_coordinates_are_clamped_to_the_screen():
    """A bad coordinate must not walk the cursor off the display."""
    x, y = cu.scale_to_click_space(99999, 99999, shot_size=(2940, 1912), screen_size=(1470, 956))
    assert x == 1469 and y == 955


def test_zero_sized_screenshot_does_not_divide_by_zero():
    x, y = cu.scale_to_click_space(10, 10, shot_size=(0, 0), screen_size=(1470, 956))
    assert (x, y) == (10, 10)


# ── action parsing ─────────────────────────────────────────────────────────

def test_valid_action_is_parsed():
    act = cu.parse_action(json.dumps({"action": "click", "x": 10, "y": 20, "reason": "open menu"}))
    assert act["action"] == "click" and act["x"] == 10


def test_markdown_fenced_json_is_accepted():
    act = cu.parse_action("```json\n" + json.dumps({"action": "done", "reason": "finished"}) + "\n```")
    assert act["action"] == "done"


def test_unknown_action_is_rejected():
    with pytest.raises(cu.ComputerUseError, match="unknown action"):
        cu.parse_action(json.dumps({"action": "launch_missiles"}))


def test_malformed_json_is_rejected():
    with pytest.raises(cu.ComputerUseError, match="could not parse"):
        cu.parse_action("not json")


# ── execution (with a fake controller — no real cursor moves) ──────────────

class _FakeController:
    def __init__(self):
        self.calls = []

    def click(self, x, y, clicks=1): self.calls.append(("click", x, y, clicks))
    def moveTo(self, x, y, duration=0): self.calls.append(("move", x, y))
    def typewrite(self, text, interval=0): self.calls.append(("type", text))
    def hotkey(self, *keys): self.calls.append(("hotkey", keys))
    def press(self, key): self.calls.append(("press", key))
    def scroll(self, amount): self.calls.append(("scroll", amount))


def _act(**kw):
    base = {"action": "click", "x": None, "y": None, "text": "", "keys": [], "amount": 0, "reason": ""}
    base.update(kw)
    return base


def test_click_uses_scaled_coordinates():
    c = _FakeController()
    cu.execute_action(_act(action="click", x=2940, y=1912), (2940, 1912), (1470, 956), c)
    assert c.calls == [("click", 1469, 955, 1)]


def test_double_click_passes_two_clicks():
    c = _FakeController()
    cu.execute_action(_act(action="double_click", x=100, y=100), (1470, 956), (1470, 956), c)
    assert c.calls[0][3] == 2


def test_typing_and_hotkeys():
    c = _FakeController()
    cu.execute_action(_act(action="type", text="hello"), (1, 1), (1, 1), c)
    cu.execute_action(_act(action="hotkey", keys=["command", "s"]), (1, 1), (1, 1), c)
    assert ("type", "hello") in c.calls
    assert ("hotkey", ("command", "s")) in c.calls


def test_click_without_coordinates_is_an_error():
    with pytest.raises(cu.ComputerUseError, match="needs coordinates"):
        cu.execute_action(_act(action="click"), (1, 1), (1, 1), _FakeController())


def test_hotkey_without_keys_is_an_error():
    with pytest.raises(cu.ComputerUseError, match="needs keys"):
        cu.execute_action(_act(action="hotkey"), (1, 1), (1, 1), _FakeController())


# ── verification: the piece that was entirely missing ──────────────────────

def test_verified_change_is_reported_as_success():
    ok, detail = cu.verify_change(
        "clicked Settings", "settings panel opens", b"before", b"after",
        lambda prompt, images: json.dumps({"succeeded": True, "detail": "settings panel is open"}),
    )
    assert ok is True and "settings" in detail


def test_unchanged_screen_is_reported_as_failure():
    """Previously Jarvis clicked and assumed. A screen that didn't change is
    exactly the case that must stop the loop."""
    ok, _ = cu.verify_change(
        "clicked Settings", "settings panel opens", b"before", b"after",
        lambda prompt, images: json.dumps({"succeeded": False, "detail": "screen identical"}),
    )
    assert ok is False


def test_verification_receives_both_screenshots():
    seen = {}
    cu.verify_change("x", "y", b"BEFORE", b"AFTER",
                     lambda prompt, images: seen.update(images=images) or json.dumps({"succeeded": True}))
    assert seen["images"] == [b"BEFORE", b"AFTER"]


def test_unparseable_verification_counts_as_failure():
    """Ambiguity must fail closed — never assume it worked."""
    ok, detail = cu.verify_change("x", "y", b"a", b"b", lambda p, i: "garbage")
    assert ok is False
    assert "could not verify" in detail


# ── approval flow ──────────────────────────────────────────────────────────

def test_plan_preview_asks_for_approval():
    text = cu.describe_plan("open the Settings app", approved=False)
    assert "approved" in text.lower()
    assert "won't touch passwords" in text.lower() or "passwords" in text.lower()


def test_plan_preview_refuses_a_blocked_goal_outright():
    text = cu.describe_plan("buy me a new keyboard on Amazon", approved=False)
    assert "refused" in text.lower()


def test_step_budget_is_bounded():
    assert 1 <= cu.MAX_STEPS <= 15
