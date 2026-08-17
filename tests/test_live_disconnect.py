import asyncio
import sys
from pathlib import Path

import os

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_module
from main import JarvisLive


class DummyUI:
    def __init__(self) -> None:
        self.state = None
        self.vps_status = None

    def set_state(self, value: str) -> None:
        self.state = value

    def set_vps_status(self, text: str, level: str = "neutral", tooltip: str | None = None) -> None:
        self.vps_status = (text, level, tooltip)

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


def test_embedded_vps_brain_waits_longer_for_first_live_session() -> None:
    local = JarvisLive(DummyUI())
    embedded = JarvisLive(DummyUI(), remote_bridge=object())

    assert local._dashboard_session_wait_steps() == 80
    assert embedded._dashboard_session_wait_steps() == 300


def test_headless_optional_message_monitors_exit_without_callbacks(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI(), remote_bridge=object())
    monkeypatch.setattr(main_module, 'poll_imessage_alerts', None)
    monkeypatch.setattr(main_module, 'get_imessage_monitor_interval', None)
    monkeypatch.setattr(main_module, 'poll_mail_alerts', None)
    monkeypatch.setattr(main_module, 'get_mail_monitor_interval', None)

    asyncio.run(jarvis._run_imessage_monitor())
    asyncio.run(jarvis._run_mail_monitor())


def test_unavailable_headless_tool_returns_capability_message(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI(), remote_bridge=object())
    monkeypatch.setattr(main_module, 'open_app', None)

    call = type('Call', (), {'id': 'call-1', 'name': 'open_app', 'args': {'app_name': 'Safari'}})()
    response = asyncio.run(jarvis._execute_tool(call))

    assert response.response['unavailable'] is True
    assert 'headless VPS' in response.response['result']


def test_headless_safe_capabilities_are_restored() -> None:
    expected = (
        main_module.weather_action,
        main_module.web_search_action,
        main_module.google_calendar,
        main_module.send_message,
        main_module.youtube_video,
        main_module.code_helper,
        main_module.workspace_agent,
        main_module.get_system_status,
    )

    assert all(callable(handler) for handler in expected)


def test_remote_apple_tool_returns_delegated_mac_result() -> None:
    class FakeBridge:
        def request_local_action(self, action, payload, timeout):
            assert action == 'imessage_control'
            assert payload == {'action': 'read_unread', 'limit': 2}
            assert timeout == 45.0
            return {'status': 'completed', 'result': 'Unread iMessages: two messages'}

    jarvis = JarvisLive(DummyUI(), remote_bridge=FakeBridge())
    call = type('Call', (), {
        'id': 'call-imessage',
        'name': 'imessage_control',
        'args': {'action': 'read_unread', 'limit': 2},
    })()

    response = asyncio.run(jarvis._execute_tool(call))

    assert response.response['result']['status'] == 'completed'
    assert 'two messages' in response.response['result']['result']


def test_remote_camera_tool_requests_logged_in_device_frame() -> None:
    events = []

    class FakeBridge:
        def publish_to_latest_client(self, event):
            events.append(event)
            return 'client-1'

    jarvis = JarvisLive(DummyUI(), remote_bridge=FakeBridge())
    call = type('Call', (), {
        'id': 'call-camera',
        'name': 'screen_process',
        'args': {'target_type': 'camera', 'text': 'Read the label in view.'},
    })()

    response = asyncio.run(jarvis._execute_tool(call))

    assert response.response['pending'] is True
    assert events == [{
        'type': 'camera_capture_request',
        'prompt': 'Read the label in view.',
        'request_id': 'camera-call-camera',
    }]


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


def test_service_is_accepted_as_wake_phrase(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())
    got = {"called": False}

    def fake_on_text_command(text: str) -> None:
        got["called"] = True
        got["text"] = text

    monkeypatch.setattr(jarvis, "_on_text_command", fake_on_text_command)

    class FakeRecognizer:
        @staticmethod
        def recognize_google(audio, language):
            return "service"

    jarvis._wake_phrases = ["jarvis", "service", "jarvis wake", "wake up jarvis", "hey jarvis"]
    jarvis._handle_speech_callback(FakeRecognizer(), object())

    assert got["called"] is True
    assert got["text"] == "service"


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
    assert "Open:" in message
    assert "Key:" in message
    assert "Auto:" in message
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
    monkeypatch.delenv("JARVIS_VPS_URL", raising=False)

    launched = jarvis._launch_local_jarvis_if_needed()

    assert launched is True
    assert calls
    assert os.path.basename(calls[0][0][0]).startswith("python")
    assert str(calls[0][0][-1]).endswith("main.py")


