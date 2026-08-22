"""The VPS and the Mac must agree on which tools the Mac will run.

Delegation is declared TWICE, in two files, for two processes:

  * `main.py`'s `mac_delegated_tools` — what the VPS brain hands to the Mac.
  * `local_worker.py`'s `LOCAL_ACTIONS` / `ACTION_HANDLERS` — what the Mac
    will actually accept and execute.

Nothing tied them together, so adding a tool to one and not the other produced
a silent, confusing failure: the VPS delegated `capability_self_test`, the Mac
answered "not a local worker action", and Jarvis relayed that to the user as
"System self-tests are currently restricted, sir" — a sentence that appears
nowhere in the codebase and describes no real restriction.

These tests make that drift impossible to ship.
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from local_worker import ACTION_HANDLERS, LOCAL_ACTIONS


def _delegated_tools() -> set[str]:
    """Read `mac_delegated_tools` out of main.py without importing it."""
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(r"mac_delegated_tools = \{(.*?)\n        \}", source, re.S)
    assert match, "could not find mac_delegated_tools in main.py"
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def test_every_delegated_tool_is_accepted_by_the_mac_worker():
    """A tool the VPS delegates but the Mac rejects fails at runtime only, and
    the user hears an invented explanation rather than an error."""
    missing = _delegated_tools() - LOCAL_ACTIONS
    assert not missing, (
        f"main.py delegates these to the Mac, but local_worker.LOCAL_ACTIONS "
        f"rejects them: {sorted(missing)}"
    )


def test_every_accepted_action_has_a_handler():
    """Being on the allowlist without a handler yields 'handler unavailable'."""
    missing = LOCAL_ACTIONS - set(ACTION_HANDLERS) - {"status"}
    assert not missing, f"no ACTION_HANDLERS entry for: {sorted(missing)}"


def test_every_handler_actually_imports():
    """Catches a renamed module or function at test time rather than the first
    time the user asks for it."""
    import importlib

    broken = []
    for action, (module_name, attribute) in ACTION_HANDLERS.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            broken.append(f"{action}: cannot import {module_name} ({exc})")
            continue
        if not callable(getattr(module, attribute, None)):
            broken.append(f"{action}: {module_name}.{attribute} is missing or not callable")
    assert not broken, "unusable delegation handlers: " + "; ".join(broken)


@pytest.mark.parametrize("tool", ["capability_self_test", "screen_awareness"])
def test_the_newest_tools_are_wired_on_both_sides(tool):
    """Regression for the specific pair that shipped half-registered."""
    assert tool in _delegated_tools()
    assert tool in LOCAL_ACTIONS
    assert tool in ACTION_HANDLERS


def test_an_unregistered_action_is_rejected_clearly():
    """The rejection must name the cause, so it is never again paraphrased
    into an invented policy."""
    from local_worker import LocalWorker

    result = LocalWorker("http://127.0.0.1:0").execute_local_action("not_a_real_tool", {})
    assert result["status"] == "rejected"
    assert result["action"] == "not_a_real_tool"
