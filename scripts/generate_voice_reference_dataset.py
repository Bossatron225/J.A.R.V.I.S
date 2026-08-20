"""
One-time local script: captures Jarvis's current ElevenLabs voice as a WAV
dataset for training an offline RVC voice-conversion model (see voice_clone/).

Usage:
    python scripts/generate_voice_reference_dataset.py --yes

Reads config/api_keys.json for the elevenlabs_api_key and tts_voice already
configured for Jarvis (core/tts.py:ElevenLabsTTSEngine uses the same voice),
synthesizes every line in voice_clone/corpus.txt through the ElevenLabs API,
and saves each as a WAV file under voice_clone/dataset/raw/. The whole folder
is then zipped for upload to the Colab training notebook.

This is the only step in the local voice-clone setup that calls ElevenLabs —
it costs ElevenLabs credits, hence the required --yes confirmation.
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
import zipfile
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).resolve().parent.parent
API_CONFIG  = BASE_DIR / "config" / "api_keys.json"
CORPUS_PATH = BASE_DIR / "voice_clone" / "corpus.txt"
DATASET_DIR = BASE_DIR / "voice_clone" / "dataset"
RAW_DIR     = DATASET_DIR / "raw"
ZIP_PATH    = DATASET_DIR / "jarvis_voice_reference.zip"

SAMPLE_RATE = 24000
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM, matches ElevenLabs pcm_24000 output


def load_corpus(path: Path = CORPUS_PATH) -> list[str]:
    if not path.exists():
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a WAV container."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buf.getvalue()


def _load_elevenlabs_config() -> tuple[str, str]:
    config = json.loads(API_CONFIG.read_text(encoding="utf-8"))
    api_key  = str(config.get("elevenlabs_api_key") or "").strip()
    voice_id = str(config.get("tts_voice") or "").strip()
    if not api_key:
        raise SystemExit("No elevenlabs_api_key configured in config/api_keys.json.")
    if not voice_id:
        raise SystemExit("No tts_voice configured in config/api_keys.json.")
    return api_key, voice_id


def _synthesize(api_key: str, voice_id: str, text: str) -> bytes:
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000",
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def generate_dataset(confirmed: bool) -> None:
    lines = load_corpus()
    if not lines:
        raise SystemExit(f"No corpus lines found at {CORPUS_PATH}.")

    total_chars = sum(len(line) for line in lines)
    print(f"[VoiceClone] {len(lines)} sentences, {total_chars} characters total.")
    print("[VoiceClone] This will call the ElevenLabs API and consume ElevenLabs credits.")
    if not confirmed:
        print("[VoiceClone] Re-run with --yes to proceed.")
        return

    api_key, voice_id = _load_elevenlabs_config()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for i, line in enumerate(lines, start=1):
        out_path = RAW_DIR / f"{i:03d}.wav"
        print(f"[VoiceClone] ({i}/{len(lines)}) {line[:60]}")
        try:
            pcm = _synthesize(api_key, voice_id, line)
            out_path.write_bytes(wrap_pcm_as_wav(pcm))
        except Exception as e:
            print(f"[VoiceClone] ⚠️ Failed on line {i}: {e}")

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for wav_file in sorted(RAW_DIR.glob("*.wav")):
            zf.write(wav_file, arcname=wav_file.name)

    print(f"[VoiceClone] Done. Dataset zipped to {ZIP_PATH}")
    print("[VoiceClone] Upload this zip to voice_clone/train_rvc_colab.ipynb on Colab.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Confirm and proceed (spends ElevenLabs credits)")
    args = parser.parse_args()
    generate_dataset(confirmed=args.yes)


if __name__ == "__main__":
    sys.exit(main())
