from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

from google import genai
from google.genai import types as gtypes

try:
    from actions.browser_control import capture_browser_tab as _capture_browser_tab
except Exception:
    _capture_browser_tab = None

_MAC_BROWSER_APP_NAMES = {
     "safari": "Safari",
     "chrome": "Google Chrome",
     "edge": "Microsoft Edge",
     "brave": "Brave Browser",
     "vivaldi": "Vivaldi",
     "opera": "Opera",
     "operagx": "Opera GX",
     "firefox": "Firefox",
}

_MAC_APP_TO_BROWSER = {v.lower(): k for k, v in _MAC_BROWSER_APP_NAMES.items()}

# Common spoken/typed names → actual macOS process name
_APP_NAME_ALIASES: dict[str, str] = {
    "vscode": "Code",
    "vs code": "Code",
    "visual studio code": "Code",
    "visual studio": "Code",
    "code": "Code",
    "iterm": "iTerm2",
    "iterm2": "iTerm2",
    "terminal": "Terminal",
    "finder": "Finder",
    "notes": "Notes",
    "messages": "Messages",
    "mail": "Mail",
    "calendar": "Calendar",
    "spotify": "Spotify",
    "slack": "Slack",
    "notion": "Notion",
    "xcode": "Xcode",
    "pycharm": "PyCharm",
    "intellij": "IntelliJ IDEA",
}


def _resolve_app_name(raw: str) -> str:
    """Map common spoken names to the real macOS process name."""
    return _APP_NAME_ALIASES.get(raw.lower().strip(), raw)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_api_key() -> str:
    key = _load_config().get("gemini_api_key", "")
    if not key:
        raise RuntimeError("gemini_api_key not found in config.")
    return key


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()

_LIVE_MODELS        = (
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
    "models/gemini-live-2.5-flash-preview",
    "models/gemini-2.5-flash-live",
)
_CHANNELS           = 1
_RECEIVE_SAMPLE_RATE = 24_000
_CHUNK_SIZE         = 1_024

_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82

_SYSTEM_PROMPT = (
    "You are JARVIS, James Lumsden's AI assistant. "
    "You are given an image from either the user's screen or their webcam. "
    "Analyze what you see with detail and intelligence. "
    "Describe objects, text, people, components, and their context clearly. "
    "For technical questions (circuits, code, hardware) give specific, expert answers. "
    "Be concise — 2-4 sentences — unless the question demands more detail. "
    "Speak directly to the user ('I can see...', 'You have...'). "
    "Address the user as 'sir' depending on the language they used."
)


def _is_live_model_unavailable_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(k in msg for k in (
        "404",
        "not_found",
        "is not found",
        "no longer available",
        "policy violation",
        "1008",
        "bidi",
        "not supported for bidigeneratecontent",
    ))


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"