def test_launch_local_jarvis_if_needed_prefers_vps_when_configured(monkeypatch) -> None:
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

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, *_args, **_kwargs):
            return b'{"ok": true, "status": "online"}'

    monkeypatch.setattr(main_module, "_subprocess", FakeSubprocess())
    monkeypatch.setattr(main_module._platform, "system", lambda: "Darwin")
    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")
    monkeypatch.setattr(main_module.urllib.request, "urlopen", lambda *args, **kwargs: FakeResp())

    launched = jarvis._launch_local_jarvis_if_needed()

    assert launched is False
    assert calls == []


def test_local_speech_is_blocked_when_vps_is_healthy(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())
    jarvis.session = object()
    jarvis._loop = object()
    calls = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, *_args, **_kwargs):
            return b'{"ok": true, "status": "online"}'

    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")
    monkeypatch.setattr(main_module.urllib.request, "urlopen", lambda *args, **kwargs: FakeResp())
    monkeypatch.setattr(main_module.asyncio, "run_coroutine_threadsafe", lambda *args, **kwargs: calls.append((args, kwargs)))

    jarvis.speak("hello")

    assert calls == []
    assert jarvis.ui.vps_status is not None
    assert "remote voice active" in jarvis.ui.vps_status[0].lower()


def test_main_keeps_gui_ui_by_default_when_vps_is_configured(monkeypatch) -> None:
    created = []

    class FakeHeadlessUI:
        class Root:
            def mainloop(self):
                pass

        def __init__(self):
            created.append("headless")
            self.root = self.Root()

        def wait_for_api_key(self):
            return None

        def write_log(self, *_args, **_kwargs):
            return None

        def __getattr__(self, name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    class FakeGuiUI:
        def __init__(self, *args, **kwargs):
            created.append("gui")
            self.root = type("Root", (), {"mainloop": lambda self: None})()

        def wait_for_api_key(self):
            return None

    monkeypatch.delenv("JARVIS_HEADLESS", raising=False)
    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")
    monkeypatch.setattr(main_module, "JarvisUI", FakeGuiUI)
    monkeypatch.setattr(main_module, "_HeadlessUI", FakeHeadlessUI)

    class FakeThread:
        def __init__(self, target=None, daemon=False, **kwargs):
            self.target = target
            self.daemon = daemon
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(main_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(main_module.asyncio, "run", lambda coro: None)

    main_module.main()

    assert created == ["gui"]


def test_main_uses_headless_ui_when_explicitly_requested(monkeypatch) -> None:
    created = []

    class FakeHeadlessUI:
        class Root:
            def mainloop(self):
                pass

        def __init__(self):
            created.append("headless")
            self.root = self.Root()

        def wait_for_api_key(self):
            return None

        def write_log(self, *_args, **_kwargs):
            return None

        def __getattr__(self, name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    class FakeGuiUI:
        def __init__(self, *args, **kwargs):
            created.append("gui")
            self.root = type("Root", (), {"mainloop": lambda self: None})()

        def wait_for_api_key(self):
            return None

    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")
    monkeypatch.setenv("JARVIS_HEADLESS", "1")
    monkeypatch.setattr(main_module, "JarvisUI", FakeGuiUI)
    monkeypatch.setattr(main_module, "_HeadlessUI", FakeHeadlessUI)

    class FakeThread:
        def __init__(self, target=None, daemon=False, **kwargs):
            self.target = target
            self.daemon = daemon
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(main_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(main_module.asyncio, "run", lambda coro: None)

    main_module.main()

    assert created == ["headless"]


def test_vps_headless_mode_blocks_shutdown(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())
    monkeypatch.setenv("JARVIS_VPS_URL", "https://vps.example.com")
    monkeypatch.setenv("JARVIS_HEADLESS", "1")
    seen = []

    def fake_exit(code):
        seen.append(code)
        raise AssertionError("shutdown should be blocked in VPS mode")

    monkeypatch.setattr(main_module.os, "_exit", fake_exit)

    result = jarvis._schedule_shutdown("tool: shutdown_jarvis")

    assert result is False
    assert seen == []


def test_on_dashboard_wake_thread_safe_scheduling(monkeypatch) -> None:
    jarvis = JarvisLive(DummyUI())
    
    # Mock loop and check if call_soon_threadsafe is called
    calls = []
    class FakeLoop:
        def is_running(self):
            return True
        def call_soon_threadsafe(self, fn, *args):
            calls.append((fn, args))
            
    jarvis._loop = FakeLoop()
    jarvis._on_dashboard_wake()
    
    # Assert that call_soon_threadsafe was called with self._wake_event.set
    assert len(calls) == 1
    assert calls[0][0] == jarvis._wake_event.set

