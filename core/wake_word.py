"""Local, opt-in wake-word detection.

Design constraints, in priority order — this component listens to a
microphone in a private home, so the failure modes matter more than the
feature:

1. DETECTION IS LOCAL OR IT DOES NOT HAPPEN. Audio is never sent anywhere to
   decide whether the wake word was spoken. The pre-existing speech listener
   in main.py used speech_recognition's recognize_google(), which uploads
   every captured phrase to a cloud API continuously — i.e. an always-on room
   microphone streaming to a third party. Nothing here may do that.

2. FAIL CLOSED. If no local engine is installed, wake-word listening stays
   OFF and says so. It must never silently degrade to a cloud recogniser,
   because the degraded mode is precisely the privacy problem.

3. OFF BY DEFAULT. Requires an explicit `wake_word_enabled: true` in
   config/api_keys.json. Installing this code changes nothing on its own.

4. IT IS NOT AN AUTHENTICATOR. Hearing "Jarvis" proves someone is in the
   room, not that it is the owner. Detection stays gated behind the biometric
   lock, so a voice cannot be used to reach a locked command surface.

5. NO AUDIO IS RETAINED. Frames are processed and discarded; nothing is
   written to disk.

Enabling it requires a local engine, installed deliberately:
    pip install openwakeword        # ships a pretrained "hey jarvis" model
then set "wake_word_enabled": true in config/api_keys.json.
"""
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

DEFAULT_MODEL = "hey_jarvis"
DEFAULT_THRESHOLD = 0.6

# 80ms at 16kHz mono — openwakeword's expected frame size.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280


def _config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def local_engine_available() -> tuple[bool, str]:
    """Is a LOCAL wake-word engine importable? Cloud recognisers deliberately
    do not count — see constraint 1."""
    try:
        import openwakeword  # noqa: F401
        return True, "openwakeword"
    except Exception:
        return False, ""


def is_enabled(config: dict | None = None) -> bool:
    cfg = config if config is not None else _config()
    return bool(cfg.get("wake_word_enabled", False))


def status(config: dict | None = None) -> str:
    cfg = config if config is not None else _config()
    available, engine = local_engine_available()
    if not is_enabled(cfg):
        return "Wake word is disabled (set wake_word_enabled: true to turn it on)."
    if not available:
        return (
            "Wake word is enabled in config but NO local engine is installed, so it is "
            "not listening. Install one (pip install openwakeword). Cloud speech "
            "recognition is deliberately not used as a fallback."
        )
    return f"Wake word active using local engine '{engine}'."


class WakeWordDetector:
    """Runs a local wake-word model over microphone frames.

    `on_wake` fires only when the model exceeds the configured confidence AND
    `gate` (if supplied) returns False. `gate` is how the biometric lock keeps
    a voice from reaching a locked command surface — see constraint 4."""

    def __init__(self, on_wake, gate=None, config: dict | None = None):
        self._on_wake = on_wake
        self._gate = gate
        self._cfg = config if config is not None else _config()
        self._model = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""

    # ── lifecycle ─────────────────────────────────────────────────────────
    def can_start(self) -> tuple[bool, str]:
        if not is_enabled(self._cfg):
            return False, "wake word disabled in config"
        available, engine = local_engine_available()
        if not available:
            return False, "no local wake-word engine installed (refusing cloud fallback)"
        return True, engine

    def start(self) -> tuple[bool, str]:
        ok, detail = self.can_start()
        if not ok:
            self.last_error = detail
            return False, detail
        if self._thread and self._thread.is_alive():
            return True, "already running"

        try:
            from openwakeword.model import Model
            model_name = str(self._cfg.get("wake_word_model", DEFAULT_MODEL))
            self._model = Model(wakeword_models=[model_name])
        except Exception as exc:
            self.last_error = f"could not load local model: {exc}"
            return False, self.last_error

        self._stop.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="wake-word")
        self._thread.start()
        return True, detail

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    # ── detection ─────────────────────────────────────────────────────────
    def process_frame(self, frame) -> bool:
        """Score one audio frame. Returns True if the wake word fired and the
        gate permitted it. Separated from the audio loop so it is testable
        without a microphone."""
        if self._model is None:
            return False
        try:
            scores = self._model.predict(frame)
        except Exception:
            return False

        threshold = float(self._cfg.get("wake_word_threshold", DEFAULT_THRESHOLD))
        triggered = any(float(v) >= threshold for v in (scores or {}).values())
        if not triggered:
            return False

        # Gate AFTER detection: a locked Jarvis must not act on a voice, but we
        # still want the attempt visible in logs rather than silently dropped.
        if self._gate is not None and self._gate():
            print("[WakeWord] heard, but ignored — command surface is locked.")
            return False

        try:
            self._on_wake()
        except Exception as exc:
            print(f"[WakeWord] handler error: {exc}")
        return True

    def _listen_loop(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:
            self.last_error = f"audio input unavailable: {exc}"
            return

        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=FRAME_SAMPLES) as stream:
                while not self._stop.is_set():
                    data, _ = stream.read(FRAME_SAMPLES)
                    # Processed and dropped immediately — constraint 5.
                    self.process_frame(np.squeeze(data))
        except Exception as exc:
            self.last_error = f"listen loop stopped: {exc}"