def _capture_screen() -> tuple[bytes, str]:

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _capture_macos_window(window_title: str = "", app_name: str = "") -> tuple[bytes, str]:
    if _get_os() != "mac":
        raise RuntimeError("Targeted application/window capture is currently supported on macOS only.")

    title_l = (window_title or "").lower().strip()
    app_l = (app_name or "").lower().strip()
    if app_l:
        script = f'''
        set targetApp to "{app_name.replace('"', '\\"')}"
        tell application "System Events"
            repeat with p in application processes
                try
                    set pName to name of p
                    if pName is not missing value then
                        if (pName as text) contains targetApp then
                            if exists front window of p then return (id of front window of p as text)
                            if (count of windows of p) > 0 then return (id of window 1 of p as text)
                        end if
                    end if
                end try
            end repeat
        end tell
        return ""
        '''
    elif title_l:
        script = f'''
        set targetTitle to "{window_title.replace('"', '\\"')}"
        tell application "System Events"
            repeat with p in application processes
                try
                    repeat with w in windows of p
                        set wName to name of w
                        if wName is not missing value then
                            if (wName as text) contains targetTitle then return (id of w as text)
                        end if
                    end repeat
                end try
            end repeat
        end tell
        return ""
        '''
    else:
        script = '''
        tell application "System Events"
            try
                set frontProc to first application process whose frontmost is true
                tell frontProc
                    if exists front window then return (id of front window as text)
                    if (count of windows) > 0 then return (id of window 1 as text)
                end tell
            end try
        end tell
        return ""
        '''

    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    window_id = (proc.stdout or "").strip()

    # Fallback: Quartz window list for Electron/non-accessible apps (e.g. VS Code)
    if (not window_id or not window_id.isdigit()) and (app_l or title_l):
        try:
            import Quartz
            wlist = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            for w in (wlist or []):
                owner = (w.get("kCGWindowOwnerName") or "").lower()
                name  = (w.get("kCGWindowName") or "").lower()
                wid   = w.get("kCGWindowNumber")
                if not wid:
                    continue
                if app_l and app_l in owner:
                    window_id = str(wid)
                    break
                if title_l and title_l in name:
                    window_id = str(wid)
                    break
        except Exception:
            pass

    if not window_id or not window_id.isdigit():
        if app_l:
            raise RuntimeError(f"Could not find a visible window for app '{app_name}'.")
        if title_l:
            raise RuntimeError(f"Could not find a visible window matching '{window_title}'.")
        raise RuntimeError("Could not find the frontmost window.")

    fd, temp_path = tempfile.mkstemp(suffix=".png", prefix="jarvis_window_")
    os.close(fd)
    try:
        shot = subprocess.run(
            ["screencapture", "-x", "-l", window_id, temp_path],
            capture_output=True,
            text=True,
        )
        if shot.returncode != 0:
            raise RuntimeError((shot.stderr or shot.stdout or "window capture failed").strip())
        data = Path(temp_path).read_bytes()
        return _compress(data, "PNG")
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _capture_safari_tab(target: str = "", index: int | None = None) -> tuple[bytes, str, str]:
    if _get_os() != "mac":
        raise RuntimeError("Safari tab capture is currently supported on macOS only.")

    target = (target or "").strip()
    target_l = target.lower()

    script = [
        'tell application "Safari"',
        'if not running then return ""',
    ]

    if index is not None or target_l:
        if index is not None:
            script.append(f"set wantedIndex to {max(1, int(index))}")
            script.append("set tabCounter to 0")
            script.append("repeat with wIndex from 1 to count of windows")
            script.append("repeat with tIndex from 1 to count of tabs of window wIndex")
            script.append("set tabCounter to tabCounter + 1")
            script.append("if tabCounter is wantedIndex then")
            script.append("set current tab of window wIndex to tab tIndex of window wIndex")
            script.append("exit repeat")
            script.append("end if")
            script.append("end repeat")
            script.append("end repeat")
        else:
            escaped = target.replace('"', '\\"')
            script.append(f'set wantedText to "{escaped}"')
            script.append("repeat with wIndex from 1 to count of windows")
            script.append("repeat with tIndex from 1 to count of tabs of window wIndex")
            script.append("set theTab to tab tIndex of window wIndex")
            script.append("set theTitle to name of theTab")
            script.append("set theURL to URL of theTab")
            script.append("if theTitle is not missing value then")
            script.append("if (theTitle as text) contains wantedText or (theTitle as text) is equal to wantedText or (theURL as text) contains wantedText then")
            script.append("set current tab of window wIndex to theTab")
            script.append("exit repeat")
            script.append("end if")
            script.append("end if")
            script.append("end repeat")
            script.append("end repeat")

    script.extend([
        'activate',
        'delay 0.2',
        'if (count of windows) is 0 then return ""',
        'set frontWin to front window',
        'return (id of frontWin as text)',
        'end tell',
    ])

    proc = subprocess.run(["osascript", "-e", "\n".join(script)], capture_output=True, text=True)
    window_id = (proc.stdout or proc.stderr or "").strip()
    if not window_id or not window_id.isdigit():
        raise RuntimeError("Could not capture the active Safari tab.")

    fd, temp_path = tempfile.mkstemp(suffix=".png", prefix="jarvis_safari_tab_")
    os.close(fd)
    try:
        shot = subprocess.run(
            ["screencapture", "-x", "-l", window_id, temp_path],
            capture_output=True,
            text=True,
        )
        if shot.returncode != 0:
            raise RuntimeError((shot.stderr or shot.stdout or "Safari tab capture failed").strip())
        data = Path(temp_path).read_bytes()
        title = target or (f"Safari tab {index}" if index is not None else "Safari active tab")
        compressed, mime = _compress(data, "PNG")
        return compressed, mime, f"safari-tab:{title}"
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _capture_macos_browser_tab(browser: str, target: str = "", index: int | None = None) -> tuple[bytes, str, str]:
    if _get_os() != "mac":
        raise RuntimeError("Native browser tab capture is currently supported on macOS only.")

    b = (browser or "").strip().lower()
    if not b:
        raise RuntimeError("Specify a browser for native tab capture on macOS.")
    if b == "safari":
        return _capture_safari_tab(target=target, index=index)

    app_name = _MAC_BROWSER_APP_NAMES.get(b)
    if not app_name:
        raise RuntimeError(f"Native tab capture is not configured for browser '{browser}'.")

    # Chrome-family and Firefox are captured from the app's active/front tab.
    activate_proc = subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        capture_output=True,
        text=True,
    )
    if activate_proc.returncode != 0:
        msg = (activate_proc.stderr or activate_proc.stdout or "browser activation failed").strip()
        raise RuntimeError(msg)

    script = f'''
    tell application "System Events"
        set procRef to first application process whose name is "{app_name}"
        if exists front window of procRef then return (id of front window of procRef as text)
    end tell
    return ""
    '''
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    window_id = (proc.stdout or "").strip()
    if not window_id or not window_id.isdigit():
        raise RuntimeError(f"Could not find a visible window for {app_name}.")

    fd, temp_path = tempfile.mkstemp(suffix=".png", prefix=f"jarvis_{b}_tab_")
    os.close(fd)
    try:
        shot = subprocess.run(
            ["screencapture", "-x", "-l", window_id, temp_path],
            capture_output=True,
            text=True,
        )
        if shot.returncode != 0:
            raise RuntimeError((shot.stderr or shot.stdout or f"{app_name} tab capture failed").strip())
        data = Path(temp_path).read_bytes()
        compressed, mime = _compress(data, "PNG")
        label = f"{b}-tab:{target or ('index-' + str(index) if index is not None else 'active')}"
        return compressed, mime, label
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _capture_targeted_visual(target_type: str = "screen", *, browser: str = "", target: str = "", index: int | None = None, window_title: str = "", app_name: str = "") -> tuple[bytes, str, str]:
    source = (target_type or "screen").strip().lower()
    if source in {"screen", "display", "full"}:
        data, mime = _capture_screen()
        return data, mime, "screen"
    if source in {"camera", "webcam"}:
        data, mime = _capture_camera()
        return data, mime, "camera"
    if source in {"tab", "browser", "browser_tab", "browser-tab"}:
        if _get_os() == "mac":
            b = (browser or "").strip().lower()
            if not b:
                b = _detect_frontmost_macos_browser()
            if b in _MAC_BROWSER_APP_NAMES:
                return _capture_macos_browser_tab(browser=b, target=target, index=index)
        if not _capture_browser_tab:
            raise RuntimeError("Browser tab capture is unavailable in this build.")
        data, mime, label = _capture_browser_tab({
            "browser": browser,
            "target": target,
            "index": index,
        })
        return data, mime, label
    if source in {"window", "app", "application"}:
        if source in {"app", "application"}:
            app = _resolve_app_name((app_name or target).strip())
            b = app.lower()
            if _get_os() == "mac" and b in _MAC_BROWSER_APP_NAMES:
                data, mime, label = _capture_macos_browser_tab(browser=b)
                return data, mime, label
            data, mime = _capture_macos_window(window_title="", app_name=app)
            return data, mime, f"app:{app or 'frontmost'}"

        title = (window_title or target).strip()
        resolved_app = _resolve_app_name((app_name or "").strip())
        data, mime = _capture_macos_window(window_title=title, app_name=resolved_app)
        return data, mime, f"window:{title or resolved_app or 'frontmost'}"
    raise RuntimeError(f"Unknown visual target type: {target_type}")


