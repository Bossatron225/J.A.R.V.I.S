import json
import pathlib
import sys

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from actions.face_auth import enroll_face_profile, verify_face_profile


def _make_png_bytes() -> bytes:
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    cv2.rectangle(img, (6, 6), (24, 24), (255, 255, 255), -1)
    ok, payload = cv2.imencode(".png", img)
    assert ok
    return payload.tobytes()


def test_face_profile_enroll_and_verify(tmp_path):
    profile_path = tmp_path / "face_profile.json"
    image_bytes = _make_png_bytes()

    profile = enroll_face_profile(image_bytes, profile_path=str(profile_path), label="James")
    assert profile["label"] == "James"
    assert profile_path.exists()

    result = verify_face_profile(image_bytes, profile_path=str(profile_path))
    assert result["authorized"] is True
    assert result["confidence"] >= 0.8

    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert payload["profiles"][0]["label"] == "James"
