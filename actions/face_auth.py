from __future__ import annotations

import json
import os
import pathlib
import pickle
import time
from typing import Any

import cv2
import numpy as np


_DEFAULT_PROFILE_PATH = pathlib.Path.home() / ".jarvis_face_profile.json"


def _ensure_cached_dir(path: str | os.PathLike[str] | None = None) -> pathlib.Path:
    profile_path = pathlib.Path(path or _DEFAULT_PROFILE_PATH)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    return profile_path


def _image_signature(image_bytes: bytes) -> list[float]:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    if arr.size == 0:
        raise ValueError("Empty image payload")
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    feat = thresh.astype(np.float32).reshape(-1)
    return (feat / 255.0).tolist()


def enroll_face_profile(image_bytes: bytes, profile_path: str | os.PathLike[str] | None = None, label: str = "User") -> dict[str, Any]:
    signature = _image_signature(image_bytes)
    profile = {
        "label": label,
        "signature": signature,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = _ensure_cached_dir(profile_path)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return profile


def verify_face_profile(image_bytes: bytes, profile_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = _ensure_cached_dir(profile_path)
    if not path.exists():
        return {"authorized": False, "confidence": 0.0, "message": "No face profile enrolled"}

    stored = json.loads(path.read_text(encoding="utf-8"))
    stored_sig = stored.get("signature")
    if not isinstance(stored_sig, list):
        return {"authorized": False, "confidence": 0.0, "message": "Stored profile is invalid"}

    incoming = _image_signature(image_bytes)
    if len(stored_sig) != len(incoming):
        return {"authorized": False, "confidence": 0.0, "message": "Signature size mismatch"}

    diff = np.linalg.norm(np.asarray(stored_sig, dtype=np.float32) - np.asarray(incoming, dtype=np.float32))
    confidence = max(0.0, min(1.0, 1.0 - diff / max(1.0, len(stored_sig) ** 0.5)))
    authorized = confidence >= 0.8
    return {
        "authorized": authorized,
        "confidence": float(confidence),
        "message": f"Face profile matched {stored.get('label', 'user')}" if authorized else "Face profile mismatch",
        "label": stored.get("label", "user"),
    }
