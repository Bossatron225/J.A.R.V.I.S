import json
import os
import sys

from core.tts import create_tts_player

with open("config/api_keys.json") as f:
    config = json.load(f)

player = create_tts_player(config)
print("Playing test audio...", flush=True)
player.speak("Hello, this is a test. I am checking if my voice is distorted.")
print("Used voice:", player._engine.voice_id)
print("Done.", flush=True)
