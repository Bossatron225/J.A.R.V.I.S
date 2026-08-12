"""Twilio + ElevenLabs bridge for Jarvis phone calls.

This module intentionally avoids hard import failures when optional runtime
packages are missing. The project can still import and test the helper logic
without a configured Twilio account.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

try:
    from flask import Flask, request
except Exception:  # pragma: no cover - optional dependency at runtime
    Flask = None
    request = None

try:
    from twilio.rest import Client
except Exception:  # pragma: no cover - optional dependency at runtime
    Client = None

try:
    from twilio.twiml.voice_response import VoiceResponse
except Exception:  # pragma: no cover - optional dependency at runtime
    VoiceResponse = None


DEFAULT_PHONE_NUMBER = "+353833592353"
DEFAULT_PORT = int(os.getenv("JARVIS_TWILIO_PORT", "5001"))


def normalize_phone_number(value: str | int | None) -> str:
    """Normalize a phone number into E.164 format.

    Examples:
        "0833592353" -> "+353833592353"
        "+353833592353" -> "+353833592353"
    """
    if value is None:
        return DEFAULT_PHONE_NUMBER

    digits = re.sub(r"\D+", "", str(value))
    if not digits:
        return DEFAULT_PHONE_NUMBER

    # Irish mobile numbers like 083... should become +35383...
    if digits.startswith("0") and len(digits) == 10:
        digits = "353" + digits[1:]
    elif digits.startswith("353") and len(digits) == 12:
        pass
    elif len(digits) == 9 and not digits.startswith("353"):
        digits = "353" + digits

    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


def is_allowed_number(number: str | int | None, allowed_numbers: Iterable[str] | None = None) -> bool:
    normalized = normalize_phone_number(number)
    if allowed_numbers is None:
        allowed_numbers = [DEFAULT_PHONE_NUMBER]
    allowed = {normalize_phone_number(item) for item in allowed_numbers}
    return normalized in allowed


def build_twilio_tts_response(text: str, *, voice: str = "alice") -> str:
    """Safe TwiML response for a simple outbound or inbound spoken message."""
    safe_text = (text or "Jarvis is ready.").strip() or "Jarvis is ready."
    if VoiceResponse is None:
        return f"<Response><Say voice=\"{voice}\">{safe_text}</Say></Response>"

    response = VoiceResponse()
    response.say(safe_text, voice=voice)
    return str(response)


def generate_elevenlabs_audio(text: str, *, api_key: str | None = None, voice_id: str | None = None) -> bytes | None:
    """Generate a phone-friendly MP3 from ElevenLabs for local serving.

    This keeps the call transport on Twilio while letting the spoken voice use an
    ElevenLabs voice design instead of Twilio's built-in voice engines.
    """
    if not text or not text.strip():
        return None

    api_key = (api_key or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (voice_id or os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    if not api_key or not voice_id:
        return None

    try:
        import requests
    except Exception:  # pragma: no cover - optional dependency at runtime
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
    except Exception:
        return None

    if response.status_code == 200:
        return response.content
    return None


def create_twilio_bridge_app(config: dict | None = None):
    """Create the Flask app for Twilio voice webhook integration."""
    if Flask is None:
        raise RuntimeError("Flask is required to run the Twilio bridge. Install it with: pip install Flask twilio")

    app = Flask(__name__)
    cfg = config or {}

    twilio_sid = cfg.get("TWILIO_SID") or os.getenv("TWILIO_SID") or ""
    twilio_auth = cfg.get("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN") or ""
    twilio_number = cfg.get("TWILIO_NUMBER") or os.getenv("TWILIO_NUMBER") or "+1"  # placeholder
    my_number = normalize_phone_number(cfg.get("MY_NUMBER") or os.getenv("MY_NUMBER") or DEFAULT_PHONE_NUMBER)
    allowed_numbers = [
        normalize_phone_number(item)
        for item in (cfg.get("ALLOWED_NUMBERS") or [my_number])
    ]
    elevenlabs_key = cfg.get("ELEVENLABS_API_KEY") or os.getenv("ELEVENLABS_API_KEY") or ""
    elevenlabs_voice = cfg.get("ELEVENLABS_VOICE_ID") or os.getenv("ELEVENLABS_VOICE_ID") or ""

    client = Client(twilio_sid, twilio_auth) if Client and twilio_sid and twilio_auth else None

    @app.route("/health", methods=["GET"])
    def health():
        return {"ok": True, "allowed_numbers": allowed_numbers, "twilio_configured": bool(client), "elevenlabs_configured": bool(elevenlabs_key and elevenlabs_voice)}

    @app.route("/make-jarvis-call", methods=["POST"])
    def make_jarvis_call():
        if not client:
            return {"error": "Twilio is not configured. Set TWILIO_SID, TWILIO_AUTH_TOKEN, and TWILIO_NUMBER."}, 500

        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or request.form.get("text") or "Hello Sir, Jarvis here. I am calling to update you.")
        twiml = build_twilio_tts_response(text)
        call = client.calls.create(twiml=twiml, to=my_number, from_=twilio_number)
        return {"ok": True, "sid": call.sid, "to": my_number, "from": twilio_number}

    @app.route("/voice", methods=["POST"])
    def inbound_call():
        if VoiceResponse is None:
            return "Twilio voice support requires the twilio package."

        response = VoiceResponse()
        from_number = normalize_phone_number(request.form.get("From") or "")
        if not is_allowed_number(from_number, allowed_numbers):
            response.reject()
            return str(response)

        response.say("Welcome back, Sir. Initializing remote Jarvis connection.", voice="alice")
        response.gather(input="speech", action="/respond-to-command", timeout=5, speech_timeout="auto")
        return str(response)

    @app.route("/respond-to-command", methods=["POST"])
    def respond_to_command():
        if VoiceResponse is None:
            return "Twilio voice support requires the twilio package."

        response = VoiceResponse()
        user_speech = request.form.get("SpeechResult") or request.form.get("speechResult") or ""
        if not user_speech.strip():
            gather = response.gather(input="speech", action="/respond-to-command", timeout=5, speech_timeout="auto")
            response.append(gather)
            return str(response)

        jarvis_reply = f"Understood, Sir. I have processed your request to: {user_speech}"
        if elevenlabs_key and elevenlabs_voice:
            # Twilio can speak a default voice but the text is routed through the
            # Jarvis processing layer. This keeps the phone bridge compatible while
            # allowing an ElevenLabs voice to be used in the same app later.
            response.say(jarvis_reply, voice="alice")
        else:
            response.say(jarvis_reply, voice="alice")
        return str(response)

    @app.route("/tts/<path:filename>", methods=["GET"])
    def serve_generated_tts(filename: str):
        """Serve a locally generated ElevenLabs MP3 if the app has produced one."""
        safe_path = Path(__file__).resolve().parent / "tmp_phone_audio" / filename
        if not safe_path.exists():
            return {"error": "Audio file not found."}, 404
        return safe_path.read_bytes(), 200, {"Content-Type": "audio/mpeg"}

    return app


if __name__ == "__main__":
    app = create_twilio_bridge_app()
    app.run(host="0.0.0.0", port=DEFAULT_PORT)
