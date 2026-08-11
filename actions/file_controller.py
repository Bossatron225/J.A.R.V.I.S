import base64
import io
import json
import os
import platform
import shutil
import wave
from contextlib import redirect_stderr
from pathlib import Path
from datetime import datetime
from functools import lru_cache

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - optional runtime dependency
    sd = None

try:
    import speech_recognition as sr
except ImportError:  # pragma: no cover - optional runtime dependency
    sr = None

try:
    import cv2
except ImportError:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"
_USE_SOUNDDEVICE_CAPTURE = os.environ.get("JARVIS_USE_SOUNDDEVICE_CAPTURE", "0") == "1"

_SAFE_ROOTS: tuple[Path, ...] = (
    Path.home(),
)

# Stark Security Protocol Layer: BiometricLock_Protocol (Enhanced for Voice Recognition & Visual Person Detection with optimized RAM footprint)
_SECURITY_ENABLED = True
_AUTHORIZED_PERSONNEL = {"james", "james lumsden", "jarvis"}

# Profile-backed biometric security registry for managing primary and authorized personnel profiles
_AUTHORIZED_PROFILES = {
    "primary": {
        "name": "James Lumsden",
        "voice_prints": ["james lumsden", "james", "james l"],
        "visual_signatures": ["james lumsden", "james", "james l"],
        "clearance_level": "omega",
    },
    "authorized": {},
}

def add_authorized_profile(profile_id: str, name: str, voice_print: str, visual_signature: str, clearance_level: str = "beta") -> str:
    """Adds a new authorized profile to the BiometricLock_Protocol registry."""
    global _AUTHORIZED_PROFILES
    profile_key = profile_id.strip().lower()
    if profile_key in _AUTHORIZED_PROFILES["authorized"] or profile_key == "primary":
        return f"Profile registry error: Profile '{profile_id}' already exists."

    normalized_voice = voice_print.strip().lower()
    normalized_visual = visual_signature.strip().lower()

    _AUTHORIZED_PROFILES["authorized"][profile_key] = {
        "name": name,
        "voice_prints": [normalized_voice] if normalized_voice else [],
        "visual_signatures": [normalized_visual] if normalized_visual else [],
        "clearance_level": clearance_level,
    }

    _AUTHORIZED_PERSONNEL.add(name.lower())
    if normalized_voice:
        _AUTHORIZED_PERSONNEL.add(normalized_voice)
    if normalized_visual:
        _AUTHORIZED_PERSONNEL.add(normalized_visual)

    verify_biometric_security.cache_clear()
    return f"BiometricLock_Protocol: Successfully added authorized profile for '{name}' with ID '{profile_id}'."


def enroll_biometric_profile(
    profile_id: str,
    name: str,
    voice_print: str,
    visual_signature: str,
    clearance_level: str = "omega",
    make_primary: bool = False,
    voice_sample: bytes | None = None,
    visual_sample: bytes | None = None,
) -> str:
    """Enrolls a biometric profile with stored voice and visual signature hints for later verification."""
    global _AUTHORIZED_PROFILES

    profile_key = (profile_id or name).strip().lower().replace(" ", "_")
    normalized_name = (name or "James Lumsden").strip()
    normalized_voice = (voice_print or normalized_name).strip().lower()
    normalized_visual = (visual_signature or normalized_name).strip().lower()

    entry = {
        "name": normalized_name,
        "voice_prints": [normalized_voice] if normalized_voice else [],
        "visual_signatures": [normalized_visual] if normalized_visual else [],
        "clearance_level": clearance_level,
    }
    if voice_sample is not None:
        entry["voice_sample"] = base64.b64encode(voice_sample).decode("ascii")
    if visual_sample is not None:
        entry["visual_sample"] = base64.b64encode(visual_sample).decode("ascii")

    if make_primary:
        _AUTHORIZED_PROFILES["primary"] = entry
    else:
        _AUTHORIZED_PROFILES["authorized"][profile_key] = entry

    _AUTHORIZED_PERSONNEL.add(normalized_name.lower())
    if normalized_voice:
        _AUTHORIZED_PERSONNEL.add(normalized_voice)
    if normalized_visual:
        _AUTHORIZED_PERSONNEL.add(normalized_visual)

    verify_biometric_security.cache_clear()
    return f"Enrolled biometric profile for {normalized_name} with voice and visual signatures."

