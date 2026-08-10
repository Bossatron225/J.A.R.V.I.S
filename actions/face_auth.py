from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

import cv2
import numpy as np


_DEFAULT_PROFILE_PATH = pathlib.Path.home() / ".jarvis_face_profiles.json"
_DEFAULT_THRESHOLD = 0.82


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


def _load_profiles(profile_path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    path = _ensure_cached_dir(profile_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if isinstance(payload, dict) and "profiles" in payload:
        profiles = payload.get("profiles", [])
    elif isinstance(payload, dict) and "signature" in payload:
        profiles = [payload]
    else:
        profiles = payload if isinstance(payload, list) else []

    return [p for p in profiles if isinstance(p, dict)]


def _save_profiles(profiles: list[dict[str, Any]], profile_path: str | os.PathLike[str] | None = None) -> None:
    path = _ensure_cached_dir(profile_path)
    path.write_text(json.dumps({"profiles": profiles}, indent=2), encoding="utf-8")


def enroll_face_profile(
    image_bytes: bytes,
    profile_path: str | os.PathLike[str] | None = None,
    label: str = "User",
    threshold: float | None = None,
) -> dict[str, Any]:
    signature = _image_signature(image_bytes)
    profile = {
        "label": label,
        "signature": signature,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "threshold": float(threshold or _DEFAULT_THRESHOLD),
    }
    path = _ensure_cached_dir(profile_path)
    profiles = _load_profiles(path)
    profiles = [p for p in profiles if p.get("label") != label]
    profiles.append(profile)
    _save_profiles(profiles, path)
    return profile


def verify_face_profile(
    image_bytes: bytes,
    profile_path: str | os.PathLike[str] | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    profiles = _load_profiles(profile_path)
    if not profiles:
        return {"authorized": False, "confidence": 0.0, "message": "No face profile enrolled"}

    incoming = _image_signature(image_bytes)
    best_match = None
    best_confidence = 0.0

    for profile in profiles:
        stored_sig = profile.get("signature")
        if not isinstance(stored_sig, list):
            continue
        if len(stored_sig) != len(incoming):
            continue

        diff = np.linalg.norm(np.asarray(stored_sig, dtype=np.float32) - np.asarray(incoming, dtype=np.float32))
        confidence = max(0.0, min(1.0, 1.0 - diff / max(1.0, len(stored_sig) ** 0.5)))
        if confidence > best_confidence:
            best_confidence = confidence
            best_match = profile

    required_threshold = float(threshold or _DEFAULT_THRESHOLD)
    authorized = best_confidence >= required_threshold and best_match is not None
    return {
        "authorized": authorized,
        "confidence": float(best_confidence),
        "message": f"Face profile matched {best_match.get('label', 'user')}" if authorized else "Face profile mismatch",
        "label": best_match.get("label", "user") if best_match else "unknown",
        "threshold": required_threshold,
    }
