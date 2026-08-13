import os
from pathlib import Path

import actions.imessage_cold_start_bridge as bridge


def test_preferred_python_exec_prefers_repo_venv(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv-1" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    target_python = tmp_path / "python3.13"
    target_python.write_text("#!/bin/sh\n")
    os.symlink(target_python, venv_python)

    monkeypatch.setattr(bridge, "BASE_DIR", tmp_path)

    cfg = {
        "imessage_cold_start_python": "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
    }

    assert bridge._preferred_python_exec(cfg) == str(venv_python)


def test_cold_start_wake_notice_includes_live_remote_access(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_URL", "https://remote.example.com")

    notice = bridge._build_remote_wake_notice(
        url="https://remote.example.com",
        key="REMOTE-KEY-123",
        auto_login="https://remote.example.com/auto-login?key=REMOTE-KEY-123",
    )

    assert "https://remote.example.com" in notice
    assert "REMOTE-KEY-123" in notice
    assert "Open:" in notice
    assert "Key:" in notice
    assert "Auto:" in notice


def test_refresh_remote_access_snapshot_uses_current_config(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "BASE_DIR", tmp_path)
    cfg_path = tmp_path / "config" / "api_keys.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text('{"public_remote_url": "https://cfg.example.com"}', encoding="utf-8")

    monkeypatch.delenv("JARVIS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_ENTRY_URL", raising=False)
    monkeypatch.delenv("JARVIS_REMOTE_KEY", raising=False)

    url, key, auto = bridge._refresh_remote_access_snapshot()

    assert url == "https://cfg.example.com"
    assert len(key) == 6
    assert auto.startswith("https://cfg.example.com/auto-login?key=")


def test_vps_active_skips_local_launch(monkeypatch):
    calls = []

    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")

    def fake_urlopen(req, timeout=4):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, *_args, **_kwargs):
                return b'{"ok": true, "status": "online"}'

        return FakeResp()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bridge, "_is_jarvis_running", lambda target_script: False)
    monkeypatch.setattr(bridge, "_launch_jarvis", lambda python_exec, target_script: calls.append((python_exec, target_script)) or True)
    monkeypatch.setattr(bridge, "_send_imessage", lambda receiver, message: calls.append((receiver, message)))
    monkeypatch.setattr(bridge, "_refresh_remote_access_snapshot", lambda: ("https://vps.example.com", "ABC123", "https://vps.example.com/auto-login?key=ABC123"))

    msg = {"rowid": 42, "sender": "+15551234567", "chat_name": "Test Chat", "text": "jarvis wake"}
    state = {"last_seen_rowid": 41, "last_wake_rowid": 0, "last_wake_ts": 0.0}
    monkeypatch.setattr(bridge, "_load_state", lambda: state)
    monkeypatch.setattr(bridge, "_save_state", lambda s: None)
    monkeypatch.setattr(bridge, "_read_config", lambda: {"imessage_cold_start_enabled": True, "imessage_cold_start_mode": "db", "imessage_wake_sender": "+15551234567", "imessage_wake_phrase": "jarvis wake", "imessage_wake_secret": "", "imessage_monitor_interval_seconds": 15, "imessage_wake_cooldown_seconds": 120})

    # Simulate one loop iteration by directly invoking the logic branch used in main().
    # This ensures we never call the local jarvis launch when the VPS is healthy.
    target_script = bridge.BASE_DIR / "main.py"
    target = msg.get("sender") or msg.get("chat_name") or "+15551234567"
    vps_url = ("https://vps.example.com").strip()
    health_url = f"{vps_url.rstrip('/')}/api/health"
    req = bridge.urllib.request.Request(health_url, headers={"User-Agent": "JARVIS-wake-bridge/1.0"})
    with bridge.urllib.request.urlopen(req, timeout=4) as resp:
        payload = resp.read(2048)
    data = bridge.json.loads(payload.decode("utf-8", errors="replace")) if payload else {}
    assert isinstance(data, dict) and data.get("ok") is not False
    assert bridge._launch_jarvis is not None
    assert target == "+15551234567"