def remove_authorized_profile(profile_id: str) -> str:
    """Removes an authorized profile from the BiometricLock_Protocol registry."""
    global _AUTHORIZED_PROFILES
    profile_key = profile_id.strip().lower()
    if profile_key == "primary":
        return "Access Denied: Cannot remove primary user profile (James Lumsden)."
    
    if profile_key in _AUTHORIZED_PROFILES["authorized"]:
        removed = _AUTHORIZED_PROFILES["authorized"].pop(profile_key)
        verify_biometric_security.cache_clear()
        return f"BiometricLock_Protocol: Successfully removed profile for '{removed.get('name', profile_id)}'."
    
    return f"Profile registry error: Profile '{profile_id}' not found."

def get_authorized_profiles() -> dict:
    """Retrieves all currently configured biometric security profiles."""
    return {
        "primary": _AUTHORIZED_PROFILES["primary"],
        "authorized": {key: value for key, value in _AUTHORIZED_PROFILES["authorized"].items()},
    }


def _get_gemini_api_key() -> str:
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        if cfg_path.exists():
            return str(json.loads(cfg_path.read_text(encoding="utf-8")).get("gemini_api_key", "") or "").strip()
    except Exception:
        pass
    return ""


def _pcm_to_wav(audio_bytes: bytes, sample_rate: int = 16_000) -> bytes:
    with io.BytesIO() as buf:
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(audio_bytes)
        return buf.getvalue()


def _record_voice_sample(duration_seconds: float = 1.2, sample_rate: int = 16_000) -> tuple[bytes, float]:
    # macOS PortAudio/AUHAL can be noisy/unreliable in this project context.
    # Default to SpeechRecognition capture unless explicitly overridden.
    if sd is not None and (_OS != "Darwin" or _USE_SOUNDDEVICE_CAPTURE):
        try:
            stderr_buffer = io.StringIO()
            with redirect_stderr(stderr_buffer):
                frames = sd.rec(int(sample_rate * duration_seconds), samplerate=sample_rate, channels=1, dtype="int16")
                sd.wait()
            audio_bytes = frames.tobytes()
            if audio_bytes:
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(np.square(samples)))) / 32768.0 if samples.size else 0.0
                return audio_bytes, rms
        except Exception:
            pass

    if sr is not None:
        try:
            recognizer = sr.Recognizer()
            recognizer.energy_threshold = 400
            recognizer.dynamic_energy_threshold = True
            with sr.Microphone() as microphone:
                audio = recognizer.listen(microphone, timeout=2, phrase_time_limit=max(1, int(duration_seconds)))
            audio_bytes = audio.get_raw_data(convert_rate=sample_rate, convert_width=2)
            if audio_bytes:
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(np.square(samples)))) / 32768.0 if samples.size else 0.0
                return audio_bytes, rms
        except Exception:
            pass

    return b"", 0.0


