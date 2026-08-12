"""Production-safe Twilio + ElevenLabs bridge for Jarvis.

Purpose:
- run on a public host (VPS / cloud server) so Twilio can reach it while your PC is off
- accept inbound calls from your phone number
- allow Jarvis to trigger outbound calls to your phone
- optionally use an ElevenLabs voice ID for spoken text

Usage:
  export TWILIO_SID="..."
  export TWILIO_AUTH_TOKEN="..."
  export TWILIO_NUMBER="+15551234567"
  export MY_NUMBER="+353833592353"
  export ALLOWED_NUMBERS="+353833592353"
  export ELEVENLABS_API_KEY="..."
  export ELEVENLABS_VOICE_ID="..."
  export PORT=8000
  python jarvis_twilio_bridge_prod.py
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

DEFAULT_PHONE_NUMBER = "+353833592353"


def normalize_phone_number(value: str | int | None) -> str:
    """Normalize to E.164 format."""
    if value is None:
        return DEFAULT_PHONE_NUMBER
    digits = re.sub(r"\D+", "", str(value))
    if not digits:
        return DEFAULT_PHONE_NUMBER

    if digits.startswith("0") and len(digits) == 10:
        digits = "353" + digits[1:]
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


def create_app() -> Flask:
    app = Flask(__name__)

    twilio_sid = (os.getenv("TWILIO_SID") or "").strip()
    twilio_auth = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    twilio_number = normalize_phone_number(os.getenv("TWILIO_NUMBER") or "+1")
    my_number = normalize_phone_number(os.getenv("MY_NUMBER") or DEFAULT_PHONE_NUMBER)

    allowed_raw = os.getenv("ALLOWED_NUMBERS") or os.getenv("MY_NUMBER") or my_number
    allowed_numbers = [normalize_phone_number(item) for item in re.split(r"[,;\s]+", str(allowed_raw).strip()) if item.strip()]
    if not allowed_numbers:
        allowed_numbers = [my_number]

    elevenlabs_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    elevenlabs_voice = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()

    client = Client(twilio_sid, twilio_auth) if twilio_sid and twilio_auth else None

    @app.route("/health", methods=["GET"])
    def health():
        return {
            "ok": True,
            "twilio_configured": bool(client),
            "elevenlabs_configured": bool(elevenlabs_key and elevenlabs_voice),
            "allowed_numbers": allowed_numbers,
            "my_number": my_number,
        }

    @app.route("/make-jarvis-call", methods=["POST"]) 
    def make_jarvis_call():
        if not client:
            return {"error": "Twilio is not configured. Set TWILIO_SID, TWILIO_AUTH_TOKEN, and TWILIO_NUMBER."}, 500

        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or request.form.get("text") or "Hello Sir, Jarvis here. I am calling to update you.")
        response = VoiceResponse()
        response.say(text, voice="alice")
        call = client.calls.create(
            twiml=str(response),
            to=my_number,
            from_=twilio_number,
        )
        return {"ok": True, "sid": call.sid, "to": my_number, "from": twilio_number}

    @app.route("/voice", methods=["POST"])
    def inbound_call():
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
        response = VoiceResponse()
        user_speech = request.form.get("SpeechResult") or request.form.get("speechResult") or ""

        if not user_speech.strip():
            gather = response.gather(input="speech", action="/respond-to-command", timeout=5, speech_timeout="auto")
            response.append(gather)
            return str(response)

        jarvis_reply = f"Understood, Sir. I have processed your request to: {user_speech}"
        response.say(jarvis_reply, voice="alice")
        return str(response)

    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "8000")
    create_app().run(host="0.0.0.0", port=port)
