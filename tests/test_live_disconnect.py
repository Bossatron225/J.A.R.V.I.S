import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
