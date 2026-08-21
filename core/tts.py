"""
Text-to-Speech engines for MARK XL.

EdgeTTS     – free Microsoft TTS (internet required, no API key)
Kokoro      – fully offline neural TTS (~330 MB model)
ElevenLabs  – cloud API (API key required, best quality)
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import queue as _queue
import subprocess
import sys
import threading
import wave
from contextlib import redirect_stderr
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd



# USE_TF=0 stops transformers from importing TensorFlow (saves 4-8 s startup).
# Do NOT set USE_TORCH or USE_JAX explicitly — forcing those values breaks
# transformers' lazy-loader on certain versions, causing AutoModel and other
# classes to vanish from the public namespace.  Auto-detection is reliable.
os.environ.setdefault("USE_TF",                 "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# ---------------------------------------------------------------------------
# Audio playback helpers
# ---------------------------------------------------------------------------

def _to_numpy(samples) -> np.ndarray:
    """Convert samples to float32 numpy array.

    Handles both numpy arrays and PyTorch tensors (Kokoro >= 0.9).

    PyTorch built against numpy 1.x raises RuntimeError('Numpy is not available')
    when numpy 2.x is installed.  The .tolist() fallback always works regardless
    of PyTorch / numpy version pairing.
    """
    if hasattr(samples, "detach"):                  # PyTorch tensor
        t = samples.detach().cpu().float()
        try:
            return t.numpy()                        # fast path (compatible versions)
        except RuntimeError:
            # PyTorch/numpy version mismatch — convert via Python list (always safe)
            return np.asarray(t.tolist(), dtype=np.float32)
    return np.asarray(samples, dtype=np.float32)


def _compress_silence(
    arr: np.ndarray,
    sample_rate: int    = 24_000,
    max_silence_ms: int = 500,    # cap punctuation pauses — keeps natural rhythm
    threshold: float    = 0.003,  # RMS below this = silence; lower = less clipping
) -> np.ndarray:
    """
    Shorten Kokoro's very long punctuation pauses (1-2 s → ≤500 ms).
    Conservative settings preserve natural prosody; only trims extreme pauses.
    """
    max_samp  = int(max_silence_ms * sample_rate / 1000)
    frame_len = 240                   # ~10 ms at 24 kHz
    out: list[np.ndarray] = []
    silent_acc = 0

    for i in range(0, len(arr), frame_len):
        chunk = arr[i : i + frame_len]
        if np.sqrt(np.mean(chunk ** 2) + 1e-12) < threshold:
            silent_acc += len(chunk)
            if silent_acc <= max_samp:
                out.append(chunk)
        else:
            silent_acc = 0
            out.append(chunk)

    return np.concatenate(out) if out else arr


def reset_audio_output() -> None:
    """Reset any stuck sounddevice stream before new playback starts."""
    try:
        with redirect_stderr(io.StringIO()):
            sd.stop()
    except Exception:
        pass


def _run_sounddevice_call(func: Callable[..., object], *args, **kwargs) -> object | None:
    stderr_capture = io.StringIO()
    try:
        with redirect_stderr(stderr_capture):
            return func(*args, **kwargs)
    except Exception as exc:
        print(f"[TTS] Audio playback failed: {exc}")
        return None


def _is_definitely_compressed(audio_bytes: bytes) -> bool:
    """Check if the bytes start with a header that we are sure is NOT raw PCM."""
    if len(audio_bytes) < 4:
        return False
    # WAV / RIFF
    if audio_bytes.startswith(b"RIFF"):
        return True
    # MP3 ID3 tag
    if audio_bytes.startswith(b"ID3"):
        return True
    # FLAC
    if audio_bytes.startswith(b"fLaC"):
        return True
    # Ogg Vorbis
    if audio_bytes.startswith(b"OggS"):
        return True
    return False


def _play_np(samples, sample_rate: int) -> None:
    """Play float32 mono (or stereo) audio via sounddevice.
    Accepts numpy arrays or PyTorch tensors.
    """
    try:
        reset_audio_output()
        arr = _to_numpy(samples)
        if arr.ndim == 2 and arr.shape[1] == 2:
            arr = arr.mean(axis=1)
        elif arr.ndim > 1 and arr.shape[-1] == 2:
            arr = arr.mean(axis=-1)

        # sounddevice expects a 1-D float32 array for PCM playback.
        # On macOS, we upmix to stereo to avoid AUHAL paramErr (-50).
        if sys.platform == "darwin":
            # (N,) -> (N, 2)
            stereo_samples = np.column_stack((arr, arr))
            data = np.ascontiguousarray(stereo_samples, dtype=np.float32)
        else:
            data = np.ascontiguousarray(arr, dtype=np.float32)

        _run_sounddevice_call(sd.play, data, sample_rate)
        _run_sounddevice_call(sd.wait)
    except Exception as exc:
        print(f"[TTS] Audio playback failed: {exc}")


def _play_audio_bytes(audio_bytes: bytes, sample_rate: int | None = None) -> None:
    """Decode compressed audio bytes and play via sounddevice.

    Primary path uses miniaudio for broad format support.
    Fallback path decodes PCM WAV directly using stdlib wave, so audio output
    can still play even if miniaudio is missing in the runtime environment.
    Raw PCM data is also supported when ElevenLabs returns a byte stream instead
    of a RIFF/WAV container.
    """
    try:
        reset_audio_output()
        import miniaudio

        # 1. Definitely compressed or WAV header? Use miniaudio.
        # 2. No sample rate provided? Must try miniaudio (probably MP3).
        # Otherwise, if sample_rate is provided and no clear header, skip to raw fallback.
        if _is_definitely_compressed(audio_bytes) or sample_rate is None:
            try:
                decoded = miniaudio.decode(
                    audio_bytes,
                    output_format=miniaudio.SampleFormat.FLOAT32,
                    nchannels=1,
                )
                samples = np.array(decoded.samples, dtype=np.float32)
                if samples.ndim == 2 and samples.shape[1] == 2:
                    samples = samples.mean(axis=1)
                elif samples.ndim > 1 and samples.shape[-1] == 2:
                    samples = samples.mean(axis=-1)

                flat_samples = samples.astype(np.float32, copy=False).reshape(-1)
                if sys.platform == "darwin":
                    stereo_samples = np.column_stack((flat_samples, flat_samples))
                    data = np.ascontiguousarray(stereo_samples, dtype=np.float32)
                else:
                    data = np.ascontiguousarray(flat_samples, dtype=np.float32)

                _run_sounddevice_call(sd.play, data, decoded.sample_rate)
                _run_sounddevice_call(sd.wait)
                return
            except Exception as _e:
                # If we were sure it was compressed (had header), log the error.
                # If it was a "last resort" try (sample_rate was None), also log.
                if _is_definitely_compressed(audio_bytes) or sample_rate is None:
                    print(f"[TTS] miniaudio decode/play failed: {_e}")
                # otherwise fall through silently to raw PCM attempt
    except Exception as _e:
        pass # miniaudio import failed or reset_audio failed

    if audio_bytes.startswith(b"RIFF"):
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            sr = wav_file.getframerate()
            ch = wav_file.getnchannels()
            width = wav_file.getsampwidth()
            raw = wav_file.readframes(wav_file.getnframes())

        if width == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError("Unsupported WAV sample width without miniaudio")

        if ch > 1:
            arr = arr.reshape(-1, ch).mean(axis=1)

        arr = arr.astype(np.float32, copy=False).reshape(-1)
        if sys.platform == "darwin":
            stereo_samples = np.column_stack((arr, arr))
            data = np.ascontiguousarray(stereo_samples, dtype=np.float32)
        else:
            data = np.ascontiguousarray(arr, dtype=np.float32)

        _run_sounddevice_call(sd.play, data, sr)
        _run_sounddevice_call(sd.wait)
        return

    # Raw PCM fallback: treat the bytes as 16-bit mono PCM when no WAV header is present.
    if sample_rate is None:
        sample_rate = 16000
    payload = audio_bytes[: len(audio_bytes) - (len(audio_bytes) % 2)]
    if not payload:
        return
    try:
        arr = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
    except ValueError as exc:
        raise RuntimeError("Unsupported raw PCM audio payload") from exc

    arr = arr.astype(np.float32, copy=False).reshape(-1)
    try:
        if sys.platform == "darwin":
            stereo_samples = np.column_stack((arr, arr))
            data = np.ascontiguousarray(stereo_samples, dtype=np.float32)
        else:
            data = np.ascontiguousarray(arr, dtype=np.float32)
        _run_sounddevice_call(sd.play, data, sample_rate)
        _run_sounddevice_call(sd.wait)
    except Exception as exc:
        print(f"[TTS] Audio playback failed: {exc}")



# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

class EdgeTTSEngine:
    """Microsoft EdgeTTS – free, requires internet."""

    def __init__(
        self,
        voice: str = "en-US-GuyNeural",
        audio_sink: Optional[Callable[[bytes], None]] = None,
    ):
        self.voice = voice
        self.audio_sink = audio_sink

    def speak(self, text: str) -> None:
        loop = asyncio.new_event_loop()
        try:
            audio_bytes = loop.run_until_complete(self._synth(text))
        finally:
            loop.close()
        if audio_bytes:
            if self.audio_sink is not None:
                try:
                    import miniaudio
                    decoded = miniaudio.decode(
                        audio_bytes,
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=1,
                    )
                    # miniaudio.decode returns a DecodedAudio object with .samples
                    self.audio_sink(decoded.samples.tobytes())
                except Exception:
                    _play_audio_bytes(audio_bytes)
            else:
                _play_audio_bytes(audio_bytes)

    async def _synth(self, text: str) -> bytes:
        import edge_tts
        comm = edge_tts.Communicate(text, self.voice)
        buf  = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)


# ---------------------------------------------------------------------------
# Kokoro import helper — auto-upgrades on version-mismatch errors
# ---------------------------------------------------------------------------

# Errors that indicate the installed kokoro uses old transformers classes
# (AlbertModel, AutoModel) that are no longer exported at the top level.
_KOKORO_COMPAT_ERRORS = ("AlbertModel", "AutoModel", "cannot import name")


def _import_kokoro_pipeline():
    """Import KPipeline, auto-upgrading kokoro if a version mismatch is found.

    Old kokoro (<0.9) imports AlbertModel / AutoModel from transformers.
    Newer transformers versions no longer export these at the top level,
    causing an ImportError.  kokoro>=0.9 removed these dependencies.

    When the error is detected we:
      1. Upgrade kokoro to >=0.9 via pip (silent, background)
      2. Flush stale kokoro entries from sys.modules
      3. Re-import — this time it should succeed
    """
    import sys

    def _try_import():
        from kokoro import KPipeline  # noqa: PLC0415
        return KPipeline

    try:
        return _try_import()
    except Exception as first_err:
        err_msg = str(first_err)
        if not any(marker in err_msg for marker in _KOKORO_COMPAT_ERRORS):
            # Unrelated error (kokoro not installed, etc.)
            raise RuntimeError(
                f"Kokoro import failed: {first_err}\n"
                "Run: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # ── Version mismatch: upgrade kokoro silently and retry ──────────
        print("[TTS] Kokoro/transformers version mismatch detected — upgrading kokoro…")
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "kokoro>=0.9",
             "--upgrade", "--quiet", "--disable-pip-version-check"],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"Kokoro auto-upgrade failed: {stderr[:200]}\n"
                "Run manually: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # Flush any stale kokoro submodules from the import cache
        stale = [k for k in sys.modules if k == "kokoro" or k.startswith("kokoro.")]
        for key in stale:
            del sys.modules[key]

        print("[TTS] Kokoro upgraded — retrying import…")
        try:
            return _try_import()
        except Exception as retry_err:
            raise RuntimeError(
                f"Kokoro still broken after upgrade: {retry_err}\n"
                "Run manually: pip install --upgrade kokoro transformers"
            ) from retry_err


# Kokoro voice prefix → KPipeline lang_code mapping
_KOKORO_LANG_CODES = {
    "a": "a",   # American English  (af_*, am_*)
    "b": "b",   # British English   (bf_*, bm_*)
    "j": "j",   # Japanese          (jf_*, jm_*)
    "z": "z",   # Mandarin Chinese  (zf_*, zm_*)
    "s": "s",   # Spanish           (sf_*, sm_*)
    "f": "f",   # French            (ff_*, fm_*)
    "h": "h",   # Hindi             (hf_*, hm_*)
    "i": "i",   # Italian           (if_*, im_*)
    "p": "p",   # Brazilian Portuguese
    "r": "r",   # Russian           (rf_*, rm_*)
    "e": "e",   # German            (ef_*, em_*)
}


class KokoroTTSEngine:
    """Fully offline Kokoro neural TTS.

    Model (~330 MB) is downloaded from HuggingFace on first use,
    then cached locally — subsequent starts load from disk.

    Warmup strategy: _init() runs synchronously in the background
    _do_tts() thread (not the UI thread).  After the pipeline loads,
    a dummy inference compiles the PyTorch JIT graph immediately so
    the first real speak() call has zero compilation overhead.
    """

    def __init__(
        self,
        voice: str = "af_heart",
        speed: float = 1.0,
        audio_sink: Optional[Callable[[bytes], None]] = None,
    ):
        self.voice      = voice
        self.speed      = speed
        self.audio_sink = audio_sink
        self._pipeline  = None
        self._lock      = threading.Lock()
        self._init()   # blocking, but called from background thread

    @property
    def _lang_code(self) -> str:
        prefix = self.voice[0].lower() if self.voice else "a"
        return _KOKORO_LANG_CODES.get(prefix, "a")

    def _init(self) -> None:
        if self._pipeline is not None:
            return

        lang = self._lang_code

        # Prefer GPU — Kokoro on CUDA is ~10x faster than CPU.
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cpu":
                import os as _os
                n_threads = max(1, min(4, (_os.cpu_count() or 4) // 2))
                try:
                    torch.set_num_threads(n_threads)
                    torch.set_num_interop_threads(2)
                except RuntimeError:
                    pass
                print(
                    f"[TTS] Kokoro on CPU — for faster speech install CUDA PyTorch:\n"
                    "      pip install torch --index-url https://download.pytorch.org/whl/cu118"
                )
        except Exception:
            device = "cpu"

        print(f"[TTS] Kokoro — loading (lang='{lang}', device='{device}')…")

        KPipeline = _import_kokoro_pipeline()

        def _create_pipeline():
            try:
                return KPipeline(lang_code=lang, device=device)
            except TypeError:
                return KPipeline(lang_code=lang)   # older build — no device param

        try:
            self._pipeline = _create_pipeline()
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print("[TTS] Kokoro model not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._pipeline = _create_pipeline()
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Kokoro model download failed.\n"
                        f"Internet access is required the first time to download the voice model (~330 MB).\n"
                        f"After the first download it runs fully offline.\n"
                        f"Tip: Switch to EdgeTTS (free, no download) in the Configure panel if offline.\n"
                        f"Details: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        print("[TTS] Kokoro compiling (first-time only)…")
        # Warmup: compiles PyTorch JIT graph so first real speak() call is instant.
        try:
            for _ in self._pipeline("hello", voice=self.voice, speed=self.speed):
                pass
            print("[TTS] Kokoro ready.")
        except Exception as e:
            print(f"[TTS] Kokoro warmup warning: {e}")

    def speak(self, text: str) -> None:
        with self._lock:
            if self._pipeline is None:
                self._init()

        # ── Concurrent synthesise + playback ────────────────────────────────
        # Kokoro generates audio chunks lazily.  Without threading, we:
        #   synthesise chunk N → play N → synthesise N+1 → play N+1 …
        # With a producer/consumer pair, chunk N+1 synthesises WHILE chunk N
        # plays, cutting perceived latency by the playback duration of all but
        # the last chunk (typically 1-3 s on multi-sentence responses).
        audio_q: "_queue.Queue[np.ndarray | None]" = _queue.Queue(maxsize=4)
        synth_error: list[Exception] = []

        def _synth():
            try:
                for _, _, audio in self._pipeline(text, voice=self.voice, speed=self.speed):
                    if audio is not None:
                        arr = _to_numpy(audio)
                        arr = _compress_silence(arr)
                        if arr.size > 0:
                            audio_q.put(arr)          # blocks if player is slow (backpressure)
            except Exception as exc:
                synth_error.append(exc)
            finally:
                audio_q.put(None)                     # sentinel → player exits

        synth_thread = threading.Thread(target=_synth, daemon=True)
        synth_thread.start()

        # Player runs in this thread so sd.wait() doesn't block the synth thread.
        while True:
            arr = audio_q.get()
            if arr is None:
                break
            if self.audio_sink is not None:
                # Kokoro emits float32; convert to 16-bit PCM for the sink
                pcm_bytes = (arr * 32767).astype(np.int16).tobytes()
                self.audio_sink(pcm_bytes)
            else:
                _play_np(arr, 24000)

        synth_thread.join()

        if synth_error:
            raise synth_error[0]


class ElevenLabsTTSEngine:
    """ElevenLabs cloud TTS – API key required."""

    _DEFAULT_FALLBACK_VOICES = (
        "pNInz6obpgDQGcFmaJgB",
        "JBFqnCBsd6RMkjVDRZzb",
    )

    def __init__(
        self,
        api_key: str,
        voice_id: str = "pNInz6obpgDQGcFmaJgB",
        audio_sink: Optional[Callable[[bytes], None]] = None,
    ):
        if not (api_key or "").strip():
            raise ValueError("Missing elevenlabs_api_key in config/api_keys.json")
        self.api_key  = api_key
        self.voice_id = (voice_id or "").strip() or "pNInz6obpgDQGcFmaJgB"
        self.audio_sink = audio_sink

    def _voice_candidates(self) -> list[str]:
        seen: set[str] = set()
        candidates: list[str] = []
        for voice_id in [self.voice_id, *self._DEFAULT_FALLBACK_VOICES]:
            if voice_id and voice_id not in seen:
                seen.add(voice_id)
                candidates.append(voice_id)
        return candidates

    def speak(self, text: str) -> None:
        import requests

        headers = {
            "xi-api-key":   self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text":     text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

        last_error: Exception | None = None
        for voice_id in self._voice_candidates():
            try:
                # We use 24kHz PCM for consistency with the rest of MARK XL's audio loop.
                resp = requests.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000",
                    json=payload,
                    headers=headers,
                    timeout=60,
                )
                resp.raise_for_status()
                self.voice_id = voice_id
                # ElevenLabs bills per character and this fires once per spoken
                # sentence, making it the largest ongoing cost in the system.
                try:
                    from memory.usage_log import record_usage
                    record_usage("elevenlabs_tts", "elevenlabs_tts", len(text or ""))
                except Exception:
                    pass
                if self.audio_sink is not None:
                    print(f"[TTS] ElevenLabs audio generated, sending to sink (len={len(resp.content)})")
                    self.audio_sink(resp.content)
                else:
                    _play_audio_bytes(resp.content, sample_rate=24000)
                return
            except Exception as exc:
                last_error = exc
                if exc.__class__.__name__ == "HTTPError" and getattr(getattr(exc, "response", None), "status_code", None) in {400, 401, 404, 422}:
                    print(f"[TTS] ElevenLabs voice '{voice_id}' rejected: {exc}")
                else:
                    print(f"[TTS] ElevenLabs request failed for '{voice_id}': {exc}")

        raise RuntimeError(f"ElevenLabs TTS failed for all candidate voices: {last_error}") from last_error


class ClonedVoiceEngine:
    """Fully offline voice clone: Kokoro (fast, local, already-offline TTS)
    synthesizes speech, then a locally-trained RVC voice-conversion model
    reshapes it to sound like Jarvis's original ElevenLabs voice. No network
    call — the RVC model was trained once, elsewhere (see voice_clone/), and
    only the small resulting .pth/.index files live on this machine.

    v1 deliberately converts each full utterance in one pass rather than
    streaming per-chunk like KokoroTTSEngine does on its own — RVC conversion
    on tiny chunks risks pitch/click artifacts at chunk boundaries. This adds
    a "thinking" pause (synthesis + conversion time) before Jarvis starts
    speaking, traded for correctness/robustness in this first version.
    """

    def __init__(
        self,
        model_path: str,
        index_path: str = "",
        kokoro_voice: str = "af_heart",
        kokoro_speed: float = 1.0,
        device: str = "cpu",
        audio_sink: Optional[Callable[[bytes], None]] = None,
    ):
        model_file = Path(model_path)
        if not model_file.exists():
            raise ValueError(
                f"RVC model not found at {model_path}. Train it via "
                "voice_clone/train_rvc_colab.ipynb and place the .pth/.index "
                "files in voice_clone/rvc_models/ first."
            )

        try:
            from rvc_python.infer import RVCInference
        except Exception as exc:
            raise RuntimeError(
                "rvc-python is not installed. Run: pip install rvc-python"
            ) from exc

        self.audio_sink = audio_sink
        self._kokoro = KokoroTTSEngine(voice=kokoro_voice, speed=kokoro_speed)

        self._rvc = RVCInference(device=device)
        try:
            if index_path and Path(index_path).exists():
                self._rvc.load_model(str(model_file), str(index_path))
            else:
                self._rvc.load_model(str(model_file))
        except TypeError:
            # Some rvc-python versions only accept a single model-path arg.
            self._rvc.load_model(str(model_file))

    def speak(self, text: str) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = str(Path(tmp_dir) / "kokoro_raw.wav")
            converted_path = str(Path(tmp_dir) / "jarvis_cloned.wav")

            self._synthesize_to_wav(text, raw_path)
            self._rvc.infer_file(raw_path, converted_path)

            audio_bytes = Path(converted_path).read_bytes()
            if self.audio_sink is not None:
                self.audio_sink(audio_bytes)
            else:
                _play_audio_bytes(audio_bytes)

    def _synthesize_to_wav(self, text: str, out_path: str) -> None:
        """Run Kokoro's pipeline directly (bypassing its own playback/sink
        path) and write the full utterance to a single WAV file."""
        chunks: list[np.ndarray] = []
        with self._kokoro._lock:
            if self._kokoro._pipeline is None:
                self._kokoro._init()
            for _, _, audio in self._kokoro._pipeline(
                text, voice=self._kokoro.voice, speed=self._kokoro.speed
            ):
                if audio is not None:
                    chunks.append(_to_numpy(audio))

        arr = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        pcm16 = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(out_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm16.tobytes())


# ---------------------------------------------------------------------------
# Thread-safe player wrapper
# ---------------------------------------------------------------------------

class TTSPlayer:
    """
    Wraps any *Engine. Exposes a blocking speak() method
    meant to be called from a dedicated background thread.
    """

    def __init__(self, engine):
        self._engine  = engine
        self._playing = False
        self._lock    = threading.Lock()

    @property
    def is_playing(self) -> bool:
        return self._playing

    def speak(
        self,
        text:     str,
        on_start: Optional[Callable] = None,
        on_done:  Optional[Callable] = None,
    ) -> None:
        """Synthesise and play text. BLOCKING – call from a dedicated thread."""
        try:
            with self._lock:
                self._playing = True
            if on_start:
                on_start()
            self._engine.speak(text)
        except Exception as e:
            print(f"[TTS] Error: {e}")
        finally:
            with self._lock:
                self._playing = False
            if on_done:
                on_done()

    def stop(self) -> None:
        sd.stop()
        with self._lock:
            self._playing = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_tts_player(
    config: dict,
    *,
    audio_sink: Optional[Callable[[bytes], None]] = None,
) -> TTSPlayer:
    engine_name = config.get("tts_engine", "edgetts").lower()
    if engine_name == "kokoro":
        voice  = config.get("tts_voice", "af_heart")
        speed  = float(config.get("tts_speed", 1.0))
        engine = KokoroTTSEngine(
            voice=voice,
            speed=speed,
            audio_sink=audio_sink,
        )
    elif engine_name == "elevenlabs":
        api_key  = config.get("elevenlabs_api_key", "")
        voice_id = config.get("tts_voice", "pNInz6obpgDQGcFmaJgB")
        engine   = ElevenLabsTTSEngine(
            api_key=api_key,
            voice_id=voice_id,
            audio_sink=audio_sink,
        )
    elif engine_name == "cloned_voice":
        base_dir = Path(config.get("cloned_voice_dir", "voice_clone/rvc_models"))
        engine = ClonedVoiceEngine(
            model_path=str(base_dir / "jarvis.pth"),
            index_path=str(base_dir / "jarvis.index"),
            kokoro_voice=config.get("cloned_voice_base", "af_heart"),
            device=config.get("cloned_voice_device", "cpu"),
            audio_sink=audio_sink,
        )
    else:   # edgetts (default)
        voice  = config.get("tts_voice", "en-US-GuyNeural")
        engine = EdgeTTSEngine(
            voice=voice,
            audio_sink=audio_sink,
        )
    return TTSPlayer(engine)