def _detect_frontmost_macos_browser() -> str:
    if _get_os() != "mac":
        return ""
    script = '''
    tell application "System Events"
        try
            set procRef to first application process whose frontmost is true
            return (name of procRef as text)
        end try
    end tell
    return ""
    '''
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    app_name = (proc.stdout or "").strip().lower()
    return _MAC_APP_TO_BROWSER.get(app_name, "")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW    
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION  
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def capture_camera_b64(parameters=None, player=None) -> dict:
    image_bytes, mime_type = _capture_camera()
    return {
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "mime_type": mime_type
    }

def capture_targeted_visual_b64(parameters=None, player=None) -> dict:
    params = parameters or {}
    target_type = params.get("target_type") or params.get("angle") or "screen"
    if target_type in {"mac_camera", "mac_webcam"}:
        target_type = "camera"
    data, mime, label = _capture_targeted_visual(
        target_type=target_type,
        browser=params.get("browser", ""),
        target=params.get("target", ""),
        index=params.get("index"),
        window_title=params.get("window_title", ""),
        app_name=params.get("app_name", ""),
    )
    return {
        "image_b64": base64.b64encode(data).decode("ascii"),
        "mime_type": mime,
        "label": label,
    }

def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()

    # AVFoundation can transiently refuse to open the device (e.g. right after
    # another process/session released it) even though it's genuinely free a
    # moment later — a couple of short retries clears this without masking a
    # real "no camera" failure.
    cap = None
    for attempt in range(3):
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            break
        cap.release()
        cap = None
        if attempt < 2:
            time.sleep(0.5)

    if cap is None:
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"

