"""Round-trip self-tests: does each capability actually DO anything?

`system_health` answers "are my subsystems alive". This answers a different and
repeatedly-painful question: "when I press this, does the world change?"

The motivating bug: `brightness_up` sent System Events key code 144. macOS
accepts that, exits 0, and moves nothing — modern macOS ignores synthesised
brightness keys. It had been broken indefinitely. Nothing caught it, because
every layer reported success: the subprocess exited clean, the action returned,
the dispatcher said "Done". It was only found by reading the level back.

That is the whole design here. A probe is only meaningful if it OBSERVES the
result independently of the thing that performed it:

    read -> write a different value -> read again -> restore

Probes are split by how intrusive they are:

  * PASSIVE — read-only. Safe to run unattended, on a schedule.
  * ACTIVE  — mutates real state, then restores it. Only on explicit request,
    because the user may be looking at the screen or listening to something.

Every active probe restores in a `finally`, so a probe that throws still puts
the machine back the way it found it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
STATE_PATH = DATA_DIR / "capability_state.json"

WORKING = "working"
BROKEN = "broken"
UNVERIFIABLE = "unverifiable"

PASSIVE = "passive"
ACTIVE = "active"

# How long a recorded result is treated as current. Beyond this Jarvis should
# say "last verified N days ago" rather than implying he just checked.
RESULT_FRESH_SECONDS = 7 * 24 * 3600


class ProbeResult(dict):
    """A single capability's verdict. dict so it serialises for free."""

    def __init__(self, name: str, status: str, detail: str, kind: str = PASSIVE):
        super().__init__(name=name, status=status, detail=detail, kind=kind,
                         checked_at=time.time())


def _round_trip(name: str, read, write, pick_test_value, kind: str = ACTIVE,
                settle: float = 0.35, tolerance: float = 3.0) -> ProbeResult:
    """Generic read → write → verify → restore probe.

    `read` returns a number or None. `pick_test_value` maps the current value
    to something meaningfully different to write. Restoration is unconditional:
    a failed probe must not leave the machine altered."""
    try:
        original = read()
    except Exception as exc:
        return ProbeResult(name, UNVERIFIABLE, f"could not read current value: {exc}", kind)

    if original is None:
        return ProbeResult(name, UNVERIFIABLE, "no readable value on this machine", kind)

    target = pick_test_value(original)
    if target is None:
        return ProbeResult(name, UNVERIFIABLE, "no safe test value available", kind)

    try:
        try:
            write(target)
        except Exception as exc:
            return ProbeResult(name, BROKEN, f"write raised: {exc}", kind)

        time.sleep(settle)

        try:
            observed = read()
        except Exception as exc:
            return ProbeResult(name, UNVERIFIABLE, f"could not read back: {exc}", kind)

        if observed is None:
            return ProbeResult(name, UNVERIFIABLE, "value became unreadable after write", kind)

        if abs(observed - target) <= tolerance:
            return ProbeResult(name, WORKING,
                               f"verified: set to {target}, read back {observed}", kind)
        return ProbeResult(
            name, BROKEN,
            f"reported success but did nothing — asked for {target}, still {observed}",
            kind,
        )
    finally:
        # Unconditional: never leave the user's machine on a test value.
        try:
            write(original)
        except Exception:
            pass


# ── individual probes ──────────────────────────────────────────────────────

def probe_brightness() -> ProbeResult:
    from actions import computer_settings as cs

    if cs._OS not in ("Darwin", "Linux"):
        return ProbeResult("brightness", UNVERIFIABLE, "not supported on this OS", ACTIVE)

    # Small delta: a 6-point flicker is barely perceptible, but is far larger
    # than any rounding wobble between write and read-back.
    def pick(current):
        return current - 6 if current >= 50 else current + 6

    return _round_trip(
        "brightness",
        read=cs.get_brightness,
        write=lambda v: cs.brightness_set(v),
        pick_test_value=pick,
    )


def probe_volume() -> ProbeResult:
    """Round-trips output volume, preserving the mute state.

    Mute must be saved and restored explicitly: on macOS `set volume output
    volume N` silently clears the mute flag, so a naive round trip leaves a
    deliberately-silenced Mac audible. Caught by this module's own first run,
    which is a fair advertisement for the approach."""
    from actions import computer_settings as cs

    if cs._OS not in ("Darwin", "Linux"):
        return ProbeResult("volume", UNVERIFIABLE, "not supported on this OS", ACTIVE)

    was_muted = cs.get_volume().get("muted")

    def read():
        return cs.get_volume().get("level")

    def pick(current):
        return current - 5 if current >= 10 else current + 5

    try:
        return _round_trip("volume", read=read, write=cs.volume_set, pick_test_value=pick)
    finally:
        if was_muted is not None:
            try:
                cs.set_muted(bool(was_muted))
            except Exception:
                pass


