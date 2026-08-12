import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "jarvis_twilio_bridge.py"


def test_jarvis_twilio_bridge_exposes_phone_helpers():
    spec = importlib.util.spec_from_file_location("jarvis_twilio_bridge", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    assert module.normalize_phone_number("0833592353") == "+353833592353"
    assert module.is_allowed_number("+353833592353", {"+353833592353"}) is True
    assert module.build_twilio_tts_response("hello sir")
