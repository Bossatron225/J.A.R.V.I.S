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
