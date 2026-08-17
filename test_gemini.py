import json
import asyncio
from google import genai
from google.genai import types

data = json.load(open('config/api_keys.json'))
key = data.get('gemini_api_key')
client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})

test_models = [
    'gemini-2.0-flash-exp',
    'models/gemini-2.0-flash-exp',
    'gemini-2.0-flash-realtime-exp',
    'models/gemini-2.0-flash-realtime-exp',
    'gemini-2.5-flash',
    'models/gemini-2.5-flash',
    'models/gemini-2.5-flash-native-audio-preview-12-2025',
    'gemini-2.5-flash-native-audio-preview-12-2025',
    'models/gemini-live-2.5-flash-preview',
]

async def test():
    config = types.LiveConnectConfig(response_modalities=['AUDIO'])
    for m in test_models:
        print(f"Testing {m}...")
        try:
            async with client.aio.live.connect(model=m, config=config) as session:
                print(f"SUCCESS with {m}!")
                return
        except Exception as e:
            print(f"Failed with {m}: {type(e).__name__}: {e}")

if __name__ == '__main__':
    asyncio.run(test())
