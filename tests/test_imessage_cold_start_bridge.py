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