class _VisionSession:
    def __init__(self):
        self._loop:       Optional[asyncio.AbstractEventLoop] = None
        self._thread:     Optional[threading.Thread]          = None
        self._session                                          = None
        self._out_queue:  Optional[asyncio.Queue]             = None
        self._audio_in:   Optional[asyncio.Queue]             = None
        self._ready_evt:  threading.Event                     = threading.Event()
        self._player                                           = None
        self._lock:       threading.Lock                       = threading.Lock()

    def start(self, player=None, timeout: float = 25.0) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if player is not None:
                    self._player = player
                return
            self._player = player
            self._thread = threading.Thread(
                target=self._run_event_loop,
                daemon=True,
                name="VisionSessionThread",
            )
            self._thread.start()

        if not self._ready_evt.wait(timeout=timeout):
            raise RuntimeError(f"Vision session did not connect within {timeout}s.")
        print("[Vision] ✅ Session ready")

    def analyze(self, image_bytes: bytes, mime_type: str, user_text: str) -> None:
        if not self._loop or not self._out_queue:
            print("[Vision] ⚠️  Session not started — dropping request")
            return
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put((image_bytes, mime_type, user_text)),
            self._loop,
        )

    def is_ready(self) -> bool:
        return self._session is not None

    def _run_event_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._session_loop())

    async def _session_loop(self) -> None:
        self._out_queue = asyncio.Queue(maxsize=30)
        self._audio_in  = asyncio.Queue()

        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"},
        )
        config = gtypes.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            system_instruction=_SYSTEM_PROMPT,
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

        backoff = 2.0
        while True:
            try:
                print("[Vision] 🔌 Connecting...")
                connected = False
                last_error = None
                for model_name in _LIVE_MODELS:
                    try:
                        print(f"[Vision] Trying model: {model_name}")
                        async with client.aio.live.connect(
                            model=model_name, config=config
                        ) as session:
                            connected = True
                            self._session = session
                            self._ready_evt.set()
                            backoff = 2.0
                            print(f"[Vision] ✅ Connected on {model_name}")

                            async with asyncio.TaskGroup() as tg:
                                tg.create_task(self._send_loop())
                                tg.create_task(self._recv_loop())
                                tg.create_task(self._play_loop())
                        break
                    except Exception as e:
                        last_error = e
                        if _is_live_model_unavailable_error(e):
                            print(f"[Vision] Model unavailable: {model_name} — trying fallback")
                            continue
                        raise
                if not connected and last_error:
                    raise last_error

            except* Exception as eg:
                for exc in eg.exceptions:
                    print(f"[Vision] ⚠️  Session error: {exc}")
            finally:
                self._session = None
                self._ready_evt.clear()

            print(f"[Vision] 🔄 Reconnecting in {backoff:.0f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
            self._ready_evt.set()  

    async def _send_loop(self) -> None:
        while True:
            image_bytes, mime_type, user_text = await self._out_queue.get()
            if not self._session:
                print("[Vision] ⚠️  No session — dropping image")
                continue
            try:
                b64 = base64.b64encode(image_bytes).decode("ascii")
                await self._session.send_client_content(
                    turns={
                        "parts": [
                            {"inline_data": {"mime_type": mime_type, "data": b64}},
                            {"text": user_text},
                        ]
                    },
                    turn_complete=True,
                )
                print(f"[Vision] 📤 Sent {len(image_bytes):,} bytes — '{user_text[:60]}'")
            except Exception as e:
                print(f"[Vision] ⚠️  Send error: {e}")
                raise  # propagate to TaskGroup → triggers session reconnect

    async def _recv_loop(self) -> None:
        transcript: list[str] = []
        try:
            async for response in self._session.receive():
                if response.data:
                    await self._audio_in.put(response.data)

                sc = response.server_content
                if not sc:
                    continue

                if sc.output_transcription and sc.output_transcription.text:
                    chunk = sc.output_transcription.text.strip()
                    if chunk:
                        transcript.append(chunk)

                if sc.turn_complete:
                    if transcript and self._player:
                        full = re.sub(r"\s+", " ", " ".join(transcript)).strip()
                        if full:
                            self._player.write_log(f"Jarvis: {full}")
                            print(f"[Vision] 💬 {full}")
                    transcript = []
                    # Auto-close camera ~2s after JARVIS finishes speaking
                    if self._player and hasattr(self._player, "stop_camera_stream"):
                        async def _deferred_close():
                            await asyncio.sleep(2.0)
                            try:
                                self._player.stop_camera_stream()
                            except Exception:
                                pass
                        asyncio.create_task(_deferred_close())

        except Exception as e:
            print(f"[Vision] ⚠️  Recv error: {e}")
            raise  

    async def _play_loop(self) -> None:
        import sys
        import numpy as np
        play_channels = 2 if sys.platform == "darwin" else _CHANNELS
        stream = sd.RawOutputStream(
            samplerate=_RECEIVE_SAMPLE_RATE,
            channels=play_channels,
            dtype="int16",
            blocksize=_CHUNK_SIZE,
        )
        stream.start()
        try:
            while True:
                chunk = await self._audio_in.get()
                if play_channels == 2 and chunk:
                    # Convert mono 16-bit PCM to stereo
                    arr = np.frombuffer(chunk, dtype=np.int16)
                    chunk = np.repeat(arr, 2).tobytes()
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[Vision] ❌ Play error: {e}")
            raise
        finally:
            stream.stop()
            stream.close()

_session      = _VisionSession()
_session_lock = threading.Lock()
_session_up   = False


def _ensure_session(player=None) -> None:
    global _session_up
    with _session_lock:
        if not _session_up:
            _session.start(player=player)
            _session_up = True
        elif player is not None:
            _session._player = player


def screen_process(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> bool:

    params    = parameters or {}
    user_text = (params.get("text") or params.get("user_text") or "").strip()
    angle     = params.get("angle", "screen").lower().strip()

    if not user_text:
        print("[Vision] ⚠️  No question provided — aborting")
        return False

    print(f"[Vision] ▶ angle={angle!r}  question='{user_text[:80]}'")

    try:
        _ensure_session(player=player)
    except Exception as e:
        print(f"[Vision] ❌ Could not start session: {e}")
        return False

    try:
        if angle == "camera":
            image_bytes, mime_type = _capture_camera()
            print(f"[Vision] 📷 Camera: {len(image_bytes):,} bytes")
            if player and hasattr(player, "start_camera_stream"):
                try:
                    player.start_camera_stream()
                except Exception as _e:
                    print(f"[Vision] ⚠️  Camera stream failed: {_e}")
            elif player and hasattr(player, "show_camera_frame"):
                try:
                    player.show_camera_frame(image_bytes)
                except Exception as _e:
                    print(f"[Vision] ⚠️  Camera preview failed: {_e}")
        else:
            image_bytes, mime_type = _capture_screen()
            print(f"[Vision] 🖥️  Screen: {len(image_bytes):,} bytes")
    except Exception as e:
        print(f"[Vision] ❌ Capture error: {e}")
        return False

    _session.analyze(image_bytes, mime_type, user_text)
    return True


def warmup_session(player=None) -> None:
    try:
        _ensure_session(player=player)
    except Exception as e:
        print(f"[Vision] ⚠️  Warmup failed: {e}")

if __name__ == "__main__":
    print("[TEST] screen_processor.py")
    print("=" * 52)
    mode = input("angle — screen / camera (default: screen): ").strip().lower() or "screen"
    q    = input("Question (Enter = default): ").strip() or "What do you see? Be brief."

    t0 = time.perf_counter()
    warmup_session()
    print(f"Session ready in {time.perf_counter()-t0:.2f}s\n")

    t1 = time.perf_counter()
    ok = screen_process({"angle": mode, "text": q})
    print(f"Queued in {time.perf_counter()-t1:.3f}s — waiting for audio...")
    time.sleep(10)
    print("Done." if ok else "Failed.")