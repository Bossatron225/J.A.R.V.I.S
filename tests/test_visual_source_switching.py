from actions.screen_processor import resolve_visual_source


def test_resolve_visual_source_defaults_to_camera() -> None:
    result = resolve_visual_source("webcam")

    assert result["target_type"] == "camera"
    assert result["label"] == "camera"


def test_resolve_visual_source_maps_alexa_to_window_capture() -> None:
    result = resolve_visual_source("alexa")

    assert result["target_type"] == "window"
    assert result["window_title"] == "Alexa"
    assert result["label"] == "Alexa camera"
