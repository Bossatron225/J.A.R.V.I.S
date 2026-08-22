"""Wake-word tests focused on the SAFETY properties, not the ML.

Detection quality is the engine's problem. What must be guaranteed here is
that this never becomes an always-on microphone streaming a private room to a
cloud API, and never lets a voice past the biometric lock.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import wake_word as ww


class _FakeModel:
    def __init__(self, score: float):
        self._score = score

    def predict(self, frame):
        return {"hey_jarvis": self._score}


def _detector(score=0.9, gate=None, config=None, on_wake=None):
    fired = []
    det = ww.WakeWordDetector(
        on_wake=on_wake or (lambda: fired.append(True)),
        gate=gate,
        config=config if config is not None else {"wake_word_enabled": True},
    )
    det._model = _FakeModel(score)
    det._fired = fired
    return det


# ── fail closed ────────────────────────────────────────────────────────────

def test_disabled_by_default():
    """Adding this code must change nothing until explicitly enabled."""
    assert ww.is_enabled({}) is False


def test_refuses_to_start_when_disabled():
    det = ww.WakeWordDetector(on_wake=lambda: None, config={"wake_word_enabled": False})
    ok, reason = det.can_start()
    assert ok is False
    assert "disabled" in reason


def test_refuses_to_start_without_a_local_engine(monkeypatch):
    """The critical property: with no local engine it must STOP, never fall
    back to a cloud recogniser — the fallback is the privacy problem."""
    monkeypatch.setattr(ww, "local_engine_available", lambda: (False, ""))
    det = ww.WakeWordDetector(on_wake=lambda: None, config={"wake_word_enabled": True})
    ok, reason = det.can_start()
    assert ok is False
    assert "no local" in reason.lower()
    assert "refusing cloud fallback" in reason


def test_status_is_explicit_when_engine_missing(monkeypatch):
    monkeypatch.setattr(ww, "local_engine_available", lambda: (False, ""))
    text = ww.status({"wake_word_enabled": True})
    assert "not listening" in text
    assert "Cloud speech recognition is deliberately not used" in text


def test_local_engine_check_does_not_count_cloud_recognisers():
    """speech_recognition is installed but is a CLOUD recogniser; it must not
    satisfy the local-engine requirement."""
    available, engine = ww.local_engine_available()
    assert engine != "speech_recognition"
    if available:
        assert engine == "openwakeword"


# ── the lock gate ──────────────────────────────────────────────────────────

def test_wake_word_does_not_fire_while_locked():
    """Hearing a name proves someone is in the room, not that it is the owner."""
    det = _detector(score=0.9, gate=lambda: True)  # locked
    assert det.process_frame(object()) is False
    assert det._fired == []


def test_wake_word_fires_when_unlocked():
    det = _detector(score=0.9, gate=lambda: False)
    assert det.process_frame(object()) is True
    assert det._fired == [True]


def test_wake_word_fires_with_no_gate_configured():
    det = _detector(score=0.9, gate=None)
    assert det.process_frame(object()) is True


# ── detection behaviour ────────────────────────────────────────────────────

def test_below_threshold_does_not_fire():
    det = _detector(score=0.2)
    assert det.process_frame(object()) is False


def test_threshold_is_configurable():
    det = _detector(score=0.5, config={"wake_word_enabled": True, "wake_word_threshold": 0.4})
    assert det.process_frame(object()) is True


def test_no_model_loaded_never_fires():
    det = ww.WakeWordDetector(on_wake=lambda: None, config={"wake_word_enabled": True})
    assert det.process_frame(object()) is False


def test_model_errors_are_swallowed():
    class _Explodes:
        def predict(self, frame):
            raise RuntimeError("bad frame")

    det = _detector()
    det._model = _Explodes()
    assert det.process_frame(object()) is False


def test_handler_exception_does_not_propagate():
    det = _detector(score=0.9, on_wake=lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert det.process_frame(object()) is True  # fired, error contained
