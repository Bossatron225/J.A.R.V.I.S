"""Tests for capability round-trip self-tests.

The point of this module is catching an effector that reports success and does
nothing, so the tests weigh heaviest on exactly that case — plus the promise
that a probe never leaves the user's machine on a test value.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import capability_test as ct


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never read or write the real capability_state.json."""
    monkeypatch.setattr(ct, "STATE_PATH", tmp_path / "capability_state.json")


# ── the core detection ─────────────────────────────────────────────────────

def test_a_writer_that_does_nothing_is_reported_broken():
    """This is the brightness_up bug exactly: exits clean, changes nothing."""
    r = ct._round_trip("fake", read=lambda: 50, write=lambda v: None,
                       pick_test_value=lambda c: c - 6, settle=0)
    assert r["status"] == ct.BROKEN
    assert "did nothing" in r["detail"]


def test_a_working_writer_is_reported_working():
    state = {"v": 50}
    r = ct._round_trip("fake", read=lambda: state["v"],
                       write=lambda v: state.__setitem__("v", v),
                       pick_test_value=lambda c: c - 6, settle=0)
    assert r["status"] == ct.WORKING


def test_a_raising_writer_is_reported_broken():
    r = ct._round_trip("fake", read=lambda: 50,
                       write=lambda v: (_ for _ in ()).throw(RuntimeError("nope")),
                       pick_test_value=lambda c: c - 6, settle=0)
    assert r["status"] == ct.BROKEN
    assert "nope" in r["detail"]


def test_an_unreadable_value_is_unverifiable_not_broken():
    """No reading means no verdict. Calling that 'broken' would cry wolf on
    every machine without the hardware."""
    r = ct._round_trip("fake", read=lambda: None, write=lambda v: None,
                       pick_test_value=lambda c: c - 6, settle=0)
    assert r["status"] == ct.UNVERIFIABLE


def test_a_raising_reader_is_unverifiable():
    r = ct._round_trip("fake", read=lambda: (_ for _ in ()).throw(OSError("x")),
                       write=lambda v: None, pick_test_value=lambda c: c, settle=0)
    assert r["status"] == ct.UNVERIFIABLE


def test_small_rounding_differences_are_tolerated():
    """Hardware rarely lands on the exact integer asked for; a 1-point wobble
    must not be reported as a fault."""
    state = {"v": 50}
    r = ct._round_trip("fake", read=lambda: state["v"],
                       write=lambda v: state.__setitem__("v", v + 1),
                       pick_test_value=lambda c: c - 6, settle=0, tolerance=3.0)
    assert r["status"] == ct.WORKING


# ── restoration ────────────────────────────────────────────────────────────

def test_the_original_value_is_restored():
    state = {"v": 62}
    ct._round_trip("fake", read=lambda: state["v"],
                   write=lambda v: state.__setitem__("v", v),
                   pick_test_value=lambda c: c - 6, settle=0)
    assert state["v"] == 62


def test_the_original_value_is_restored_even_when_the_probe_fails():
    """A probe that throws mid-way must still put the machine back."""
    writes = []
    state = {"v": 62}

    def write(v):
        writes.append(v)
        state["v"] = v
        if len(writes) == 1:
            raise RuntimeError("boom")

    ct._round_trip("fake", read=lambda: state["v"], write=write,
                   pick_test_value=lambda c: c - 6, settle=0)
    assert state["v"] == 62, "must restore after a failed write"


def test_restoration_failure_does_not_mask_the_verdict():
    def write(v):
        if v == 44:
            return
        raise RuntimeError("restore failed")

    r = ct._round_trip("fake", read=lambda: 50, write=write,
                       pick_test_value=lambda c: c - 6, settle=0)
    assert r["status"] == ct.BROKEN  # verdict survives a failed restore


# ── active vs passive ──────────────────────────────────────────────────────

