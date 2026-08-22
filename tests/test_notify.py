import json

import actions.notify as notify_module


def _write_config(tmp_path, monkeypatch, **overrides):
    config = {"jarvis_notify_enabled": True, "jarvis_notify_target": "5551234567", **overrides}
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(notify_module, "API_CONFIG_PATH", config_path)
    return config_path


def test_notify_user_sends_when_enabled_and_configured(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(notify_module, "send_imessage", lambda receiver, msg, attachment_path=None: calls.append((receiver, msg)) or "sent")

    result = notify_module.notify_user("Jarvis updated main.py: fixed a bug.")

    assert calls == [("5551234567", "Jarvis updated main.py: fixed a bug.")]
    assert result == "sent"


def test_notify_user_noops_when_disabled(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, jarvis_notify_enabled=False)
    calls = []
    monkeypatch.setattr(notify_module, "send_imessage", lambda *a, **k: calls.append(a))

    result = notify_module.notify_user("hello")

    assert calls == []
    assert "disabled" in result.lower()


def test_notify_user_noops_when_target_missing(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, jarvis_notify_target="")
    calls = []
    monkeypatch.setattr(notify_module, "send_imessage", lambda *a, **k: calls.append(a))

    result = notify_module.notify_user("hello")

    assert calls == []
    assert "no jarvis_notify_target" in result.lower()


def test_notify_user_noops_on_empty_message(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(notify_module, "send_imessage", lambda *a, **k: calls.append(a))

    result = notify_module.notify_user("   ")

    assert calls == []
    assert "no notification message" in result.lower()


def test_notify_user_defaults_enabled_when_key_missing(tmp_path, monkeypatch):
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps({"jarvis_notify_target": "5551234567"}), encoding="utf-8")
    monkeypatch.setattr(notify_module, "API_CONFIG_PATH", config_path)
    calls = []
    monkeypatch.setattr(notify_module, "send_imessage", lambda receiver, msg, attachment_path=None: calls.append((receiver, msg)) or "sent")

    notify_module.notify_user("hello")

    assert calls == [("5551234567", "hello")]