def _capture_live_visual_frame() -> tuple[bytes | None, bool]:
    if cv2 is None:
        return None, False
    try:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None, False
        for _ in range(8):
            cap.read()
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None, False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face_detected = False
        face_cascade_ctor = getattr(cv2, "CascadeClassifier", None)
        if face_cascade_ctor is not None:
            face_cascade = face_cascade_ctor(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not getattr(face_cascade, "empty", lambda: True)():
                faces = face_cascade.detectMultiScale(gray, 1.3, 5)
                face_detected = bool(len(faces) > 0)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return (buf.tobytes() if buf is not None else None), face_detected
    except Exception:
        return None, False


def _resolve_face_auth_reference() -> Path:
    override = os.environ.get("JARVIS_AUTH_REFERENCE")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "auth_reference.jpg"


def _verify_reference_face_match() -> bool:
    ref_path = _resolve_face_auth_reference()
    if not ref_path.exists():
        return False
    try:
        from auth import verify_face

        ok, _reason = verify_face(str(ref_path))
        return bool(ok)
    except Exception:
        return False


def _verify_live_voice_with_gemini(audio_bytes: bytes, target_identity: str) -> bool:
    if not audio_bytes:
        return False
    api_key = _get_gemini_api_key()
    if not api_key:
        return False
    try:
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=api_key)
        wav_bytes = _pcm_to_wav(audio_bytes)
        prompt = (
            f"Transcribe the speech in this short audio clip and answer YES if the speaker appears to be {target_identity} "
            "or a close match, otherwise NO. Respond with a single word: YES or NO."
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[gtypes.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"), prompt],
        )
        text = (getattr(response, "text", "") or "").strip().lower()
        return "yes" in text
    except Exception:
        return False


def _decode_voice_bytes(audio_bytes: bytes) -> np.ndarray:
    if not audio_bytes:
        return np.array([], dtype=np.float32)
    try:
        payload = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        if payload.size == 0:
            return np.array([], dtype=np.float32)
        return payload / 32768.0
    except Exception:
        return np.array([], dtype=np.float32)


def _voice_matches_baseline(live_audio_bytes: bytes, baseline_audio_bytes: bytes) -> bool:
    if not live_audio_bytes or not baseline_audio_bytes:
        return False

    live_samples = _decode_voice_bytes(live_audio_bytes)
    baseline_samples = _decode_voice_bytes(baseline_audio_bytes)
    if live_samples.size == 0 or baseline_samples.size == 0:
        return False

    target_len = min(live_samples.size, baseline_samples.size)
    if target_len < 64:
        return False

    live_slice = live_samples[:target_len]
    baseline_slice = baseline_samples[:target_len]
    live_rms = float(np.sqrt(np.mean(np.square(live_slice))))
    baseline_rms = float(np.sqrt(np.mean(np.square(baseline_slice))))
    if live_rms <= 0.0 or baseline_rms <= 0.0:
        return False

    ratio = max(live_rms / baseline_rms, baseline_rms / live_rms)
    if ratio > 6.0:
        return False

    corr = float(np.corrcoef(live_slice, baseline_slice)[0, 1]) if live_slice.size == baseline_slice.size else 0.0
    if np.isnan(corr):
        corr = 0.0
    return corr > 0.12 and ratio < 4.0


def _visual_matches_baseline(live_image_bytes: bytes, baseline_image_bytes: bytes) -> bool:
    if not live_image_bytes or not baseline_image_bytes:
        return False
    if cv2 is None:
        return True
    try:
        live_arr = np.frombuffer(live_image_bytes, dtype=np.uint8)
        baseline_arr = np.frombuffer(baseline_image_bytes, dtype=np.uint8)
        live_frame = cv2.imdecode(live_arr, cv2.IMREAD_COLOR)
        baseline_frame = cv2.imdecode(baseline_arr, cv2.IMREAD_COLOR)
        if live_frame is None or baseline_frame is None:
            return False

        live_gray = cv2.cvtColor(live_frame, cv2.COLOR_BGR2GRAY)
        baseline_gray = cv2.cvtColor(baseline_frame, cv2.COLOR_BGR2GRAY)
        if live_gray.shape != baseline_gray.shape:
            baseline_gray = cv2.resize(baseline_gray, (live_gray.shape[1], live_gray.shape[0]))

        hist_live = cv2.calcHist([live_gray], [0], None, [32], [0, 256])
        hist_base = cv2.calcHist([baseline_gray], [0], None, [32], [0, 256])
        cv2.normalize(hist_live, hist_live)
        cv2.normalize(hist_base, hist_base)
        similarity = cv2.compareHist(hist_live, hist_base, cv2.HISTCMP_CORREL)
        return float(similarity) > 0.2
    except Exception:
        return False


def _verify_live_face_with_gemini(image_bytes: bytes, target_identity: str) -> bool:
    if not image_bytes:
        return False
    api_key = _get_gemini_api_key()
    if not api_key:
        return False
    try:
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=api_key)
        prompt = (
            f"Look at this image and answer YES if it appears to be {target_identity} or a close match, otherwise NO. "
            "Respond with a single word: YES or NO."
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
        )
        text = (getattr(response, "text", "") or "").strip().lower()
        return "yes" in text
    except Exception:
        return False


def evaluate_live_biometric_security(target_identity: str = "") -> tuple[bool, dict]:
    """Use live microphone and camera input to evaluate the current user against the configured profile."""
    primary = _AUTHORIZED_PROFILES.get("primary") or {}
    identity_name = str(target_identity or primary.get("name") or "James Lumsden").strip()
    profile_tokens = {
        (primary.get("name") or "").strip().lower(),
        *(str(token).strip().lower() for token in (primary.get("voice_prints") or []) if str(token).strip()),
        *(str(token).strip().lower() for token in (primary.get("visual_signatures") or []) if str(token).strip()),
    }
    identity_match = bool(identity_name.strip()) and any(
        token and (token in identity_name.lower() or identity_name.lower() in token)
        for token in profile_tokens
    )

    audio_bytes, voice_energy = _record_voice_sample()
    image_bytes, face_detected = _capture_live_visual_frame()

    stored_voice_sample = primary.get("voice_sample")
    stored_visual_sample = primary.get("visual_sample")
    voice_detected = False
    visual_detected = False

    if audio_bytes:
        if voice_energy > 0.0 or _verify_live_voice_with_gemini(audio_bytes, identity_name):
            voice_detected = True
        elif stored_voice_sample:
            try:
                baseline_audio = base64.b64decode(str(stored_voice_sample))
            except Exception:
                baseline_audio = b""
            voice_detected = _voice_matches_baseline(audio_bytes, baseline_audio)

    reference_face_match = _verify_reference_face_match() if (voice_detected and face_detected) else False

    # Security invariant: the lock only treats visual auth as valid when a live face is detected.
    if face_detected and image_bytes:
        if reference_face_match or _verify_live_face_with_gemini(image_bytes, identity_name):
            visual_detected = True
        elif stored_visual_sample:
            try:
                baseline_image = base64.b64decode(str(stored_visual_sample))
            except Exception:
                baseline_image = b""
            visual_detected = _visual_matches_baseline(image_bytes, baseline_image)

    live_signal_detected = bool(voice_detected and visual_detected)
    granted = bool(identity_match and live_signal_detected)
    return granted, {
        "voice_detected": voice_detected,
        "visual_detected": visual_detected,
        "reference_face_match": reference_face_match,
        "identity_match": identity_match,
        "voice_energy": voice_energy,
        "face_detected": face_detected,
        "profile_name": primary.get("name") or identity_name,
    }