def test_scheduled_runs_skip_active_probes(monkeypatch):
    """A background self-test must never dim the screen the user is reading."""
    ran = []
    monkeypatch.setattr(ct, "PROBES", {
        "act": lambda: ran.append("act") or ct.ProbeResult("act", ct.WORKING, "", ct.ACTIVE),
        "pas": lambda: ran.append("pas") or ct.ProbeResult("pas", ct.WORKING, "", ct.PASSIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"act": ct.ACTIVE, "pas": ct.PASSIVE})

    ct.run_self_test(include_active=False)
    assert ran == ["pas"]


def test_full_test_includes_active_probes(monkeypatch):
    ran = []
    monkeypatch.setattr(ct, "PROBES", {
        "act": lambda: ran.append("act") or ct.ProbeResult("act", ct.WORKING, "", ct.ACTIVE),
        "pas": lambda: ran.append("pas") or ct.ProbeResult("pas", ct.WORKING, "", ct.PASSIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"act": ct.ACTIVE, "pas": ct.PASSIVE})

    ct.run_self_test(include_active=True)
    assert set(ran) == {"act", "pas"}


def test_a_crashing_probe_does_not_abort_the_run(monkeypatch):
    monkeypatch.setattr(ct, "PROBES", {
        "bad": lambda: (_ for _ in ()).throw(RuntimeError("kaboom")),
        "good": lambda: ct.ProbeResult("good", ct.WORKING, "fine", ct.PASSIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"bad": ct.PASSIVE, "good": ct.PASSIVE})

    results = ct.run_self_test()
    assert {r["name"] for r in results} == {"bad", "good"}
    assert next(r for r in results if r["name"] == "bad")["status"] == ct.UNVERIFIABLE


# ── persistence and recall ─────────────────────────────────────────────────

def test_results_are_persisted_and_readable(monkeypatch):
    monkeypatch.setattr(ct, "PROBES", {"x": lambda: ct.ProbeResult("x", ct.BROKEN, "dead", ct.PASSIVE)})
    monkeypatch.setattr(ct, "PROBE_KINDS", {"x": ct.PASSIVE})

    ct.run_self_test()
    assert ct.capability_status("x")["status"] == ct.BROKEN
    assert ct.known_broken() == ["x"]


def test_known_broken_excludes_working_and_unverifiable(monkeypatch):
    monkeypatch.setattr(ct, "PROBES", {
        "a": lambda: ct.ProbeResult("a", ct.BROKEN, "", ct.PASSIVE),
        "b": lambda: ct.ProbeResult("b", ct.WORKING, "", ct.PASSIVE),
        "c": lambda: ct.ProbeResult("c", ct.UNVERIFIABLE, "", ct.PASSIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {k: ct.PASSIVE for k in "abc"})

    ct.run_self_test()
    assert ct.known_broken() == ["a"]


def test_a_later_pass_clears_a_previous_broken_verdict(monkeypatch):
    monkeypatch.setattr(ct, "PROBE_KINDS", {"x": ct.PASSIVE})
    monkeypatch.setattr(ct, "PROBES", {"x": lambda: ct.ProbeResult("x", ct.BROKEN, "", ct.PASSIVE)})
    ct.run_self_test()
    monkeypatch.setattr(ct, "PROBES", {"x": lambda: ct.ProbeResult("x", ct.WORKING, "", ct.PASSIVE)})
    ct.run_self_test()
    assert ct.known_broken() == []


def test_missing_state_file_is_not_an_error():
    assert ct.known_broken() == []
    assert ct.capability_status("anything") is None


def test_corrupt_state_file_is_survived():
    ct.STATE_PATH.write_text("{not json")
    assert ct.known_broken() == []


# ── reporting ──────────────────────────────────────────────────────────────

def test_report_leads_with_the_broken_ones():
    results = [ct.ProbeResult("a", ct.BROKEN, "dead", ct.ACTIVE),
               ct.ProbeResult("b", ct.WORKING, "fine", ct.PASSIVE)]
    out = ct.format_results(results)
    assert "BROKEN" in out and "a" in out.split("\n")[0]


def test_report_says_all_clear_when_nothing_is_broken():
    out = ct.format_results([ct.ProbeResult("b", ct.WORKING, "fine", ct.PASSIVE)])
    assert "verified working" in out


def test_reports_address_the_user_as_sir():
    assert "sir" in ct.format_results([ct.ProbeResult("b", ct.WORKING, "f", ct.PASSIVE)])
    assert "sir" in ct.format_state()


def test_status_action_does_not_run_probes(monkeypatch):
    monkeypatch.setattr(ct, "PROBES", {
        "x": lambda: pytest.fail("status must not run probes")})
    ct.capability_self_test({"action": "status"})


def test_default_action_is_the_read_only_status(monkeypatch):
    monkeypatch.setattr(ct, "PROBES", {
        "x": lambda: pytest.fail("default must not run probes")})
    ct.capability_self_test({})


def test_test_action_runs_passive_probes_only(monkeypatch):
    ran = []
    monkeypatch.setattr(ct, "PROBES", {
        "act": lambda: ran.append("act") or ct.ProbeResult("act", ct.WORKING, "", ct.ACTIVE),
        "pas": lambda: ran.append("pas") or ct.ProbeResult("pas", ct.WORKING, "", ct.PASSIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"act": ct.ACTIVE, "pas": ct.PASSIVE})

    out = ct.capability_self_test({"action": "test"})
    assert ran == ["pas"]
    assert "full self-test" in out, "should tell the user how to check the rest"


def test_full_action_runs_everything(monkeypatch):
    ran = []
    monkeypatch.setattr(ct, "PROBES", {
        "act": lambda: ran.append("act") or ct.ProbeResult("act", ct.WORKING, "", ct.ACTIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"act": ct.ACTIVE})

    ct.capability_self_test({"action": "full"})
    assert ran == ["act"]


def test_a_single_named_capability_can_be_tested(monkeypatch):
    ran = []
    monkeypatch.setattr(ct, "PROBES", {
        "brightness": lambda: ran.append("b") or ct.ProbeResult("brightness", ct.WORKING, "", ct.ACTIVE),
        "volume": lambda: ran.append("v") or ct.ProbeResult("volume", ct.WORKING, "", ct.ACTIVE),
    })
    monkeypatch.setattr(ct, "PROBE_KINDS", {"brightness": ct.ACTIVE, "volume": ct.ACTIVE})

    ct.capability_self_test({"action": "test", "capability": "brightness"})
    assert ran == ["b"]


def test_an_unknown_capability_name_is_rejected_helpfully():
    out = ct.capability_self_test({"action": "test", "capability": "teleporter"})
    assert "don't have a probe" in out
    assert "brightness" in out


def test_age_phrasing_does_not_imply_a_fresh_check():
    assert ct._age_phrase(None) == "never tested"
    assert ct._age_phrase(1000.0, now=1000.0) == "just now"
    assert "d ago" in ct._age_phrase(1000.0, now=1000.0 + 10 * 86400)
    assert "h ago" in ct._age_phrase(1000.0, now=1000.0 + 3 * 3600)


# ── the real probes, without touching real hardware ────────────────────────

def test_volume_probe_restores_the_mute_state(monkeypatch):
    """Regression: volume_set clears mute on macOS, so the first real run of
    this probe un-muted a deliberately silenced Mac."""
    from actions import computer_settings as cs
    state = {"level": 25, "muted": True}

    monkeypatch.setattr(cs, "_OS", "Darwin")
    monkeypatch.setattr(cs, "get_volume", lambda: dict(state))
    monkeypatch.setattr(cs, "volume_set", lambda v: state.update(level=v, muted=False))
    monkeypatch.setattr(cs, "set_muted", lambda m: state.update(muted=m))
    monkeypatch.setattr(ct.time, "sleep", lambda *_: None)

    result = ct.probe_volume()
    assert result["status"] == ct.WORKING
    assert state == {"level": 25, "muted": True}


def test_brightness_probe_uses_the_real_reader(monkeypatch):
    from actions import computer_settings as cs
    state = {"v": 62}
    monkeypatch.setattr(cs, "_OS", "Darwin")
    monkeypatch.setattr(cs, "get_brightness", lambda: state["v"])
    monkeypatch.setattr(cs, "brightness_set", lambda v: state.__setitem__("v", v))
    monkeypatch.setattr(ct.time, "sleep", lambda *_: None)

    assert ct.probe_brightness()["status"] == ct.WORKING
    assert state["v"] == 62


def test_camera_probe_treats_not_streaming_as_unverifiable(monkeypatch):
    """The camera only runs while the Nanny-cam protocol is engaged; idle is
    not a fault."""
    import actions.camera_session as cam

    class _S:
        def last_frame_age_seconds(self):
            return None

    monkeypatch.setattr(cam, "get_camera_session", lambda i: _S())
    assert ct.probe_camera()["status"] == ct.UNVERIFIABLE


def test_camera_probe_flags_a_stall(monkeypatch):
    import actions.camera_session as cam

    class _S:
        def last_frame_age_seconds(self):
            return 300.0

    monkeypatch.setattr(cam, "get_camera_session", lambda i: _S())
    assert ct.probe_camera()["status"] == ct.BROKEN