def probe_mouse() -> ProbeResult:
    """Can Jarvis actually move the pointer?

    Without Accessibility permission macOS accepts every pyautogui move and
    performs none of them — no error, no exception, the call simply returns.
    That is indistinguishable from success unless the position is read back,
    which is why "I have clicked it for you, sir" was said about clicks that
    never happened. Restores the original pointer position afterwards."""
    try:
        import pyautogui
    except Exception as exc:
        return ProbeResult("mouse", UNVERIFIABLE, f"pyautogui unavailable: {exc}", ACTIVE)

    try:
        origin = pyautogui.position()
        width, height = pyautogui.size()
    except Exception as exc:
        return ProbeResult("mouse", UNVERIFIABLE, f"could not read pointer: {exc}", ACTIVE)

    # Somewhere central and harmless, and never where the pointer already is.
    target = (int(width * 0.5), int(height * 0.5))
    if abs(origin[0] - target[0]) < 5 and abs(origin[1] - target[1]) < 5:
        target = (int(width * 0.3), int(height * 0.3))

    try:
        pyautogui.moveTo(target[0], target[1], duration=0.1)
        time.sleep(0.1)
        landed = pyautogui.position()
    except Exception as exc:
        return ProbeResult("mouse", BROKEN, f"move raised: {exc}", ACTIVE)
    finally:
        try:
            pyautogui.moveTo(origin[0], origin[1], duration=0.1)
        except Exception:
            pass

    if abs(landed[0] - target[0]) <= 2 and abs(landed[1] - target[1]) <= 2:
        return ProbeResult("mouse", WORKING, f"pointer moved to {target} as instructed", ACTIVE)
    return ProbeResult(
        "mouse", BROKEN,
        f"pointer did not move — asked for {target}, stayed at {tuple(landed)}. "
        f"Accessibility permission is probably not granted.",
        ACTIVE,
    )


def probe_camera() -> ProbeResult:
    """Passive: is the camera currently delivering frames?

    Read-only — it never opens the camera itself, so it cannot steal the device
    from the visitor monitor. 'Not streaming' is unverifiable, not broken: the
    camera is only expected to run while the Nanny-cam protocol is engaged."""
    try:
        from actions.camera_session import get_camera_session
        age = get_camera_session(0).last_frame_age_seconds()
    except Exception as exc:
        return ProbeResult("camera", UNVERIFIABLE, f"could not probe: {exc}", PASSIVE)

    if age is None:
        return ProbeResult("camera", UNVERIFIABLE, "not currently streaming", PASSIVE)
    if age > 30:
        return ProbeResult("camera", BROKEN, f"stalled — last frame {age:.0f}s ago", PASSIVE)
    return ProbeResult("camera", WORKING, f"streaming, last frame {age:.0f}s ago", PASSIVE)


def probe_app_listing() -> ProbeResult:
    """Passive: can Jarvis actually see the running applications?

    Underpins every 'close Safari' / 'watch app 3' command, so a silent failure
    here breaks a whole family of instructions."""
    from actions import computer_settings as cs

    if cs._OS != "Darwin":
        return ProbeResult("app_listing", UNVERIFIABLE, "macOS only", PASSIVE)
    try:
        out = cs._list_macos_apps_and_windows()
    except Exception as exc:
        return ProbeResult("app_listing", BROKEN, f"raised: {exc}", PASSIVE)
    if not out or not out.strip():
        return ProbeResult("app_listing", BROKEN, "returned nothing", PASSIVE)
    return ProbeResult("app_listing", WORKING, f"{len(out.splitlines())} line(s) of apps/windows", PASSIVE)


def probe_cloud() -> ProbeResult:
    try:
        from core.offline_mode import cloud_reachable
        ok = cloud_reachable(force=True)
    except Exception as exc:
        return ProbeResult("cloud", UNVERIFIABLE, f"could not probe: {exc}", PASSIVE)
    return (ProbeResult("cloud", WORKING, "reachable", PASSIVE) if ok
            else ProbeResult("cloud", BROKEN, "unreachable", PASSIVE))


def probe_wiz_lights() -> ProbeResult:
    """Passive: are the bulbs reachable from THIS machine?

    The bulbs sit on the home LAN, which the VPS cannot route to — so on the
    VPS 'unreachable' is expected, not a fault. Reported as unverifiable there
    rather than red."""
    try:
        from actions import wiz_lights as wl
    except Exception as exc:
        return ProbeResult("wiz_lights", UNVERIFIABLE, f"module unavailable: {exc}", PASSIVE)

    try:
        ips = wl._load_cached_ips()
    except Exception as exc:
        return ProbeResult("wiz_lights", UNVERIFIABLE, f"could not read bulb cache: {exc}", PASSIVE)
    if not ips:
        return ProbeResult("wiz_lights", UNVERIFIABLE, "no bulbs have been discovered yet", PASSIVE)

    # A plain synchronous UDP getPilot, deliberately NOT the module's async
    # discovery: asyncio.run() cannot be called from inside Jarvis's running
    # live loop, and a probe that only works standalone is worse than none.
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.5)
        for ip in ips[:3]:
            try:
                sock.sendto(b'{"method":"getPilot","params":{}}', (str(ip), 38899))
                sock.recvfrom(2048)
                return ProbeResult("wiz_lights", WORKING, f"bulb at {ip} responded", PASSIVE)
            except Exception:
                continue
    finally:
        sock.close()

    return ProbeResult("wiz_lights", UNVERIFIABLE,
                       f"{len(ips)} known bulb(s), none responding — expected on the VPS, "
                       "which cannot route to the home LAN", PASSIVE)