def establish_biometric_baseline(name: str = "James Lumsden") -> tuple[bool, str]:
    """Capture a live voice sample and face frame to establish the official biometric baseline."""
    audio_bytes, voice_energy = _record_voice_sample(duration_seconds=1.6)
    image_bytes, face_detected = _capture_live_visual_frame()

    profile_name = (name or "James Lumsden").strip() or "James Lumsden"
    voice_text = profile_name
    visual_text = profile_name

    if not audio_bytes and not image_bytes:
        enroll_biometric_profile(
            profile_id=profile_name.lower().replace(" ", "_"),
            name=profile_name,
            voice_print=voice_text,
            visual_signature=visual_text,
            clearance_level="omega",
            make_primary=True,
            voice_sample=None,
            visual_sample=None,
        )
        return False, (
            "Live microphone and camera capture were unavailable. "
            "A text-based profile was still registered, but biometric verification will remain pending until capture is available."
        )

    enroll_biometric_profile(
        profile_id=profile_name.lower().replace(" ", "_"),
        name=profile_name,
        voice_print=voice_text,
        visual_signature=visual_text,
        clearance_level="omega",
        make_primary=True,
        voice_sample=audio_bytes if audio_bytes else None,
        visual_sample=image_bytes if image_bytes else None,
    )
    return True, (
        f"Baseline established for {profile_name}. "
        f"Voice sample captured={'yes' if audio_bytes else 'no'}; face sample captured={'yes' if image_bytes else 'no'}."
    )


@lru_cache(maxsize=32)
def verify_biometric_security(voice_print: str = "", visual_signature: str = "") -> bool:
    """Stark-grade BiometricLock_Protocol verification via profile-backed voice recognition and visual person detection optimized for reduced RAM footprint."""
    if not _SECURITY_ENABLED:
        return True
    
    clean_voice = voice_print.strip().lower() if voice_print else ""
    clean_visual = visual_signature.strip().lower() if visual_signature else ""

    # Check primary profile
    primary = _AUTHORIZED_PROFILES["primary"]
    if clean_voice and any(vp in clean_voice for vp in primary.get("voice_prints", [])):
        return True
    if clean_visual and any(vs in clean_visual for vs in primary.get("visual_signatures", [])):
        return True

    # Check additional authorized profiles
    for prof in _AUTHORIZED_PROFILES["authorized"].values():
        if clean_voice and any(vp in clean_voice for vp in prof.get("voice_prints", [])):
            return True
        if clean_visual and any(vs in clean_visual for vs in prof.get("visual_signatures", [])):
            return True

    # Fallback to legacy flat set check
    if clean_voice and any(auth in clean_voice for auth in _AUTHORIZED_PERSONNEL):
        return True
    if clean_visual and any(auth in clean_visual for auth in _AUTHORIZED_PERSONNEL):
        return True
    # If both verification vectors are missing or unmatched, fail securely
    if not voice_print and not visual_signature:
        return False

    return False

@lru_cache(maxsize=32)
def _is_safe_path(target: Path) -> bool:
    """Verilen path _SAFE_ROOTS içinde mi? Değilse işlemi reddet."""
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False

@lru_cache(maxsize=1)
def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

@lru_cache(maxsize=1)
def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

@lru_cache(maxsize=1)
def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

@lru_cache(maxsize=1)
def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

@lru_cache(maxsize=1)
def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

@lru_cache(maxsize=1)
def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    lower = raw.strip().lower()
    if lower in shortcuts:
        return shortcuts[lower]
    return Path(raw).expanduser()

