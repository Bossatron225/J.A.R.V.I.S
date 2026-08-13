import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_module
from main import JarvisLive


class DummyUI:
    def __init__(self) -> None:
        self.state = None

    def set_state(self, value: str) -> None:
        self.state = value

    def write_log(self, *_args, **_kwargs) -> None:
        pass

    def notify_phone_connected(self) -> None:
        pass


def test_disconnect_error_is_detected() -> None:
    jarvis = JarvisLive(DummyUI())

    class FakeClosedError(Exception):
        pass

    exc = FakeClosedError("sent 1011 (internal error) keepalive ping timeout; no close frame received")
    assert jarvis._is_disconnect_error(exc) is True

    assert jarvis._is_disconnect_error(RuntimeError("temporary network hiccup")) is False


def test_billing_error_is_not_treated_as_disconnect() -> None:
    jarvis = JarvisLive(DummyUI())

    exc = RuntimeError("APIError: 1011 None. Your prepayment credits are depleted. Please go to AI Studio")
    assert jarvis._is_disconnect_error(exc) is False
    assert jarvis._is_billing_error(exc) is True


def test_wake_protocol_accepts_any_sender_when_sender_is_unset() -> None:
    jarvis = JarvisLive(DummyUI())
    jarvis._wake_protocol_cfg = {"enabled": True, "sender": "", "phrase": "jarvis wake"}

    assert jarvis._is_authorized_wake_sender("James", "James") is True
    assert jarvis._is_authorized_wake_sender("+3531234567", "James") is True


def test_receive_audio_disconnect_does_not_print_traceback(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())

    class FakeIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("ConnectionClosedError: sent 1011 (internal error)")

    class FakeSession:
        def receive(self):
            return FakeIterator()

    jarvis.session = FakeSession()
    tracebacks = []

    def fake_print_exc() -> None:
        tracebacks.append("traceback")

    monkeypatch.setattr("main.traceback.print_exc", fake_print_exc)

    asyncio.run(jarvis._receive_audio())

    assert jarvis.session is None
    assert tracebacks == []


def test_wake_message_includes_remote_url_and_access_key() -> None:
    jarvis = JarvisLive(DummyUI())

    class FakeDashboard:
        def new_key(self):
            return "REMOTE-KEY-123"

        def get_remote_url(self):
            return "https://remote.example.com"

        def get_auto_login_url(self, key):
            return f"https://remote.example.com/auto-login?key={key}"

        def get_remote_security_status(self):
            return "SECURITY: PUBLIC=ON | PIN=OFF"

    jarvis._dashboard = FakeDashboard()
    jarvis.session = None

    message = jarvis._build_wake_remote_access_message()

    assert "https://remote.example.com" in message
    assert "REMOTE-KEY-123" in message
    assert "Remote access:" in message
    assert "Key:" in message
    assert "auto-login" in message.lower()
    assert "PUBLIC=ON" in message


def test_launch_local_jarvis_if_needed_uses_python_entry(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())
    jarvis.session = None
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        class DummyProc:
            pass
        return DummyProc()

    class FakeSubprocess:
        DEVNULL = None

        @staticmethod
        def Popen(*args, **kwargs):
            return fake_popen(*args, **kwargs)

    monkeypatch.setattr(main_module, "_subprocess", FakeSubprocess())
    monkeypatch.setattr(main_module._platform, "system", lambda: "Darwin")

    launched = jarvis._launch_local_jarvis_if_needed()

    assert launched is True
    assert calls and calls[0][0][0].endswith("python")
    assert str(calls[0][0][-1]).endswith("main.py")
