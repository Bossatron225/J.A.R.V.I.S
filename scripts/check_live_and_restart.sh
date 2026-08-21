#!/bin/bash
# Probes Gemini Live API connectivity; starts jarvis-vps.service only once it
# actually succeeds. Google has been rejecting this IP with "User location is
# not supported" (an IP-reputation flag, not a real geo-block) since the old
# retry-storm bug hammered it for ~6h on 2026-08-18. Safe to run repeatedly:
# does exactly one connect attempt per invocation, no retry loop.
LOG=/root/jarvis-vps-runtime/logs/live_probe.log
mkdir -p "$(dirname "$LOG")"

cd /root/jarvis-vps-runtime || exit 1

RESULT=$(.venv/bin/python - << 'PYEOF'
import asyncio, json
from google import genai
from google.genai import types

cfg = json.load(open("config/api_keys.json"))
key = cfg["gemini_api_key"]

async def main():
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    live_cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
            )
        ),
    )
    try:
        async with client.aio.live.connect(
            model="models/gemini-2.5-flash-native-audio-preview-12-2025",
            config=live_cfg,
        ):
            print("OK")
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}")

asyncio.run(main())
PYEOF
)

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $RESULT" >> "$LOG"

if [[ "$RESULT" == "OK" ]]; then
    if ! systemctl is-active --quiet jarvis-vps.service; then
        systemctl start jarvis-vps.service
        echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') Live API reachable — started jarvis-vps.service" >> "$LOG"
    fi
fi