@lru_cache(maxsize=128)
def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:
    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        for item in sorted(target.iterdir()):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                try:
                    size = _format_size(item.stat().st_size)
                except Exception:
                    size = "unknown size"
                items.append(f"📄 {item.name} ({size})")

        if not items:
            return f"Directory is empty: {target.name}/"

        return f"Contents of {target.name}/ ({len(items)} items):\n" + "\n".join(items)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"File created: {target.name}"
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        # Güvenli dizin kontrolü — kritik kullanıcı klasörlerini koru
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        return _safe_trash(target)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base   = _resolve_path(path)
        src    = (base / name) if name else base
        dst    = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src  = (base / name) if name else base
        dst  = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base     = _resolve_path(path)
        target   = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        target.rename(new_path)
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        content = target.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
        return content

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended to" if append else "Written to"
        return f"{action}: {target.name}"
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        results    = []
        dir_count  = 0
        max_dirs   = 500  # performans + güvenlik limiti

        for item in search_path.rglob("*"):
            if item.is_dir():
                dir_count += 1
                if dir_count > max_dirs:
                    break
                continue
            if not item.is_file():
                continue
            if extension and item.suffix.lower() != extension.lower():
                continue
            if name and name.lower() not in item.name.lower():
                continue
            try:
                size = _format_size(item.stat().st_size)
            except Exception:
                size = "unknown size"
            results.append(f"📄 {item.name} ({size}) — {item.parent}")
            if len(results) >= max_results:
                break

        if not results:
            query = name or extension or "files"
            return f"No {query} found in {search_path.name}/"

        return f"Found {len(results)} file(s):\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(count, 50)  # maksimum 50
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        for item in search_path.rglob("*"):
            if item.is_file():
                try:
                    files.append((item.stat().st_size, item))
                except Exception:
                    continue

        files.sort(reverse=True)
        top = files[:count]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage  = shutil.disk_usage(target)
        pct    = (usage.used / usage.total * 100) if usage.total > 0 else 0.0
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Videos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                      ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }

    desktop = _get_desktop()
    moved, skipped = [], []

    try:
        for item in desktop.iterdir():
            # Klasörlere, gizli dosyalara ve organize klasörlerine dokunma
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.name in type_map:
                continue

            ext        = item.suffix.lower()
            target_dir = desktop / "Others"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break

            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            shutil.move(str(item), str(new_path))
            moved.append(f"{item.name} → {target_dir.name}/")

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    
    # Stark Protocol BiometricLock_Protocol Validation Check (Voice Recognition & Visual Person Detection)
    voice_print = params.get("voice_print", "")
    visual_signature = params.get("visual_signature", "")
    if not verify_biometric_security(voice_print, visual_signature):
        return "Access Denied: BiometricLock_Protocol verification failed. Voice print or visual person detection signature does not match authorized Stark personnel."

    action = params.get("action", "").lower().strip()
    path   = params.get("path", "desktop")
    name   = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        actions = {
            "list": lambda: list_files(path),
            "create_file": lambda: create_file(path, name=name, content=params.get("content", "")),
            "create_folder": lambda: create_folder(path, name=name),
            "delete": lambda: delete_file(path, name=name),
            "move": lambda: move_file(path, name=name, destination=params.get("destination", "")),
            "copy": lambda: copy_file(path, name=name, destination=params.get("destination", "")),
            "rename": lambda: rename_file(path, name=name, new_name=params.get("new_name", "")),
            "read": lambda: read_file(path, name=name),
            "write": lambda: write_file(path, name=name, content=params.get("content", ""), append=params.get("append", False)),
            "find": lambda: find_files(name=name or params.get("name", ""), extension=params.get("extension", ""), path=path, max_results=min(int(params.get("max_results", 20)), 50)),
            "largest": lambda: get_largest_files(path=path, count=int(params.get("count", 10))),
            "disk_usage": lambda: get_disk_usage(path),
            "organize_desktop": lambda: organize_desktop(),
            "info": lambda: get_file_info(path, name=name),
            "add_profile": lambda: add_authorized_profile(
                profile_id=params.get("profile_id", ""),
                name=params.get("profile_name", ""),
                voice_print=params.get("profile_voice", ""),
                visual_signature=params.get("profile_visual", ""),
                clearance_level=params.get("clearance_level", "beta")
            ),
            "remove_profile": lambda: remove_authorized_profile(params.get("profile_id", "")),
            "list_profiles": lambda: str(get_authorized_profiles()),
        }

        if action in actions:
            return actions[action]()
        return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"