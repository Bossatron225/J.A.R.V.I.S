"""Regression tripwire for core/prompt.txt's TOOL ROUTING guidance.

Calls the real Gemini API in forced function-calling mode with the same
system prompt + tool declarations main.py's live session uses, and checks
that a handful of representative utterances route to the expected tool.
Requires config/api_keys.json (gitignored) — skips automatically without it.
"""

import json

import pytest

import main as main_module

_HAS_API_KEY = main_module.API_CONFIG_PATH.exists()
if _HAS_API_KEY:
    try:
        with open(main_module.API_CONFIG_PATH, "r", encoding="utf-8") as f:
            _HAS_API_KEY = bool(json.load(f).get("gemini_api_key"))
    except Exception:
        _HAS_API_KEY = False

requires_gemini_key = pytest.mark.skipif(
    not _HAS_API_KEY, reason="requires config/api_keys.json with a live Gemini key"
)

ROUTING_CASES = [
    ("watch this tab", "visual_watch"),
    ("keep an eye on Chrome for me", "visual_watch"),
    ("what's my CPU usage right now", "system_status"),
    ("take a look at what's on my screen right now", "screen_process"),
    ("search the web for the latest iPhone price", "web_search"),
    ("what time is it", "get_current_time"),
    ("has anyone been seen at the door recently", "visitor_log"),
]


@requires_gemini_key
def test_tool_routing_matches_expected_tool_names():
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=main_module._get_api_key())
    system_prompt = main_module._load_system_prompt()

    failures = []
    for utterance, expected_tool in ROUTING_CASES:
        response = client.models.generate_content(
            model="models/gemini-flash-latest",
            contents=utterance,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[{"function_declarations": main_module.TOOL_DECLARATIONS}],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="ANY")
                ),
            ),
        )
        parts = response.candidates[0].content.parts
        call = next((p.function_call for p in parts if p.function_call), None)
        actual_tool = call.name if call else None
        if actual_tool != expected_tool:
            failures.append(f"{utterance!r}: expected {expected_tool!r}, got {actual_tool!r}")

    assert not failures, "Tool routing regressions:\n" + "\n".join(failures)
