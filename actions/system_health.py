"""Functional health checks for Jarvis's own subsystems.

Every check here answers "did this actually do its job recently", never "is
the thread alive". That distinction is the whole point of this module: a
string of outages all presented identically as healthy —

  * the visitor monitor reported "engaged" for 8+ minutes while its camera was
    never successfully opened (thread alive, subprocess absent, no error);
  * the VPS local-worker poller was silently garbage-collected mid-run and
    simply stopped polling (no exception, no log);
  * the Gemini live session died on a clean websocket close and never
    reconnected, silently dropping every dashboard command (session object
    still present).

In all three, a liveness check would have reported green. Age-of-last-success
is what actually catches them, so that is what these probes measure.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# How stale each signal may get before it counts as unhealthy. These are set
# from each subsystem's real cadence with generous headroom, so a slow cycle
# is never mistaken for an outage.
LIVE_ACTIVITY_MAX_AGE = 900.0     # live session is idle-quiet; only long silence is suspicious
CAMERA_FRAME_MAX_AGE = 30.0       # streaming subprocess self-throttles to ~0.4s
LOCAL_WORKER_MAX_AGE = 60.0       # poller runs every ~5s


def _fmt_age(age: float | None) -> str:
    if age is None:
        return "never"
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60:.0f}m ago"
    return f"{age / 3600:.1f}h ago"


def _check(name: str, ok: bool, detail: str, applicable: bool = True) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "applicable": applicable}


def git_revision() -> str:
    """Short commit SHA this process's code came from — lets a health report
    expose which machine is running which build, so silent drift between the
    Mac and the VPS is visible instead of being discovered 30 commits later."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def collect_health(jarvis) -> dict:
    """Probe every subsystem of a running JarvisLive. Returns
    {"ok", "revision", "checks": [...]}; each check carries `applicable`
    so a capability that legitimately does not run here (e.g. the camera on
    the headless VPS) is reported as N/A rather than as a failure."""
    import time

    checks: list[dict] = []
    now = time.monotonic()

    is_headless = type(getattr(jarvis, "ui", None)).__name__ == "_HeadlessUI"

    # ── Live Gemini session ────────────────────────────────────────────────
    session = getattr(jarvis, "session", None)
    last_live = getattr(jarvis, "_last_live_activity_ts", 0.0)
    live_age = (now - last_live) if last_live else None
    if getattr(jarvis, "_worker_mode", False):
        # Pure Mac-side worker for a VPS-hosted brain: having no local live
        # session is correct operation here, not a fault.
        checks.append(_check(
            "live_session", True,
            "N/A — remote worker mode; the VPS holds the live session",
            applicable=False,
        ))
    elif session is None:
        checks.append(_check("live_session", False, "not connected — dashboard/voice commands will be dropped"))
    elif live_age is not None and live_age > LIVE_ACTIVITY_MAX_AGE:
        checks.append(_check("live_session", False, f"connected but no traffic {_fmt_age(live_age)} — may be a dead socket"))
    else:
        checks.append(_check("live_session", True, f"connected, last traffic {_fmt_age(live_age)}"))

    # ── Camera / visitor monitor ───────────────────────────────────────────
    watch_engaged = None
    try:
        from actions.visitor_log import is_watch_active
        watch_engaged = is_watch_active()
    except Exception:
        pass

    thread = getattr(jarvis, "_visitor_monitor_thread", None)
    thread_alive = bool(thread and thread.is_alive())

    if is_headless:
        checks.append(_check("visitor_monitor", True, "N/A — no camera on the headless VPS", applicable=False))
        checks.append(_check("camera_frames", True, "N/A — no camera on the headless VPS", applicable=False))
    else:
        if not thread_alive:
            checks.append(_check("visitor_monitor", False, "monitor thread is not running"))
        elif watch_engaged is False:
            checks.append(_check("visitor_monitor", True, "running, Nanny-cam protocol disengaged"))
        else:
            checks.append(_check("visitor_monitor", True, "running, Nanny-cam protocol engaged"))

        # Only meaningful while the monitor is actually supposed to be capturing.
        if not thread_alive or watch_engaged is False:
            checks.append(_check("camera_frames", True, "N/A — monitor not capturing right now", applicable=False))
        else:
            frame_age = None
            gate_active = False
            try:
                from actions.camera_session import get_camera_session
                cfg = getattr(jarvis, "_visitor_watch_cfg", {}) or {}
                index = int(cfg.get("camera_index", 0) or 0)
                frame_age = get_camera_session(index).last_frame_age_seconds()
                gate_active = bool(jarvis._local_speech_gate_active())
            except Exception as exc:
                checks.append(_check("camera_frames", False, f"could not probe camera: {exc}"))
                frame_age = "probe-failed"

            if frame_age == "probe-failed":
                pass
            elif gate_active:
                checks.append(_check("camera_frames", True, "N/A — held off by biometric lock", applicable=False))
            elif frame_age is None:
                checks.append(_check("camera_frames", False, "engaged but camera has NEVER delivered a frame"))
            elif frame_age > CAMERA_FRAME_MAX_AGE:
                checks.append(_check("camera_frames", False, f"stalled — last frame {_fmt_age(frame_age)}"))
            else:
                checks.append(_check("camera_frames", True, f"streaming, last frame {_fmt_age(frame_age)}"))

    # ── Mac↔VPS local worker link ──────────────────────────────────────────
    import os
    vps_url = (os.getenv("JARVIS_VPS_URL") or "").strip()
    if is_headless or not vps_url:
        checks.append(_check("vps_worker_link", True, "N/A — no VPS link configured for this process", applicable=False))
    else:
        last_poll = getattr(jarvis, "_local_worker_last_poll_ts", 0.0)
        poll_age = (now - last_poll) if last_poll else None
        if poll_age is None:
            checks.append(_check("vps_worker_link", False, "has never polled the VPS — remote actions will time out"))
        elif poll_age > LOCAL_WORKER_MAX_AGE:
            checks.append(_check("vps_worker_link", False, f"stopped polling — last poll {_fmt_age(poll_age)}"))
        else:
            checks.append(_check("vps_worker_link", True, f"polling, last poll {_fmt_age(poll_age)}"))

    # ── Background tasks ───────────────────────────────────────────────────
    tasks = getattr(jarvis, "_background_tasks", None)
    if tasks is None:
        checks.append(_check("background_tasks", False, "task registry missing"))
    else:
        alive = [t for t in tasks if not t.done()]
        checks.append(_check("background_tasks", True, f"{len(alive)} running"))

    applicable = [c for c in checks if c["applicable"]]
    return {
        "ok": all(c["ok"] for c in applicable),
        "revision": git_revision(),
        "checks": checks,
    }


def format_health(report: dict) -> str:
    """Render a health report as short spoken/readable text."""
    lines: list[str] = []
    failing = [c for c in report["checks"] if c["applicable"] and not c["ok"]]

    if report["ok"]:
        lines.append(f"All systems nominal, sir. (build {report['revision']})")
    else:
        problems = ", ".join(c["name"] for c in failing)
        lines.append(f"Attention, sir — {len(failing)} subsystem(s) degraded: {problems}. (build {report['revision']})")

    for check in report["checks"]:
        if not check["applicable"]:
            mark = "-"
        elif check["ok"]:
            mark = "OK"
        else:
            mark = "FAIL"
        lines.append(f"  [{mark}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


def system_health(parameters: dict | None = None, response=None, player=None,
                  session_memory=None, speak=None, jarvis=None) -> str:
    if jarvis is None:
        return "Health checks require the running Jarvis instance."
    return format_health(collect_health(jarvis))
