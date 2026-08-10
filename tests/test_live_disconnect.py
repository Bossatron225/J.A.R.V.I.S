import pytest

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
