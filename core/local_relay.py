import os

"""Lets VPS-only code (actions/web_search.py, memory/conversation_log.py) delegate a
specific Gemini call to the Mac over the existing local-worker task queue
(local_worker.py + vps_orchestrator.py's /api/local-worker/* routes), instead of
calling Gemini directly from the VPS. Exists because two Gemini features (search
grounding, embeddings) return "User location is not supported for the API use" from
this VPS's datacenter region while working fine from the Mac's own connection — this
routes those two calls through real infrastructure the user already owns and control
(their own Mac, on their own network), not a proxy/VPN spoofing the VPS's location."""

_runtime_bridge = None


def set_runtime_bridge(bridge) -> None:
    global _runtime_bridge
    _runtime_bridge = bridge


def is_available() -> bool:
    # Only ever engages in the VPS/headless deployment (JARVIS_HEADLESS=1, set by
    # jarvis-vps.service) — on the Mac, _runtime_bridge is never set at all, since
    # vps_orchestrator.py (the only thing that calls set_runtime_bridge) never runs
    # there, so this is a no-op fallthrough to the direct Gemini call in that case.
    return _runtime_bridge is not None and os.environ.get("JARVIS_HEADLESS", "").strip() not in ("", "0", "false", "False")


def call(action: str, payload: dict, timeout: float = 20.0) -> dict:
    if _runtime_bridge is None:
        raise RuntimeError("no local worker bridge configured")
    return _runtime_bridge.request_local_action(action, payload, timeout=timeout)