PROBES: dict[str, callable] = {
    "brightness": probe_brightness,
    "volume": probe_volume,
    "camera": probe_camera,
    "app_listing": probe_app_listing,
    "cloud": probe_cloud,
    "wiz_lights": probe_wiz_lights,
}

PROBE_KINDS: dict[str, str] = {
    "brightness": ACTIVE,
    "volume": ACTIVE,
    "camera": PASSIVE,
    "app_listing": PASSIVE,
    "cloud": PASSIVE,
    "wiz_lights": PASSIVE,
}


# ── running and recording ──────────────────────────────────────────────────

def run_self_test(include_active: bool = False, only: list[str] | None = None) -> list[ProbeResult]:
    """Run probes and persist the verdicts.

    `include_active` defaults False so scheduled runs never mutate the machine
    unasked — a background task must not dim the screen the user is reading."""
    results: list[ProbeResult] = []
    for name, probe in PROBES.items():
        if only and name not in only:
            continue
        if not include_active and PROBE_KINDS.get(name) == ACTIVE and not only:
            continue
        try:
            results.append(probe())
        except Exception as exc:
            results.append(ProbeResult(name, UNVERIFIABLE, f"probe crashed: {exc}",
                                       PROBE_KINDS.get(name, PASSIVE)))
    _record(results)
    return results


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _record(results: list[ProbeResult]) -> None:
    state = _load_state()
    for result in results:
        state[result["name"]] = dict(result)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def capability_status(name: str) -> dict | None:
    """Last recorded verdict for one capability, or None if never tested."""
    return _load_state().get(name)


def known_broken() -> list[str]:
    """Capabilities most recently verified as NOT working.

    This is what stops Jarvis cheerfully offering something that is dead."""
    return sorted(n for n, r in _load_state().items() if r.get("status") == BROKEN)


def _age_phrase(checked_at: float | None, now: float | None = None) -> str:
    if not checked_at:
        return "never tested"
    age = (now if now is not None else time.time()) - checked_at
    if age < 90:
        return "just now"
    if age < 3600:
        return f"{age / 60:.0f}m ago"
    if age < RESULT_FRESH_SECONDS:
        return f"{age / 3600:.0f}h ago"
    return f"{age / 86400:.0f}d ago"


def format_results(results: list[ProbeResult]) -> str:
    if not results:
        return "No capability probes ran, sir."

    broken = [r for r in results if r["status"] == BROKEN]
    working = [r for r in results if r["status"] == WORKING]

    if broken:
        head = (f"Self-test complete, sir — {len(broken)} capability(ies) verified BROKEN: "
                + ", ".join(r["name"] for r in broken) + ".")
    else:
        head = f"Self-test complete, sir — {len(working)} capability(ies) verified working."

    lines = [head]
    for r in results:
        mark = {WORKING: "OK", BROKEN: "BROKEN", UNVERIFIABLE: "-"}[r["status"]]
        lines.append(f"  [{mark}] {r['name']}: {r['detail']}")
    return "\n".join(lines)


def format_state() -> str:
    """Report what is already known, without re-running anything."""
    state = _load_state()
    if not state:
        return "I haven't verified any of my capabilities yet, sir. Ask me to run a self-test."

    lines = ["Capability status, sir:"]
    for name in sorted(state):
        entry = state[name]
        mark = {WORKING: "OK", BROKEN: "BROKEN", UNVERIFIABLE: "-"}.get(entry.get("status"), "?")
        lines.append(f"  [{mark}] {name}: {entry.get('detail', '')} "
                     f"(checked {_age_phrase(entry.get('checked_at'))})")
    return "\n".join(lines)


def capability_self_test(parameters: dict | None = None, response=None, player=None,
                         session_memory=None, speak=None, jarvis=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "") or "").strip().lower()

    if action in ("status", "report", ""):
        return format_state()

    only_raw = str(params.get("capability", "") or "").strip().lower()
    only = [only_raw] if only_raw and only_raw in PROBES else None
    if only_raw and not only:
        return (f"I don't have a probe for '{only_raw}', sir. "
                f"I can test: {', '.join(sorted(PROBES))}.")

    include_active = str(params.get("include_active", "")).lower() in ("true", "1", "yes") \
        or action in ("full", "full_test", "deep")

    results = run_self_test(include_active=include_active, only=only)
    text = format_results(results)
    if not include_active and not only:
        text += ("\n(Read-only probes only. Say “run a full self-test” to also verify the "
                 "controls that have to change something to prove they work.)")
    return text
