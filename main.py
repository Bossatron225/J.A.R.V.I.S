import platform as _platform
import subprocess as _subprocess
import sys
import warnings 

if sys.version_info < (3, 10):
    sys.stderr.write(
        "ERROR: MARK L requires Python 3.10 or newer.\n"
        f"Current interpreter: {sys.version}\n"
        "Please install Python 3.10+ and run: python3 main.py\n"
    )
    sys.exit(1)

# NumPy 2.5 warns about in-place shape assignment used internally by
# current sounddevice releases. Keep logs clean until upstream updates.
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module=r"sounddevice",
)
warnings.filterwarnings(
    "ignore",
    message=r".*Setting the shape on a NumPy array has been deprecated in NumPy 2\.5\..*",
    category=DeprecationWarning,
)

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from functools import lru_cache

import sounddevice as sd
import speech_recognition as sr
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, get_recent_sessions,
)
from memory.obsidian_memory import (
    remember_user_fact,
    recall_user_profile,
    build_personal_memory_context,
    recall_personal_memory,
)
from memory.remote_sync import (
    load_memory_with_vps_sync,
    push_memory_to_vps,
)
from local_worker import LocalWorker
from memory.document_ingestion import (
    ingest_document,
    index_codebase,
    recall_document_details,
    search_document_index,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.google_calendar   import google_calendar
from actions.computer_settings import computer_settings
from actions.wiz_lights        import wiz_lights
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.screen_processor  import _capture_targeted_visual
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import (
    file_controller,
    enroll_biometric_profile,
    get_authorized_profiles,
    verify_biometric_security,
)
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.dev_agent         import REBOOT_MARKER
from actions.web_search        import (
    web_search as web_search_action,
    manage_interest_profile,
)
from actions.workspace_agent   import workspace_agent
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.predictive_automation import PredictiveAutomationDaemon
from actions.visual_monitor import VisualMonitorRegistry
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from actions.imessage_integration import (
    imessage_control,
    poll_imessage_alerts,
    get_imessage_monitor_interval,
    imessage_monitor_start,
    send_imessage,
)
from actions.mail_integration import (
    mail_control,
    poll_mail_alerts,
    get_mail_monitor_interval,
    mail_monitor_start,
)
from actions.find_my import find_my
from actions.alexa_routines import alexa_routines, ifttt_webhooks
from memory.config_manager     import get_brief_enabled
from core.tts import create_tts_player, reset_audio_output
from core.context_optimizer.context import ContextManager as _CtxMgr
from core.context_optimizer.optimizer import ToolExecutionOptimizer as _ToolOptimizer


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODELS = (
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
)
SESSION_SUMMARY_MODELS = (
    "models/gemini-flash-lite-latest",
    "models/gemini-flash-latest",
    "models/gemini-3.5-flash-lite",
)
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000

AUDIO_TUNING_PROFILES = {
    "aggressive": {
        "mic_chunk_size": 320,
        "speaker_chunk_size": 480,
        "mic_queue_max_chunks": 16,
        "speaker_queue_max_chunks": 64,
        "phone_idle_timeout_seconds": 0.22,
        "incoming_slice_bytes": 1920,
        "play_batch_cap_bytes": 3840,
        "input_latency": "low",
        "output_latency": "low",
        "speaker_drop_policy": "drop_oldest",
        "speaker_put_timeout_seconds": 0.06,
        "diag_interval_seconds": 1.0,
    },
    "balanced": {
        "mic_chunk_size": 512,
        "speaker_chunk_size": 960,
        "mic_queue_max_chunks": 24,
        "speaker_queue_max_chunks": 160,
        "phone_idle_timeout_seconds": 0.35,
        "incoming_slice_bytes": 4800,
        "play_batch_cap_bytes": 4800,
        "input_latency": "low",
        "output_latency": "low",
        "speaker_drop_policy": "preserve",
        "speaker_put_timeout_seconds": 0.2,
        "diag_interval_seconds": 1.0,
    },
    "safe": {
        "mic_chunk_size": 768,
        "speaker_chunk_size": 1440,
        "mic_queue_max_chunks": 40,
        "speaker_queue_max_chunks": 260,
        "phone_idle_timeout_seconds": 0.6,
        "incoming_slice_bytes": 4800,
        "play_batch_cap_bytes": 7200,
        "input_latency": "high",
        "output_latency": "high",
        "speaker_drop_policy": "preserve",
        "speaker_put_timeout_seconds": 0.3,
        "diag_interval_seconds": 1.5,
    },
}

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


@lru_cache(maxsize=4)
def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, James Lumsden's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _error_text(err: BaseException | Exception) -> str:
    """Collect nested exception messages (incl. ExceptionGroup children)."""
    seen: set[int] = set()
    stack: list[BaseException] = [err]
    parts: list[str] = []

    while stack:
        cur = stack.pop()
        if cur is None:
            continue
        cur_id = id(cur)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        parts.append(str(cur))

        cause = getattr(cur, "__cause__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)

        context = getattr(cur, "__context__", None)
        if isinstance(context, BaseException):
            stack.append(context)

        group_children = getattr(cur, "exceptions", None)
        if isinstance(group_children, tuple):
            for child in group_children:
                if isinstance(child, BaseException):
                    stack.append(child)

    return "\n".join(parts).lower()


def _is_live_model_unavailable_error(err: Exception) -> bool:
    msg = _error_text(err)
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


def _is_live_audio_unsupported_error(err: Exception | BaseException) -> bool:
    msg = _error_text(err)
    return any(k in msg for k in (
        "content_type_audio",
        "audio content type",
        "not supported for this model configuration",
        "received 1007",
    ))


def _is_billing_error(err: Exception | BaseException) -> bool:
    msg = _error_text(err)
    return any(k in msg for k in (
        "prepayment credits are depleted",
        "credits are depleted",
        "credits depleted",
        "billing",
        "quota exceeded",
        "resource exhausted",
        "rate limit",
        "payment required",
        "insufficient funds",
        "limit exceeded",
        "429",
    ))


def _is_disconnect_error(err: Exception | BaseException) -> bool:
    if _is_billing_error(err):
        return False
    msg = _error_text(err)
    return any(k in msg for k in (
        "keepalive ping timeout",
        "connectionclosederror",
        "connection closed",
        "no close frame received",
        "1011",
        "websocket connection is closed",
        "disconnect",
    ))

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer, including future apps you install on macOS when you provide the app's name. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "App name or bundle name to launch (e.g. 'WhatsApp', 'Chrome', 'Spotify', 'TablePlus')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "manage_interest_profile",
        "description": (
            "Manages personalized search/news interests inferred from Safari history. "
            "Use this to show the current inferred profile and to edit allow/block topic lists. "
            "Use actions: show, allow_add, allow_remove, allow_clear, "
            "block_add, block_remove, block_clear, enable, disable, refresh."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "show | allow_add | allow_remove | allow_clear | "
                        "block_add | block_remove | block_clear | enable | disable | refresh"
                    )
                },
                "topic": {
                    "type": "STRING",
                    "description": "Topic/category to allow or block (for add/remove actions)"
                }
            },
            "required": []
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, iMessage, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: iMessage, WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "imessage_control",
        "description": (
            "macOS iMessage control: read latest messages, read unread messages, "
            "and enable/disable background iMessage alerts. "
            "Use this for any request to read iMessages or manage iMessage monitoring."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "send | read_latest | read_unread | monitor_start | monitor_stop | monitor_status"
                },
                "receiver": {"type": "STRING", "description": "Recipient for send action"},
                "message_text": {"type": "STRING", "description": "Message text for send action"},
                "limit": {"type": "INTEGER", "description": "How many messages to read (default: 5)"},
                "interval_seconds": {
                    "type": "INTEGER",
                    "description": "Polling interval for monitor_start (default: 15, min: 5)"
                },
            },
            "required": ["action"]
        }
    },
    {
        "name": "mail_control",
        "description": (
            "macOS Apple Mail control: read latest mail, read unread mail, "
            "and enable or disable background new-mail alerts. "
            "Use this for requests to read email or manage spoken mail announcements."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "read_latest | read_unread | monitor_start | monitor_stop | monitor_status"
                },
                "limit": {
                    "type": "INTEGER",
                    "description": "How many messages to read (default: 5)"
                },
                "interval_seconds": {
                    "type": "INTEGER",
                    "description": "Polling interval for monitor_start (default: 30, min: 10)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "find_my",
        "description": (
            "Find My access on macOS for your own devices and people who already share location with you. "
            "Use this when the user asks where their iPhone/Mac/AirPods are, or where shared contacts are. "
            "Supports opening the Find My app and shortcut-based location lookups."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "open | web_open | open_and_read | status | people | devices | all | locate | where | setup"
                },
                "scope": {
                    "type": "STRING",
                    "description": "people | devices | all (used by action=status)"
                },
                "preferred": {
                    "type": "STRING",
                    "description": "app | web (used by open_and_read to choose Find My app or iCloud Find)"
                },
                "target": {
                    "type": "STRING",
                    "description": "Person or device name for locate/where, e.g. 'James iPhone'"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "ifttt_webhooks",
        "description": (
            "Triggers IFTTT or other webhook-driven smart-home automations. "
            "Use this for controlling smart plugs through IFTTT-to-Smart Life or similar webhook flows, including named protocols like ventilation_protocol."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "setup | help | provider | set_provider | set_auth | set_routine | set_plug | list | trigger | on | off | status | initiate | disengage"
                },
                "provider": {
                    "type": "STRING",
                    "description": "webhook"
                },
                "target": {
                    "type": "STRING",
                    "description": "Protocol or plug alias for on/off/status, e.g. ventilation_protocol or smart_plug_1"
                },
                "routine": {
                    "type": "STRING",
                    "description": "Routine name for trigger/set_routine"
                },
                "url": {
                    "type": "STRING",
                    "description": "Webhook URL for set_routine"
                },
                "method": {
                    "type": "STRING",
                    "description": "HTTP method for set_routine (default POST)"
                },
                "auth_header": {
                    "type": "STRING",
                    "description": "Auth header name for webhook requests"
                },
                "auth_token": {
                    "type": "STRING",
                    "description": "Auth token value for webhook requests"
                },
                "alias": {
                    "type": "STRING",
                    "description": "Plug alias for set_plug"
                },
                "on_routine": {
                    "type": "STRING",
                    "description": "Routine name mapped to plug on"
                },
                "off_routine": {
                    "type": "STRING",
                    "description": "Routine name mapped to plug off"
                },
                "status_routine": {
                    "type": "STRING",
                    "description": "Optional routine name mapped to plug status"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "google_calendar",
        "description": (
            "Integrates with Google Calendar for setup, status checks, event lookup, and event creation. "
            "Use this whenever the user asks about their calendar, schedule, events, or adding a meeting to Google Calendar."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "help | setup | reauth | status | open | today | upcoming | list | create | add | delete | remove"
                },
                "calendar_id": {
                    "type": "STRING",
                    "description": "Calendar ID to target. Default: primary"
                },
                "max_results": {
                    "type": "INTEGER",
                    "description": "How many events to return for today/upcoming/list"
                },
                "title": {
                    "type": "STRING",
                    "description": "Event title for create/add"
                },
                "date": {
                    "type": "STRING",
                    "description": "Event date in YYYY-MM-DD format"
                },
                "time": {
                    "type": "STRING",
                    "description": "Event start time in HH:MM 24h format"
                },
                "duration_minutes": {
                    "type": "INTEGER",
                    "description": "Event duration in minutes for create/add (default: 60)"
                },
                "location": {
                    "type": "STRING",
                    "description": "Optional event location"
                },
                "description": {
                    "type": "STRING",
                    "description": "Optional event description"
                },
                "event_id": {
                    "type": "STRING",
                    "description": "Event ID for delete/remove"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures a full screen, a specific window/app, a specific browser tab, or a webcam image and lets you analyze it. "
            "Use this when the user asks what is on screen, what you see, look at camera, inspect a browser tab, inspect a window, or analyze a specific app. "
            "You have NO visual ability without this tool. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "Backward-compatible alias for target_type. 'screen' or 'camera'."},
                "target_type": {"type": "STRING", "description": "screen | camera | window | app | tab | browser_tab"},
                "browser": {"type": "STRING", "description": "Browser name for tab capture: chrome | edge | firefox | safari | brave | opera | vivaldi"},
                "target": {"type": "STRING", "description": "Tab title/url fragment or general visual target label"},
                "index": {"type": "INTEGER", "description": "1-based browser tab index for tab capture"},
                "window_title": {"type": "STRING", "description": "Window title fragment for focused app/window capture"},
                "app_name": {"type": "STRING", "description": "App name for focused app/window capture"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "visual_watch",
        "description": (
            "Registers, lists, removes, or clears live visual targets so Jarvis can keep watching specific browser tabs, apps, or windows over time. "
            "Use this when the user wants a persistent monitor instead of a one-off screenshot."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "add | list | remove | clear | status"},
                "target_type": {"type": "STRING", "description": "tab | browser_tab | window | app | screen"},
                "browser": {"type": "STRING", "description": "Browser name for tab watching"},
                "target": {"type": "STRING", "description": "Tab title/url fragment or generic target label"},
                "index": {"type": "INTEGER", "description": "1-based browser tab index"},
                "app_index": {"type": "INTEGER", "description": "1-based app index from computer_settings action=list_apps output"},
                "window_title": {"type": "STRING", "description": "Window title fragment for native app/window watching"},
                "app_name": {"type": "STRING", "description": "App name for native app/window watching"},
                "label": {"type": "STRING", "description": "Friendly label for the watch target"},
                "interval_seconds": {"type": "NUMBER", "description": "Polling interval for this watch target"},
                "enabled": {"type": "BOOLEAN", "description": "Enable or disable the watch target"},
                "target_id": {"type": "STRING", "description": "Existing target ID for remove/status"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, Safari tab listing/closing, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform (for example: close_app, close_tab, list_tabs, list_safari_tabs, list_apps, list_windows, next_tab, prev_tab, refresh_page, screenshot, fullscreen)"},
                "app_name":    {"type": "STRING", "description": "Specific app to close or target, for example Safari, Chrome, Finder"},
                "target":      {"type": "STRING", "description": "Optional window/app target name for close_window or close_app"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "wiz_lights",
        "description": (
            "Controls WiZ smart lights on your local network. "
            "Use for turning lights on/off, setting brightness, color, and checking status. "
            "Use action=discover to find light IPs first if needed."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "discover | list | status | on | off | set | set_alias | remove_alias | list_aliases"
                },
                "target": {
                    "type": "STRING",
                    "description": "Target scope: all | cache. Default: all (uses cache first, then network discover if needed)"
                },
                "ip": {
                    "type": "STRING",
                    "description": "Single WiZ bulb IP address"
                },
                "ips": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of bulb IPs"
                },
                "name": {
                    "type": "STRING",
                    "description": "Single saved bulb alias/name (for example desk_lamp)"
                },
                "names": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of saved bulb aliases/names"
                },
                "alias": {
                    "type": "STRING",
                    "description": "Alias label used by set_alias/remove_alias"
                },
                "brightness": {
                    "type": "INTEGER",
                    "description": "Brightness level 1-100"
                },
                "color": {
                    "type": "STRING",
                    "description": "Color name (red), hex (#FF0000), or RGB string (255,0,0)"
                },
                "kelvin": {
                    "type": "INTEGER",
                    "description": "Color temperature in Kelvin (2200-6500)"
                },
                "refresh": {
                    "type": "BOOLEAN",
                    "description": "Force a fresh network discovery instead of using cached bulb IPs"
                },
                "cache_only": {
                    "type": "BOOLEAN",
                    "description": "Use only cached bulbs and do not auto-discover when cache is empty"
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, tab listing, tab switching, any web-based task. "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | list_tabs | switch_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all | close_safari_all | close_browser_all_tabs"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "index":       {"type": "INTEGER", "description": "1-based tab index for list_tabs / switch_tab / close_tab"},
                "target":      {"type": "STRING", "description": "Tab title, URL fragment, or tab number as text for switch_tab / close_tab"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch or safely improves Jarvis's own source code. For self-improvement, it supports approval-based propose/apply workflows and sandbox validation before workspace writes.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do, what Jarvis should improve, or which new features to explore"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
                "self_improve": {"type": "BOOLEAN", "description": "Set to true to let Jarvis improve its own source code safely and suggest enhancements"},
                "target_files": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Optional repo-relative Python files to improve"},
                "sandbox":      {"type": "BOOLEAN", "description": "Run validation in a temporary sandbox before applying changes (default: true)"},
                "require_approval": {"type": "BOOLEAN", "description": "For self_improve: true queues a proposal that requires explicit apply approval (default: true)"},
                "approval_action": {"type": "STRING", "description": "For self_improve: propose | apply | status | clear"},
                "approval_id": {"type": "STRING", "description": "Approval ID returned by propose, used by apply or clear"},
                "self_reboot": {"type": "BOOLEAN", "description": "For self_improve apply runs: restart Jarvis automatically after successful code writes (default: true)"},
                "integrate_project": {"type": "BOOLEAN", "description": "Set to true to analyse a JarvisProjects project and generate a plan to integrate it into Jarvis's own codebase"},
            },
            "required": []
        }
    },
    {
        "name": "workspace_agent",
        "description": (
            "Repo-scoped coding agent for real code changes. "
            "Use it to search files, read code ranges, write/replace text, run safe dev commands, "
            "and trigger sandboxed self-improvement."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "status | list_files | search | read_file | write_file | append_file | "
                        "replace_text | run_command | improve_self"
                    )
                },
                "path": {"type": "STRING", "description": "Repo-relative file path"},
                "glob": {"type": "STRING", "description": "Glob for list_files (optional)"},
                "query": {"type": "STRING", "description": "Search text for search action"},
                "include": {"type": "STRING", "description": "Include glob for search action"},
                "start_line": {"type": "INTEGER", "description": "1-based start line for read_file"},
                "end_line": {"type": "INTEGER", "description": "1-based end line for read_file"},
                "content": {"type": "STRING", "description": "File content for write/append"},
                "overwrite": {"type": "BOOLEAN", "description": "Set true to overwrite existing file"},
                "old_text": {"type": "STRING", "description": "Text to replace"},
                "new_text": {"type": "STRING", "description": "Replacement text"},
                "replace_all": {"type": "BOOLEAN", "description": "Replace all matches (default false)"},
                "command": {"type": "STRING", "description": "Safe shell command for run_command"},
                "timeout": {"type": "INTEGER", "description": "Timeout seconds for run/improve_self"},
                "description": {"type": "STRING", "description": "Goal/description for improve_self"},
                "target_files": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional repo-relative files for improve_self"
                },
                "require_approval": {"type": "BOOLEAN", "description": "For improve_self: true queues a proposal and waits for explicit apply (default: true)"},
                "approval_action": {"type": "STRING", "description": "For improve_self: propose | apply | status | clear"},
                "approval_id": {"type": "STRING", "description": "Approval ID from propose for apply/clear"},
                "self_reboot": {"type": "BOOLEAN", "description": "For improve_self apply: restart Jarvis automatically after successful code writes (default: true)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file or bundle of files that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to uploaded file(s) and wants an action on them. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to a single uploaded file. Leave empty to use the currently uploaded file."
            },
            "file_paths": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional list of uploaded file paths for cross-analysis or batch processing."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze\n"
                    "multi-file analysis: analyze | summarize | compare | cross_analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "style": {
                "type": "STRING",
                "description": "Optional essay voice: student | professional | casual"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory."
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "obsidian_memory",
        "description": (
            "Appends or reads a permanent local memory note in the user's Obsidian vault on macOS. "
            "Use this to store durable facts about the user or recall them later."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "save | recall"},
                "category": {"type": "STRING", "description": "Fact category such as Preferences, Projects, or Notes"},
                "fact": {"type": "STRING", "description": "Fact to store in the Obsidian memory vault"},
            },
            "required": []
        }
    },
    {
        "name": "personal_memory",
        "description": (
            "Searches both the structured JSON memory and the local Obsidian memory for a user-specific fact. "
            "Use this whenever the user asks about preferences, plans, relationships, or past facts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Short phrase or topic to search for in personal memory"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "document_memory",
        "description": (
            "Indexes local PDFs, markdown notes, and codebases for later recall. "
            "Use this to read, summarize, and search local knowledge stores when the user references files or asks for details from them."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "ingest_file | ingest_codebase | search | recall"},
                "path": {"type": "STRING", "description": "File or directory to index"},
                "query": {"type": "STRING", "description": "Search term or question to match against the index"},
                "source_name": {"type": "STRING", "description": "Optional label for the source document"},
                "limit": {"type": "INTEGER", "description": "How many matches to return for search/recall"},
            },
            "required": []
        }
    },
    {
        "name": "security_biometrics",
        "description": (
            "Upgraded Stark Security Protocol: Performs real-time voice print recognition and visual facial person detection "
            "for enhanced security clearances, access authorization, and user personalization."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "verify_voice | detect_person | enroll | calibrate | status"
                },
                "target_identity": {
                    "type": "STRING",
                    "description": "Expected identity to match (e.g. 'James Lumsden', 'Authorized User')"
                },
                "name": {
                    "type": "STRING",
                    "description": "Display name to enroll or verify"
                },
                "voice_print": {
                    "type": "STRING",
                    "description": "Voice sample text or signature to enroll or verify"
                },
                "visual_signature": {
                    "type": "STRING",
                    "description": "Visual signature text or descriptor to enroll or verify"
                },
                "profile_id": {
                    "type": "STRING",
                    "description": "Identifier for the enrolled profile"
                },
                "make_primary": {
                    "type": "BOOLEAN",
                    "description": "Whether the new profile should become the primary profile"
                },
                "clearance_level": {
                    "type": "STRING",
                    "description": "Security clearance level (e.g. omega, alpha)"
                }
            },
            "required": ["action"]
        }
    }
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_remote_url_clicked = self._get_live_remote_url
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_biometric_failure = self._handle_biometric_failure
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._last_public_url = None
        self._boot_remote_notice_sent = False
        self._boot_remote_notice_enabled, self._boot_remote_notice_receiver = self._load_boot_remote_notice_config()
        self._wake_protocol_cfg = self._load_imessage_wake_config()
        self._shutdown_protocol_cfg = self._load_imessage_shutdown_config()
        self._mail_monitor_cfg = self._load_mail_monitor_config()
        self._audio_profile_name, self._audio_cfg = self._load_audio_tuning_config()
        self._audio_diag = {
            "mic_enqueued": 0,
            "mic_dropped": 0,
            "speaker_enqueued": 0,
            "speaker_dropped": 0,
            "speaker_received_bytes": 0,
            "speaker_played_bytes": 0,
            "interrupt_drained_chunks": 0,
            "phone_enqueued": 0,
            "phone_dropped": 0,
        }
        self._last_wake_protocol_ts = 0.0
        self._last_wake_protocol_rowid = 0
        self._last_shutdown_protocol_ts = 0.0
        self._last_shutdown_protocol_rowid = 0
        self._shutdown_in_progress = False
        self._reboot_in_progress = False
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._predictive_daemon = PredictiveAutomationDaemon(API_CONFIG_PATH)
        self._predictive_cfg = self._load_predictive_config()
        self._diag_stream_cfg = self._load_diagnostics_stream_config()
        self._visual_watch_cfg = self._load_visual_watch_config()
        self._visual_monitor = VisualMonitorRegistry(API_CONFIG_PATH)
        self._visual_error_seen: dict[str, tuple[str, float]] = {}
        self._started_ts = time.time()
        self._current_live_model: str | None = None
        self._live_model_backoff_until: dict[str, float] = {}
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self._tts_player = None
        self._use_external_tts = False
        self._tts_sentence_queue: asyncio.Queue[str | None] | None = None
        self._tts_pending_sentence = ""  # partial sentence awaiting next chunk
        # Per-session tool result cache (avoids redundant API calls for same args)
        self._tool_optimizer = _ToolOptimizer(_CtxMgr(max_context_window=256))
        self._speech_recognizer: sr.Recognizer | None = None
        self._speech_mic: sr.Microphone | None = None
        self._wake_phrases = ["jarvis", "service", "jarvis service", "jarvis wake", "wake up jarvis", "hey jarvis"]
        self._speech_listener_running = False
        self._local_worker = None
        self._vps_link_established_said = False

    def _print_startup_banner(self) -> None:
        vps_url = (os.getenv("JARVIS_VPS_URL") or "").strip()
        public_url = (os.getenv("JARVIS_PUBLIC_URL") or "").strip()
        tunnel_enabled = (os.getenv("JARVIS_ENABLE_TUNNEL") or "").strip().lower() in ("1", "true", "yes", "on")

        print("\n" + "=" * 72)
        print("JARVIS STARTUP STATUS")
        print(f"VPS brain: {vps_url or 'not configured'}")
        if public_url:
            print(f"Remote/public URL: {public_url}")
        elif tunnel_enabled:
            print("Remote/public URL: tunnel enabled but URL not resolved yet")
        else:
            print("Remote/public URL: disabled")
        print("=" * 72)

        if vps_url:
            self.ui.write_log(f"SYS: VPS link configured: {vps_url}")
        if public_url:
            self.ui.write_log(f"SYS: Public remote URL: {public_url}")

    @staticmethod
    def _load_runtime_config() -> dict:
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _external_tts_enabled(config: dict) -> bool:
        return str(config.get("tts_engine", "")).strip().lower() in {
            "edgetts",
            "kokoro",
            "elevenlabs",
        }

    @staticmethod
    def _external_tts_label(config: dict) -> str:
        engine_name = str(config.get("tts_engine", "")).strip().lower()
        return {
            "edgetts": "EdgeTTS",
            "kokoro": "Kokoro",
            "elevenlabs": "ElevenLabs",
        }.get(engine_name, engine_name or "external TTS")

    async def _tts_worker(self) -> None:
        """Serialises external TTS calls; speaks one sentence at a time."""
        while True:
            text = await self._tts_sentence_queue.get()
            if text is None:  # sentinel
                return
            try:
                await self._speak_external_tts(text)
            except Exception as e:
                print(f"[TTS] Worker error: {e}")

    def _enqueue_tts_sentence(self, sentence: str) -> None:
        """Queue a sentence for external TTS; drops if the queue is not available."""
        if self._tts_sentence_queue and sentence.strip():
            try:
                self._tts_sentence_queue.put_nowait(sentence.strip())
            except asyncio.QueueFull:
                pass

    async def _speak_external_tts(self, text: str) -> None:
        if not text or not self._tts_player:
            return
        self.set_speaking(True)
        try:
            await asyncio.to_thread(self._tts_player.speak, text)
        except RuntimeError as e:
            if "cannot schedule new futures after shutdown" in str(e).lower():
                return
            raise
        finally:
            self.set_speaking(False)

    async def _play_external_audio_fallback(self, chunks: list[bytes]) -> None:
        if not chunks:
            return

        def _play_raw_pcm16(data_chunks: list[bytes]) -> None:
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=int(self._audio_cfg.get("speaker_chunk_size", 960)),
                latency=self._audio_cfg.get("output_latency", "low"),
            )
            stream.start()
            try:
                for chunk in data_chunks:
                    if chunk:
                        stream.write(chunk)
            finally:
                stream.stop()
                stream.close()

        self.set_speaking(True)
        try:
            await asyncio.to_thread(_play_raw_pcm16, chunks)
        finally:
            self.set_speaking(False)

    @staticmethod
    def _load_boot_remote_notice_config() -> tuple[bool, str]:
        enabled = True
        receiver = "0833592353"
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            enabled = bool(cfg.get("boot_imessage_remote_enabled", True))
            receiver = str(cfg.get("boot_imessage_remote_receiver", receiver) or "").strip()
        except Exception:
            pass
        return enabled, receiver

    def _send_boot_remote_access_imessage(self, url: str) -> None:
        if self._boot_remote_notice_sent:
            return
        if not self._boot_remote_notice_enabled:
            return
        if not self._boot_remote_notice_receiver:
            self.ui.write_log("SYS: Boot remote notice skipped.")
            return
        if self._dashboard is None:
            return

        try:
            if not self._boot_remote_notice_receiver:
                self.ui.write_log("SYS: Boot remote notice skipped.")
                return
            key = self._dashboard.new_key() if hasattr(self._dashboard, "new_key") else ""
            security = self._dashboard.get_remote_security_status() if hasattr(self._dashboard, "get_remote_security_status") else "SECURITY: STATUS UNAVAILABLE"
            message_lines = [
                "JARVIS remote is online.",
                f"Open: {url}",
            ]
            if key:
                message_lines.append(f"Key: {key}")
            auto_login = self._dashboard.get_auto_login_url(key) if hasattr(self._dashboard, "get_auto_login_url") else ""
            if auto_login:
                message_lines.append(f"Auto: {auto_login}")
            message_lines.append(f"Status: online")
            message_lines.append(security)
            result = send_imessage(self._boot_remote_notice_receiver, "\n".join(message_lines))
            self.ui.write_log("SYS: Boot remote notice sent.")
            if "sent" in result.lower():
                self._boot_remote_notice_sent = True
        except Exception as e:
            self.ui.write_log(f"ERR: Boot remote iMessage failed — {e}")

    @staticmethod
    def _run_osascript_probe(script: str, timeout_seconds: int = 6) -> tuple[bool, str]:
        try:
            proc = _subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=max(2, int(timeout_seconds or 6)),
            )
            if proc.returncode == 0:
                return True, "ok"
            details = (proc.stderr or proc.stdout or "unknown osascript error").strip()
            return False, details
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _looks_like_automation_denied(details: str) -> bool:
        text = (details or "").lower()
        markers = (
            "not authorized",
            "not permitted",
            "privacy",
            "-1743",
            "operation not permitted",
            "authorization denied",
        )
        return any(marker in text for marker in markers)

    def _run_macos_permission_preflight(self) -> None:
        if _platform.system() != "Darwin":
            return

        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        if not bool(cfg.get("permissions_preflight_on_start", True)):
            return

        self.ui.write_log("SYS: Checking permissions...")
        caller_exec = str(Path(sys.executable).resolve())
        issues: list[tuple[str, str]] = []

        automation_probes = [
            ("Mail", 'tell application "Mail" to get name'),
            ("Contacts", 'tell application "Contacts" to get name'),
            ("Messages", 'tell application "Messages" to get name'),
        ]
        for app_name, script in automation_probes:
            ok, details = self._run_osascript_probe(script, timeout_seconds=6)
            if not ok:
                if self._looks_like_automation_denied(details):
                    issues.append((
                        f"Automation denied for {app_name}",
                        "System Settings > Privacy & Security > Automation",
                    ))
                else:
                    issues.append((f"{app_name} probe failed: {details[:180]}", "check app state/permissions"))

        db_path = Path.home() / "Library" / "Messages" / "chat.db"
        if not db_path.exists():
            issues.append(("Messages database not found", str(db_path)))
        else:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    conn.execute("SELECT 1 FROM message LIMIT 1").fetchone()
                finally:
                    conn.close()
            except Exception as e:
                issues.append((
                    f"Messages DB read failed: {str(e)[:180]}",
                    "System Settings > Privacy & Security > Full Disk Access",
                ))

        if not issues:
            self.ui.write_log("SYS: Permissions OK.")
            return

        self.ui.write_log("SYS: Permission preflight found issues. Apply this once:")
        self.ui.write_log(f"SYS: 1) Use one stable interpreter for JARVIS: {caller_exec}")
        self.ui.write_log("SYS: 2) Privacy & Security > Automation: allow your JARVIS caller to control Mail, Contacts, and Messages")
        self.ui.write_log("SYS: 3) Privacy & Security > Full Disk Access: allow the same caller")
        self.ui.write_log("SYS: 4) Keep launching JARVIS the same way each time (same app + same Python path)")
        for idx, (problem, hint) in enumerate(issues[:6], start=1):
            self.ui.write_log(f"SYS:   - Issue {idx}: {problem} | Hint: {hint}")

    def _configure_imessage_cold_start_bridge(self) -> None:
        """Install/update launchd bridge so an iMessage can cold-start JARVIS."""
        label = "com.jarvis.imessage.wakebridge"
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_path = plist_dir / f"{label}.plist"
        service = f"gui/{os.getuid()}/{label}"

        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        # Keep a stable interpreter identity for launchd/macOS TCC permissions.
        for candidate in (
            BASE_DIR / ".venv-1" / "bin" / "python",
            BASE_DIR / ".venv" / "bin" / "python",
            BASE_DIR / ".venv-1" / "bin" / "python3",
            BASE_DIR / ".venv" / "bin" / "python3",
        ):
            if candidate.exists():
                stable_exec = str(candidate)
                break
        else:
            configured_exec = str(cfg.get("imessage_cold_start_python", "") or "").strip()
            remembered_exec = str(cfg.get("jarvis_python_exec", "") or "").strip()
            stable_exec = configured_exec or remembered_exec or str(Path(sys.executable).resolve())
            try:
                stable_exec = str(Path(stable_exec).expanduser())
            except Exception:
                stable_exec = str(Path(sys.executable).resolve())
            if not Path(stable_exec).exists():
                stable_exec = str(Path(sys.executable).resolve())

        cfg_changed = False
        if str(cfg.get("jarvis_python_exec", "") or "").strip() != stable_exec:
            cfg["jarvis_python_exec"] = stable_exec
            cfg_changed = True
        if str(cfg.get("imessage_cold_start_python", "") or "").strip() != stable_exec:
            cfg["imessage_cold_start_python"] = stable_exec
            cfg_changed = True

        # Preserve existing API keys and other user settings when updating config.
        if cfg_changed:
            try:
                with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
            except Exception as e:
                self.ui.write_log(f"ERR: Could not persist stable Python path in config — {e}")

        enabled = bool(cfg.get("imessage_cold_start_enabled", True))

        def _launchctl(args: list[str]) -> None:
            try:
                _subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=10)
            except Exception:
                pass

        def _service_loaded() -> bool:
            try:
                proc = _subprocess.run(
                    ["launchctl", "print", service],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return proc.returncode == 0
            except Exception:
                return False

        if not enabled:
            _launchctl(["bootout", service])
            try:
                plist_path.unlink(missing_ok=True)
                self.ui.write_log("SYS: iMessage cold-start bridge disabled.")
            except Exception as e:
                self.ui.write_log(f"ERR: Could not remove cold-start bridge plist — {e}")
            return

        bridge_script = BASE_DIR / "actions" / "imessage_cold_start_bridge.py"
        if not bridge_script.exists():
            self.ui.write_log("ERR: iMessage cold-start bridge script missing.")
            return

        plist_dir.mkdir(parents=True, exist_ok=True)
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>{label}</string>\n'
            '  <key>ProgramArguments</key><array>\n'
            f'    <string>{stable_exec}</string>\n'
            f'    <string>{bridge_script}</string>\n'
            '  </array>\n'
            f'  <key>WorkingDirectory</key><string>{BASE_DIR}</string>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '</dict></plist>\n'
        )

        plist_changed = False
        try:
            current = plist_path.read_text(encoding="utf-8") if plist_path.exists() else ""
            if current != plist:
                plist_path.write_text(plist, encoding="utf-8")
                plist_changed = True
        except Exception as e:
            self.ui.write_log(f"ERR: Could not write cold-start plist — {e}")
            return

        # Never drop/restart the bridge unconditionally; only recycle when plist changed.
        if plist_changed:
            _launchctl(["bootout", service])
            _launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
            _launchctl(["kickstart", "-k", service])
        elif not _service_loaded():
            # Only start when missing; avoid killing an in-flight wake launch.
            _launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
        try:
            current_exec_resolved = str(Path(sys.executable).expanduser().resolve())
            stable_exec_resolved = str(Path(stable_exec).expanduser().resolve())
        except Exception:
            current_exec_resolved = str(Path(sys.executable).expanduser())
            stable_exec_resolved = str(Path(stable_exec).expanduser())

        if current_exec_resolved != stable_exec_resolved:
            self.ui.write_log(
                "SYS: Note: running with a different Python binary than the pinned "
                "launchd interpreter; macOS permissions may differ for this session."
            )
        self.ui.write_log("SYS: Wake bridge ready.")

    @staticmethod
    def _cold_wake_bridge_health_path() -> Path:
        return Path.home() / "Library" / "Application Support" / "JARVIS" / "imessage_wake_bridge_health.json"

    @staticmethod
    def _cold_wake_bridge_service() -> str:
        return f"gui/{os.getuid()}/com.jarvis.imessage.wakebridge"

    def _cold_wake_bridge_enabled(self) -> bool:
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return bool(cfg.get("imessage_cold_start_enabled", True))
        except Exception:
            return True

    def _cold_wake_bridge_monitor_interval(self) -> int:
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return max(5, min(int(cfg.get("imessage_monitor_interval_seconds", 15) or 15), 300))
        except Exception:
            return 15

    def _probe_cold_wake_bridge_status(self) -> tuple[str, str, str]:
        if _platform.system() != "Darwin":
            return ("Wake Bridge: n/a", "off", "Cold-start wake bridge is only available on macOS.")

        if not self._cold_wake_bridge_enabled():
            return ("Wake Bridge: off", "off", "Set imessage_cold_start_enabled=true in config/api_keys.json.")

        service = self._cold_wake_bridge_service()
        proc = _subprocess.run(
            ["launchctl", "print", service],
            capture_output=True,
            text=True,
            timeout=8,
        )
        out = proc.stdout or ""
        if proc.returncode != 0:
            err = (proc.stderr or out or "launchctl could not read service state").strip()
            return ("Wake Bridge: down", "bad", err[:240])

        state_match = re.search(r"^\s*state\s*=\s*([^\n]+)$", out, re.MULTILINE)
        pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", out, re.MULTILINE)
        state = (state_match.group(1).strip().lower() if state_match else "unknown")
        pid = (pid_match.group(1).strip() if pid_match else "?")
        if state != "running":
            return (f"Wake Bridge: {state}", "warn", f"launchd state={state}, pid={pid}")

        health_path = self._cold_wake_bridge_health_path()
        if health_path.exists():
            try:
                with open(health_path, "r", encoding="utf-8") as f:
                    health = json.load(f)
            except Exception as e:
                return ("Wake Bridge: running", "warn", f"Could not read health file: {e}")

            status = str(health.get("status", "running") or "running").strip().lower()
            ts = float(health.get("ts") or 0.0)
            age = max(0, int(time.time() - ts)) if ts else None
            interval = self._cold_wake_bridge_monitor_interval()
            stale_after = max(45, interval * 3)

            if age is not None and age > stale_after:
                return (
                    f"Wake Bridge: stale ({age}s)",
                    "bad",
                    f"No health update for {age}s (expected < {stale_after}s).",
                )
            if status == "disabled":
                return ("Wake Bridge: off", "off", "Cold-start bridge is disabled by config.")
            if status in {"error", "stalled"}:
                err = str(health.get("error") or "bridge reported an internal error")
                return (f"Wake Bridge: {status}", "bad", err[:240])

            if age is not None:
                return (
                    f"Wake Bridge: ok ({age}s)",
                    "ok",
                    f"launchd state={state}, pid={pid}, last heartbeat {age}s ago.",
                )

        return ("Wake Bridge: running", "ok", f"launchd state={state}, pid={pid}")

    async def _run_cold_wake_bridge_health(self) -> None:
        while True:
            try:
                text, level, tooltip = await asyncio.to_thread(self._probe_cold_wake_bridge_status)
                self.ui.set_wake_bridge_status(text, level, tooltip)
            except Exception as e:
                self.ui.set_wake_bridge_status("Wake Bridge: error", "bad", str(e))
            await asyncio.sleep(12)

    @staticmethod
    def _digits_only(value: str) -> str:
        return re.sub(r"\D+", "", value or "")

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

    @classmethod
    def _phrase_in_text(cls, phrase: str, text: str) -> bool:
        phrase_n = cls._normalize_match_text(phrase)
        text_n = cls._normalize_match_text(text)
        if not phrase_n or not text_n:
            return False
        return f" {phrase_n} " in f" {text_n} "

    @staticmethod
    def _phone_variants(value: str) -> set[str]:
        d = re.sub(r"\D+", "", value or "")
        if not d:
            return set()
        variants = {d}
        if d.startswith("0") and len(d) > 1:
            local = d[1:]
            variants.add(local)
            variants.add(f"353{local}")
        if d.startswith("353") and len(d) > 3:
            local = d[3:]
            variants.add(local)
            variants.add(f"0{local}")
        return {v for v in variants if v}

    @staticmethod
    def _load_imessage_wake_config() -> dict:
        cfg = {
            "enabled": True,
            "sender": "",
            "phrase": "jarvis wake",
            "secret": "",
            "autostart": True,
            "interval_seconds": 15,
            "cooldown_seconds": 120,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["enabled"] = bool(raw.get("imessage_wake_enabled", cfg["enabled"]))
            cfg["sender"] = str(raw.get("imessage_wake_sender", cfg["sender"]) or "").strip()
            cfg["phrase"] = str(raw.get("imessage_wake_phrase", cfg["phrase"]) or "").strip().lower()
            cfg["secret"] = str(raw.get("imessage_wake_secret", cfg["secret"]) or "").strip()
            cfg["autostart"] = bool(raw.get("imessage_monitor_autostart", cfg["autostart"]))
            cfg["interval_seconds"] = max(5, min(int(raw.get("imessage_monitor_interval_seconds", cfg["interval_seconds"]) or 15), 300))
            cfg["cooldown_seconds"] = max(15, min(int(raw.get("imessage_wake_cooldown_seconds", cfg["cooldown_seconds"]) or 120), 3600))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_imessage_shutdown_config() -> dict:
        cfg = {
            "enabled": True,
            "sender": "",
            "phrase": "jarvis shutdown",
            "secret": "",
            "cooldown_seconds": 120,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            wake_sender = str(raw.get("imessage_wake_sender", cfg["sender"]) or "").strip()
            cfg["enabled"] = bool(raw.get("imessage_shutdown_enabled", cfg["enabled"]))
            cfg["sender"] = str(raw.get("imessage_shutdown_sender", wake_sender) or "").strip()
            cfg["phrase"] = str(raw.get("imessage_shutdown_phrase", cfg["phrase"]) or "").strip().lower()
            cfg["secret"] = str(raw.get("imessage_shutdown_secret", cfg["secret"]) or "").strip()
            cfg["cooldown_seconds"] = max(15, min(int(raw.get("imessage_shutdown_cooldown_seconds", cfg["cooldown_seconds"]) or 120), 3600))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_mail_monitor_config() -> dict:
        cfg = {
            "autostart": True,
            "interval_seconds": 30,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["autostart"] = bool(raw.get("mail_monitor_autostart", cfg["autostart"]))
            cfg["interval_seconds"] = max(10, min(int(raw.get("mail_monitor_interval_seconds", cfg["interval_seconds"]) or 30), 900))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_predictive_config() -> dict:
        cfg = {
            "enabled": True,
            "interval_seconds": 75,
            "silence_seconds": 25,
            "voice_announce": False,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["enabled"] = bool(raw.get("predictive_automation_enabled", cfg["enabled"]))
            cfg["interval_seconds"] = max(20, min(int(raw.get("predictive_interval_seconds", cfg["interval_seconds"]) or 75), 3600))
            cfg["silence_seconds"] = max(8, min(int(raw.get("predictive_silence_seconds", cfg["silence_seconds"]) or 25), 600))
            cfg["voice_announce"] = bool(raw.get("predictive_voice_announce", cfg["voice_announce"]))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_diagnostics_stream_config() -> dict:
        cfg = {
            "enabled": True,
            "interval_seconds": 2.5,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["enabled"] = bool(raw.get("dashboard_diagnostics_enabled", cfg["enabled"]))
            cfg["interval_seconds"] = max(0.8, min(float(raw.get("dashboard_diagnostics_interval_seconds", cfg["interval_seconds"]) or 2.5), 30.0))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_visual_watch_config() -> dict:
        cfg = {
            "enabled": True,
            "interval_seconds": 3.5,
            "announce_changes": True,
        }
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["enabled"] = bool(raw.get("visual_watch_enabled", cfg["enabled"]))
            cfg["interval_seconds"] = max(1.0, min(float(raw.get("visual_watch_interval_seconds", cfg["interval_seconds"]) or 3.5), 30.0))
            cfg["announce_changes"] = bool(raw.get("visual_watch_announce_changes", cfg["announce_changes"]))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _load_audio_tuning_config() -> tuple[str, dict]:
        profile_name = "balanced"
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            requested = str(raw.get("audio_latency_profile", "balanced") or "balanced").strip().lower()
            if requested in AUDIO_TUNING_PROFILES:
                profile_name = requested
        except Exception:
            pass
        return profile_name, dict(AUDIO_TUNING_PROFILES[profile_name])

    def _audio_diag_inc(self, key: str, amount: int = 1) -> None:
        self._audio_diag[key] = int(self._audio_diag.get(key, 0)) + int(amount)

    @staticmethod
    def _queue_fill(q: asyncio.Queue | None) -> tuple[int, int]:
        if q is None:
            return 0, 0
        maxsize = int(getattr(q, "maxsize", 0) or 0)
        return int(q.qsize()), maxsize

    def _build_audio_diag_status(self, drop_delta: int) -> tuple[str, str, str]:
        mic_q, mic_max = self._queue_fill(self.out_queue)
        spk_q, spk_max = self._queue_fill(self.audio_in_queue)
        if not self.session:
            text = f"Audio {self._audio_profile_name}: idle"
            return text, "off", "Audio pipeline is idle (no active live session)."

        pressure = 0.0
        if mic_max > 0:
            pressure = max(pressure, mic_q / mic_max)
        if spk_max > 0:
            pressure = max(pressure, spk_q / spk_max)

        level = "ok"
        if drop_delta >= 8 or pressure >= 0.95:
            level = "bad"
        elif drop_delta > 0 or pressure >= 0.75:
            level = "warn"

        total_dropped = int(self._audio_diag.get("mic_dropped", 0)) + int(self._audio_diag.get("speaker_dropped", 0)) + int(self._audio_diag.get("phone_dropped", 0))
        text = (
            f"Audio {self._audio_profile_name} "
            f"M:{mic_q}/{mic_max or '-'} S:{spk_q}/{spk_max or '-'} "
            f"D+{drop_delta} T{total_dropped}"
        )
        tooltip = (
            f"profile={self._audio_profile_name}\n"
            f"mic_queue={mic_q}/{mic_max or 0}, speaker_queue={spk_q}/{spk_max or 0}\n"
            f"mic_enqueued={self._audio_diag.get('mic_enqueued', 0)}, mic_dropped={self._audio_diag.get('mic_dropped', 0)}\n"
            f"speaker_enqueued={self._audio_diag.get('speaker_enqueued', 0)}, speaker_dropped={self._audio_diag.get('speaker_dropped', 0)}\n"
            f"phone_enqueued={self._audio_diag.get('phone_enqueued', 0)}, phone_dropped={self._audio_diag.get('phone_dropped', 0)}\n"
            f"rx_bytes={self._audio_diag.get('speaker_received_bytes', 0)}, played_bytes={self._audio_diag.get('speaker_played_bytes', 0)}\n"
            f"interrupt_drained_chunks={self._audio_diag.get('interrupt_drained_chunks', 0)}"
        )
        return text, level, tooltip

    async def _run_audio_diagnostics(self) -> None:
        prev_total_drops = 0
        interval = max(0.5, float(self._audio_cfg.get("diag_interval_seconds", 1.0) or 1.0))
        while True:
            try:
                total_drops = (
                    int(self._audio_diag.get("mic_dropped", 0))
                    + int(self._audio_diag.get("speaker_dropped", 0))
                    + int(self._audio_diag.get("phone_dropped", 0))
                )
                drop_delta = max(0, total_drops - prev_total_drops)
                prev_total_drops = total_drops
                text, level, tooltip = self._build_audio_diag_status(drop_delta)
                self.ui.set_audio_status(text, level, tooltip)
            except Exception as e:
                self.ui.set_audio_status("Audio diagnostics: error", "bad", str(e))
            await asyncio.sleep(interval)

    def _is_authorized_imessage_sender(self, expected_sender: str, sender: str, chat_name: str) -> bool:
        expected_variants = self._phone_variants(expected_sender)
        expected_norm = self._normalize_match_text(expected_sender)
        if not expected_variants and not expected_norm:
            return False
        incoming_candidates = [sender, chat_name]

        # Primary matching for phone-number based sender IDs.
        for candidate in incoming_candidates:
            candidate_variants = self._phone_variants(candidate)
            if not candidate_variants:
                continue
            for cv in candidate_variants:
                for ev in expected_variants:
                    if cv == ev or cv.endswith(ev) or ev.endswith(cv):
                        return True

        # Fallback matching for email/contact-name sender IDs.
        for candidate in incoming_candidates:
            candidate_norm = self._normalize_match_text(candidate)
            if not candidate_norm or not expected_norm:
                continue
            if (
                candidate_norm == expected_norm
                or candidate_norm.endswith(expected_norm)
                or expected_norm.endswith(candidate_norm)
            ):
                return True
        return False

    def _is_authorized_wake_sender(self, sender: str, chat_name: str) -> bool:
        expected_sender = str(self._wake_protocol_cfg.get("sender", "") or "").strip()
        if not expected_sender:
            return bool((sender or chat_name or "").strip())
        return self._is_authorized_imessage_sender(expected_sender, sender, chat_name)

    def _is_authorized_shutdown_sender(self, sender: str, chat_name: str) -> bool:
        expected_sender = str(self._shutdown_protocol_cfg.get("sender", "") or "").strip()
        if not expected_sender:
            return True
        return self._is_authorized_imessage_sender(expected_sender, sender, chat_name)

    def _schedule_shutdown(self, reason: str) -> bool:
        vps_mode = bool((os.getenv("JARVIS_VPS_URL") or "").strip())
        headless_mode = bool((os.getenv("JARVIS_HEADLESS") or "").strip())
        if vps_mode and headless_mode:
            self.ui.write_log("SYS: VPS/headless mode blocks shutdown requests to keep the public brain alive.")
            return False

        if self._shutdown_in_progress:
            self.ui.write_log("SYS: Shutdown in progress.")
            return False

        if self._reboot_in_progress:
            self.ui.write_log("SYS: Reboot in progress.")
            return False

        self._shutdown_in_progress = True
        self.ui.write_log("SYS: Shutdown requested.")

        async def _wait_for_speech_drain(timeout_seconds: float = 1.5) -> bool:
            deadline = time.monotonic() + max(0.5, timeout_seconds)
            while time.monotonic() < deadline:
                with self._speaking_lock:
                    speaking = self._is_speaking
                queue_empty = (self.audio_in_queue is None) or self.audio_in_queue.empty()
                turn_done = (self._turn_done_event is None) or self._turn_done_event.is_set()
                if (not speaking) and queue_empty and turn_done:
                    return True
                await asyncio.sleep(0.05)
            return False

        async def _do_shutdown():
            self.ui.write_log("SYS: Shutting down.")
            try:
                await self._save_session_summary()
            except Exception as save_error:
                self.ui.write_log(f"SYS: Shutdown summary skipped: {save_error}")

            if self.session:
                try:
                    if self._turn_done_event:
                        self._turn_done_event.clear()
                    await self.session.send_client_content(
                        turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                        turn_complete=True,
                    )
                except Exception as send_error:
                    self.ui.write_log(f"SYS: Shutdown goodbye skipped: {send_error}")

            drained = await _wait_for_speech_drain(timeout_seconds=1.5)
            if not drained:
                self.ui.write_log("SYS: Shutdown continuing.")
            await asyncio.sleep(0.2)
            self.ui.write_log("SYS: Exiting.")
            import os as _os
            _os._exit(0)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            loop.create_task(_do_shutdown())
        else:
            def _run_shutdown_loop():
                try:
                    asyncio.run(_do_shutdown())
                except Exception as exc:
                    self.ui.write_log(f"SYS: Shutdown startup failed: {exc}")

            threading.Thread(target=_run_shutdown_loop, daemon=True).start()
        return True

    def _schedule_reboot(self, reason: str) -> bool:
        if self._reboot_in_progress:
            self.ui.write_log("SYS: Reboot in progress.")
            return False
        if self._shutdown_in_progress:
            self.ui.write_log(f"SYS: Shutdown in progress; reboot ignored ({reason}).")
            return False

        self._reboot_in_progress = True
        self.ui.write_log("SYS: Reboot requested.")

        async def _wait_for_speech_drain(timeout_seconds: float = 14.0) -> bool:
            deadline = time.monotonic() + max(1.0, timeout_seconds)
            while time.monotonic() < deadline:
                with self._speaking_lock:
                    speaking = self._is_speaking
                queue_empty = (self.audio_in_queue is None) or self.audio_in_queue.empty()
                turn_done = (self._turn_done_event is None) or self._turn_done_event.is_set()
                if (not speaking) and queue_empty and turn_done:
                    return True
                await asyncio.sleep(0.05)
            return False

        async def _do_reboot():
            await self._save_session_summary()
            if self.session:
                try:
                    if self._turn_done_event:
                        self._turn_done_event.clear()
                    await self.session.send_client_content(
                        turns={"parts": [{"text": "Tell the user you are rebooting to apply updates, in one short sentence."}]},
                        turn_complete=True,
                    )
                except Exception:
                    pass

            drained = await _wait_for_speech_drain(timeout_seconds=14.0)
            if not drained:
                self.ui.write_log("SYS: Restarting.")

            restart_cmd = [sys.executable] + list(sys.argv)
            self.ui.write_log(f"SYS: Relaunch command: {' '.join(restart_cmd)}")

            try:
                _subprocess.Popen(
                    restart_cmd,
                    cwd=str(BASE_DIR),
                    env=os.environ.copy(),
                    close_fds=True,
                )
                self.ui.write_log("SYS: Relaunching.")
                os._exit(0)
            except Exception as spawn_error:
                self.ui.write_log(f"SYS: Relaunch spawn failed: {spawn_error}")

            import os as _os
            try:
                _os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                self.ui.write_log(f"SYS: Reboot failed: {e}")
                self._reboot_in_progress = False
                return

        asyncio.create_task(_do_reboot())
        return True

    async def _maybe_handle_imessage_wake_protocol(self, alert: dict) -> bool:
        if not self._wake_protocol_cfg.get("enabled", True):
            return False

        text = str(alert.get("text") or "").strip()
        if not text:
            self.ui.write_log("SYS: Wake protocol ignored (empty message text).")
            return False

        sender = str(alert.get("sender") or "").strip()
        chat_name = str(alert.get("chat_name") or "").strip()
        self.ui.write_log("SYS: Wake protocol received.")
        if not self._is_authorized_wake_sender(sender=sender, chat_name=chat_name):
            self.ui.write_log("SYS: Wake protocol rejected (sender not authorized).")
            return False

        phrase = str(self._wake_protocol_cfg.get("phrase", "jarvis wake") or "jarvis wake")
        if not self._phrase_in_text(phrase, text):
            self.ui.write_log(f"SYS: Wake protocol rejected (phrase '{phrase}' not found).")
            return False

        rowid = int(alert.get("rowid") or 0)
        cooldown = int(self._wake_protocol_cfg.get("cooldown_seconds", 120) or 120)
        now = time.time()
        if rowid and rowid <= self._last_wake_protocol_rowid:
            self.ui.write_log(f"SYS: Wake protocol ignored (duplicate rowid {rowid}).")
            return True
        if (now - self._last_wake_protocol_ts) < cooldown:
            self.ui.write_log(f"SYS: iMessage wake suppressed (cooldown {cooldown}s).")
            if rowid:
                self._last_wake_protocol_rowid = rowid
            return True

        secret = str(self._wake_protocol_cfg.get("secret", "") or "").strip()
        text_l = text.lower()
        if secret and secret.lower() not in text_l:
            self.ui.write_log("SYS: iMessage wake command rejected (secret mismatch).")
            return True

        self.ui.write_log("SYS: Wake accepted.")

        self._last_wake_protocol_ts = now
        if rowid:
            self._last_wake_protocol_rowid = rowid

        self.ui.write_log("SYS: Wake acknowledged.")
        if self.session:
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": "[WAKE_PROTOCOL] Trusted iMessage wake command received. Confirm that you are awake in one short sentence."}]},
                    turn_complete=True,
                )
            except Exception as e:
                self.ui.write_log(f"ERR: Wake protocol dispatch failed — {e}")

        ack_target = sender or chat_name
        if ack_target:
            local_started = False
            if self.session is None:
                local_started = self._launch_local_jarvis_if_needed()
            status = "online" if self.session or local_started else "remote only"
            ack = self._build_wake_remote_access_message()
            ack = ack + f"\nLocal status: {status}."
            result = await asyncio.to_thread(send_imessage, ack_target, ack)
            self.ui.write_log(f"SYS: iMessage wake acknowledgement: {result}")
        return True

    async def _maybe_handle_imessage_shutdown_protocol(self, alert: dict) -> bool:
        if not self._shutdown_protocol_cfg.get("enabled", True):
            return False

        text = str(alert.get("text") or "").strip()
        if not text:
            return False

        sender = str(alert.get("sender") or "").strip()
        chat_name = str(alert.get("chat_name") or "").strip()
        self.ui.write_log("SYS: Shutdown protocol received.")
        if not self._is_authorized_shutdown_sender(sender=sender, chat_name=chat_name):
            self.ui.write_log("SYS: Shutdown protocol rejected (sender not authorized).")
            return False

        phrase = str(self._shutdown_protocol_cfg.get("phrase", "jarvis shutdown") or "jarvis shutdown")
        if not self._phrase_in_text(phrase, text):
            self.ui.write_log(f"SYS: Shutdown protocol rejected (phrase '{phrase}' not found).")
            return False

        rowid = int(alert.get("rowid") or 0)
        cooldown = int(self._shutdown_protocol_cfg.get("cooldown_seconds", 120) or 120)
        now = time.time()
        if rowid and rowid <= self._last_shutdown_protocol_rowid:
            self.ui.write_log(f"SYS: Shutdown protocol ignored (duplicate rowid {rowid}).")
            return True
        if (now - self._last_shutdown_protocol_ts) < cooldown:
            self.ui.write_log(f"SYS: iMessage shutdown suppressed (cooldown {cooldown}s).")
            if rowid:
                self._last_shutdown_protocol_rowid = rowid
            return True

        secret = str(self._shutdown_protocol_cfg.get("secret", "") or "").strip()
        text_l = text.lower()
        if secret and secret.lower() not in text_l:
            self.ui.write_log("SYS: iMessage shutdown command rejected (secret mismatch).")
            return True

        self._last_shutdown_protocol_ts = now
        if rowid:
            self._last_shutdown_protocol_rowid = rowid

        self.ui.write_log("SYS: Shutdown accepted.")

        ack_target = sender or chat_name
        if ack_target:
            ack = "JARVIS shutdown protocol accepted. Powering down now."
            result = await asyncio.to_thread(send_imessage, ack_target, ack)
            self.ui.write_log(f"SYS: iMessage shutdown acknowledgement: {result}")

        self._schedule_shutdown("iMessage shutdown protocol")
        return True

    def _on_public_url_changed(self, url: str | None):
        if not url:
            return
        if self._last_public_url == url:
            return
        self._last_public_url = url
        self.ui.set_remote_url_status(url)
        self.ui.write_log("SYS: Remote URL updated.")
        sec = ""
        if self._dashboard and hasattr(self._dashboard, "get_remote_security_status"):
            try:
                sec = self._dashboard.get_remote_security_status()
            except Exception:
                sec = "SECURITY: STATUS UNAVAILABLE"
        self.ui.show_content("Remote URL", f"{url}\n\n{sec}")
        self._send_boot_remote_access_imessage(url)

    def _maybe_handle_remote_url_request(self, text: str) -> bool:
        if not text:
            return False
        cleaned = (text or "").strip().lower().replace("?", "").replace(".", "")
        if not any(term in cleaned for term in ("remote url", "public url", "public tunnel", "tunnel url", "remote address")):
            return False
        if not any(term in cleaned for term in ("show", "what", "tell", "display", "give", "get", "open")):
            return False
        if self._dashboard is None:
            self.ui.write_log("SYS: Dashboard unavailable for remote URL lookup.")
            return True
        url = self._dashboard.get_remote_url() if hasattr(self._dashboard, "get_remote_url") else self._dashboard.get_url()
        sec = self._dashboard.get_remote_security_status() if hasattr(self._dashboard, "get_remote_security_status") else "SECURITY: STATUS UNAVAILABLE"
        self.ui.write_log(f"SYS: Public tunnel URL: {url}")
        self.ui.show_content("Remote URL", f"{url}\n\n{sec}")
        self.speak(f"The public tunnel URL is {url}")
        return True

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None

        key = self._dashboard.new_key()
        if hasattr(self._dashboard, "get_remote_url"):
            url = self._dashboard.get_remote_url()
        else:
            url = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        if hasattr(self._dashboard, "get_auto_login_url"):
            auto = self._dashboard.get_auto_login_url(key)
        else:
            auto = f"{url}/auto-login?key={key}"
        if hasattr(self._dashboard, "get_remote_security_status"):
            sec = self._dashboard.get_remote_security_status()
        else:
            sec = "SECURITY: STATUS UNAVAILABLE"
        return url, key, auto, manual, sec

    def _build_wake_remote_access_message(self) -> str:
        """Return the live remote access details to include in a wake acknowledgement."""
        if self._dashboard is None:
            url = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "remote dashboard unavailable"
            key = ""
            auto = ""
            sec = "SECURITY: STATUS UNAVAILABLE"
        else:
            if hasattr(self._dashboard, "new_key"):
                key = self._dashboard.new_key()
            else:
                key = ""

            if hasattr(self._dashboard, "get_remote_url"):
                url = self._dashboard.get_remote_url()
            elif hasattr(self._dashboard, "get_url"):
                url = self._dashboard.get_url()
            else:
                url = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "remote dashboard unavailable"

            auto = ""
            if key and hasattr(self._dashboard, "get_auto_login_url"):
                auto = self._dashboard.get_auto_login_url(key)
            elif key:
                auto = f"{url}/auto-login?key={key}"

            sec = self._dashboard.get_remote_security_status() if hasattr(self._dashboard, "get_remote_security_status") else "SECURITY: STATUS UNAVAILABLE"

        lines = [
            "JARVIS wake accepted",
        ]
        if url:
            lines.append(f"Open: {url}")
        if key:
            lines.append(f"Key: {key}")
        if auto:
            lines.append(f"Auto: {auto}")
        if self.session is not None:
            lines.append("Status: local online")
        else:
            lines.append("Status: VPS remote voice active")
        lines.append(sec)
        return "\n".join(lines)

    def _launch_local_jarvis_if_needed(self) -> bool:
        """Launch the Mac-side app only when the VPS is not the active remote brain."""
        if self.session is not None:
            return True
        vps_url = (os.getenv("JARVIS_VPS_URL") or "").strip()
        if vps_url:
            try:
                url = f"{vps_url.rstrip('/')}/api/health"
                with urllib.request.urlopen(url, timeout=4) as resp:
                    payload = resp.read(2048)
                try:
                    data = json.loads(payload.decode("utf-8", errors="replace"))
                except Exception:
                    data = {}
                if isinstance(data, dict) and data.get("ok") is not False:
                    if hasattr(self.ui, "set_vps_status"):
                        self.ui.set_vps_status(
                            "VPS: connected • remote-only wake mode",
                            "ok",
                            "Remote VPS is active; local wake launch is intentionally skipped.",
                        )
                    self.ui.write_log("SYS: VPS brain active; skipping local wake launch to keep sleep-mode wake remote-first.")
                    return False
            except Exception:
                pass
        if _platform.system() != "Darwin":
            return False
        try:
            _subprocess.Popen(
                [sys.executable, str(BASE_DIR / "main.py")],
                cwd=str(BASE_DIR),
                stdin=_subprocess.DEVNULL,
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            self.ui.write_log("SYS: Local JARVIS launch triggered for wake command.")
            return True
        except Exception as exc:
            self.ui.write_log(f"SYS: Local JARVIS launch failed: {exc}")
            return False

    def _is_disconnect_error(self, err: Exception | BaseException) -> bool:
        return _is_disconnect_error(err)

    def _is_billing_error(self, err: Exception | BaseException) -> bool:
        return _is_billing_error(err)

    def _get_live_remote_url(self):
        """Return currently active remote URL and security summary without issuing a new key."""
        if self._dashboard is None:
            self.ui.write_log("SYS: Dashboard unavailable.")
            return None

        if hasattr(self._dashboard, "get_remote_url"):
            url = self._dashboard.get_remote_url()
        else:
            url = self._dashboard.get_url()

        if hasattr(self._dashboard, "get_remote_security_status"):
            sec = self._dashboard.get_remote_security_status()
        else:
            sec = "SECURITY: STATUS UNAVAILABLE"

        return url, sec

    def _on_text_command(self, text: str):
        if getattr(self.ui, "is_biometric_lock_active", lambda: False)():
            self.ui.write_log("SYS: Biometric lock active — input blocked until verification completes.")
            return
        self._predictive_daemon.record_text_command(text)
        if self._maybe_handle_remote_url_request(text):
            return
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        if getattr(self.ui, "is_biometric_lock_active", lambda: False)():
            return
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        if self._tts_player:
            try:
                self._tts_player.stop()
            except Exception:
                pass
        if self._tts_sentence_queue:
            while not self._tts_sentence_queue.empty():
                try:
                    self._tts_sentence_queue.get_nowait()
                except Exception:
                    break
        self._tts_pending_sentence = ""
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                self._audio_diag_inc("interrupt_drained_chunks", drained)
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if getattr(self.ui, "is_biometric_lock_active", lambda: False)():
            return
        vps_url = (os.getenv("JARVIS_VPS_URL") or "").strip()
        if vps_url:
            try:
                url = f"{vps_url.rstrip('/')}/api/health"
                with urllib.request.urlopen(url, timeout=4) as resp:
                    payload = resp.read(2048)
                try:
                    data = json.loads(payload.decode("utf-8", errors="replace"))
                except Exception:
                    data = {}
                if isinstance(data, dict) and data.get("ok") is not False:
                    if hasattr(self.ui, "set_vps_status"):
                        self.ui.set_vps_status(
                            "VPS: connected • remote voice active • local speech suppressed",
                            "ok",
                            "Remote VPS is authoritative; local speech is intentionally blocked.",
                        )
                    self.ui.write_log("SYS: VPS healthy; local speech suppressed to keep remote voice authoritative.")
                    return
            except Exception:
                pass
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _handle_biometric_failure(self) -> None:
        self.ui.write_log("SYS: Biometric verification failed — initiating fail-closed shutdown.")
        self._schedule_shutdown("biometric verification failed")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
        except Exception:
            self._asst_name = "JARVIS"

        memory = load_memory_with_vps_sync(load_memory(), os.getenv('JARVIS_VPS_URL'))
        obsidian_mem = recall_user_profile()
        personal_context = build_personal_memory_context(memory, obsidian_mem)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (
            "ADDRESS: When speaking Turkish, always say \"efendim\". "
            "When speaking English, always say \"sir\". "
            "Never address the user by personal name unless explicitly asked to do so. "
            "Never call the user \"James\"."
        )
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if personal_context:
            parts.append(personal_context)
        parts.append(sys_prompt)

        cfg_kwargs: dict = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
        )
        # speech_config is only valid when Gemini itself produces audio
        if not self._use_external_tts:
            cfg_kwargs["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            )
        return types.LiveConnectConfig(**cfg_kwargs)

    def _block_live_model(self, model: str, seconds: int = 300) -> None:
        if not model:
            return
        self._live_model_backoff_until[model] = time.monotonic() + max(30, int(seconds))

    def _available_live_models(self) -> list[str]:
        now = time.monotonic()
        blocked = {
            model
            for model, until in self._live_model_backoff_until.items()
            if until > now
        }
        # Prune expired entries.
        self._live_model_backoff_until = {
            model: until
            for model, until in self._live_model_backoff_until.items()
            if until > now
        }

        models = [m for m in LIVE_MODELS if m not in blocked]
        if models:
            return models

        # Safety valve: if everything is blocked, reset and try all again.
        self._live_model_backoff_until.clear()
        return list(LIVE_MODELS)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        self._predictive_daemon.record_tool_call(name, args)

        if self._shutdown_in_progress or self._reboot_in_progress:
            return types.FunctionResponse(
                id=fc.id,
                name=name,
                response={"result": "Shutdown in progress; skipping tool execution."},
            )

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        if name == "obsidian_memory":
            action = str(args.get("action", "") or "").strip().lower()
            if action == "save":
                category = str(args.get("category", "Notes") or "Notes").strip()
                fact = str(args.get("fact", "") or "").strip()
                result = remember_user_fact(category, fact) if fact else "No fact provided."
            else:
                result = recall_user_profile()
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": result, "silent": True}
            )

        if name == "personal_memory":
            query = str(args.get("query", "") or "").strip()
            result = recall_personal_memory(query) if query else "No query provided."
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": result, "silent": True}
            )

        if name == "document_memory":
            action = str(args.get("action", "") or "").strip().lower()
            path = str(args.get("path", "") or "").strip()
            query = str(args.get("query", "") or "").strip()
            source_name = str(args.get("source_name", "") or "").strip()
            limit = int(args.get("limit", 3) or 3)

            if action == "ingest_file":
                if not path:
                    result = "No file path provided."
                else:
                    result = ingest_document(path, source_name=source_name or None)
            elif action == "ingest_codebase":
                if not path:
                    result = "No directory path provided."
                else:
                    result = index_codebase(path, source_name=source_name or None)
            elif action == "search":
                if not query:
                    result = "No query provided."
                else:
                    result = search_document_index(query, limit=limit)
            elif action == "recall":
                if not query:
                    result = "No query provided."
                else:
                    result = recall_document_details(query, limit=limit)
            else:
                result = "Use action: ingest_file | ingest_codebase | search | recall."

            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": result, "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if self._shutdown_in_progress or self._reboot_in_progress:
                result = "Shutdown in progress; skipping tool execution."

            elif name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                opt = self._tool_optimizer.optimize_flow("weather_report", args)
                if opt["status"] == "cached":
                    result = opt["result"]
                else:
                    r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                    result = r or "Weather delivered."
                    self._tool_optimizer.context_manager.store(f"tool_cache:weather_report:{hash(frozenset(args.items()))}", result)

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "imessage_control":
                r = await loop.run_in_executor(None, lambda: imessage_control(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or "Done."

            elif name == "mail_control":
                r = await loop.run_in_executor(None, lambda: mail_control(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or "Done."

            elif name == "find_my":
                action = str(args.get("action", "") or "").strip().lower()
                if action == "open_and_read":
                    preferred = str(args.get("preferred", "app") or "app").strip().lower()
                    open_action = "web_open" if preferred == "web" else "open"

                    # Open Find My (or iCloud Find) first.
                    open_args = dict(args)
                    open_args["action"] = open_action
                    r = await loop.run_in_executor(None, lambda: find_my(parameters=open_args, player=self.ui))

                    import time as _t_mod
                    _now = _t_mod.monotonic()
                    _cooldown = 4.0
                    if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                        _wait = max(0, _cooldown - (_now - self._vision_last_time))
                        result = (
                            f"{r} Vision is still processing the previous request. "
                            f"Please retry in {_wait:.1f}s."
                        )
                    else:
                        self._vision_busy = True
                        self._vision_last_time = _now
                        # Give app/web a moment to render locations before capture.
                        await asyncio.sleep(1.5)
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        target = str(args.get("target", "") or "").strip()
                        scope = str(args.get("scope", "all") or "all").strip().lower()
                        if target:
                            prompt = (
                                f"From this Find My screen, locate '{target}'. "
                                "Report the most recent location, last-updated time, and whether it appears exact or approximate. "
                                "If not visible, say not visible and what section/tab should be opened."
                            )
                        elif scope == "people":
                            prompt = (
                                "From this Find My screen, list visible PEOPLE locations with latest timestamp and confidence. "
                                "If people are not visible, say which tab/section to open."
                            )
                        elif scope == "devices":
                            prompt = (
                                "From this Find My screen, list visible DEVICE locations with battery and latest timestamp when shown. "
                                "If devices are not visible, say which tab/section to open."
                            )
                        else:
                            prompt = (
                                "From this Find My screen, summarize visible people and device locations, including timestamps when visible."
                            )
                        self._pending_vision = (img_b, mime_t, prompt, "screen")
                        result = (
                            f"{r} [VISION_ACTIVE] Find My screen captured. "
                            "I will now read the visible locations."
                        )
                else:
                    r = await loop.run_in_executor(None, lambda: find_my(parameters=args, player=self.ui))
                    result = r or "Done."

            elif name == "ifttt_webhooks":
                r = await loop.run_in_executor(None, lambda: ifttt_webhooks(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "alexa_routines":
                r = await loop.run_in_executor(None, lambda: alexa_routines(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "google_calendar":
                r = await loop.run_in_executor(None, lambda: google_calendar(parameters=args, response=None, player=self.ui))
                result = r or "Google Calendar action completed."

            elif name == "visual_watch":
                action = str(args.get("action", "") or "").strip().lower()
                if action == "add":
                    target = self._visual_monitor.add_target(args)
                    result = f"Watching {target.label} ({target.target_type}) as {target.target_id}."
                elif action == "list":
                    items = self._visual_monitor.list_targets()
                    if not items:
                        result = "No active visual targets."
                    else:
                        lines = ["Active visual targets:"]
                        for item in items:
                            lines.append(
                                f"- {item['target_id']}: {item['label']} [{item['target_type']}] every {item['interval_seconds']}s"
                            )
                        result = "\n".join(lines)
                elif action == "remove":
                    target_id = str(args.get("target_id", "") or "").strip()
                    if not target_id:
                        result = "Specify target_id to remove a visual watch."
                    else:
                        ok = self._visual_monitor.remove_target(target_id)
                        result = f"Removed visual watch {target_id}." if ok else f"No visual watch found for {target_id}."
                elif action == "clear":
                    self._visual_monitor.clear()
                    result = "Cleared all visual watches."
                elif action == "status":
                    items = self._visual_monitor.list_targets()
                    result = f"Visual watch status: {len(items)} target(s) active."
                else:
                    result = "Unknown visual_watch action. Use add, list, remove, clear, or status."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    target_type = str(args.get("target_type", args.get("angle", "screen")) or "screen").lower().strip()
                    user_text = args.get("text", "What do you see?")
                    browser_name = str(args.get("browser", "") or "").strip()
                    target = str(args.get("target", "") or "").strip()
                    window_title = str(args.get("window_title", "") or "").strip()
                    app_name = str(args.get("app_name", "") or "").strip()
                    index = args.get("index")
                    if index in ("", None):
                        index = None
                    else:
                        try:
                            index = int(index)
                        except Exception:
                            index = None
                    img_b, mime_t, target_label = await loop.run_in_executor(
                        None,
                        lambda: _capture_targeted_visual(
                            target_type,
                            browser=browser_name,
                            target=target,
                            index=index,
                            window_title=window_title,
                            app_name=app_name,
                        ),
                    )
                    if target_type in {"camera", "webcam"}:
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                    print(f"[Vision] 📸 {target_label}: {len(img_b):,} bytes")
                    _stall = target_label
                    if target_type in {"tab", "browser", "browser_tab", "browser-tab"}:
                        user_text = (
                            f"{user_text}\n\nFocus specifically on the browser tab content, visible controls, and any text or status relevant to the tab." 
                        )
                    elif target_type in {"window", "app", "application"}:
                        user_text = (
                            f"{user_text}\n\nFocus specifically on the visible app or window contents, controls, and any important on-screen state."
                        )
                    self._pending_vision = (img_b, mime_t, user_text, target_label)
                    result = (
                        f"[VISION_ACTIVE] {target_label} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {target_label} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "wiz_lights":
                r = await loop.run_in_executor(None, lambda: wiz_lights(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                if isinstance(r, str) and REBOOT_MARKER in r:
                    cleaned = r.replace(REBOOT_MARKER, "").strip()
                    result = cleaned or "Dev agent completed self-improvement updates."
                    rebooted = self._schedule_reboot("dev_agent self-improvement applied")
                    if rebooted:
                        result = f"{result}\n\nSYS: Rebooting to apply updated code."
                else:
                    result = r or "Done."

            elif name == "workspace_agent":
                r = await loop.run_in_executor(None, lambda: workspace_agent(parameters=args, player=self.ui))
                result = r or "Done."
                if isinstance(r, str) and REBOOT_MARKER in r:
                    cleaned = r.replace(REBOOT_MARKER, "").strip()
                    result = cleaned or "Workspace self-improvement updates applied."
                    rebooted = self._schedule_reboot("workspace_agent self-improvement applied")
                    if rebooted:
                        result = f"{result}\n\nSYS: Rebooting to apply updated code."
                if r:
                    self.ui.show_content("WORKSPACE AGENT", r)

            elif name == "web_search":
                opt = self._tool_optimizer.optimize_flow("web_search", args)
                if opt["status"] == "cached":
                    result = opt["result"]
                else:
                    r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                    result = r or "Done."
                    self._tool_optimizer.context_manager.store(f"tool_cache:web_search:{hash(frozenset(args.items()))}", result)
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            elif name == "manage_interest_profile":
                r = await loop.run_in_executor(None, lambda: manage_interest_profile(parameters=args, player=self.ui))
                result = r or "Done."
                if r:
                    self.ui.show_content("INTEREST PROFILE", r)

            elif name == "file_processor":
                if not args.get("file_path") and not args.get("file_paths"):
                    current_files = getattr(self.ui, "current_files", []) or []
                    if len(current_files) > 1:
                        args["file_paths"] = current_files
                    elif current_files:
                        args["file_path"] = current_files[-1]
                    elif self.ui.current_file:
                        args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."
                if r and not r.startswith("Done."):
                    title = args.get("action") or "FILE PROCESSOR"
                    if args.get("file_paths") and len(args.get("file_paths", [])) > 1:
                        title = f"{title.upper()} — {len(args.get('file_paths', []))} files"
                    elif args.get("file_path"):
                        title = f"{title.upper()} — {Path(args['file_path']).name}"
                    self.ui.show_content(str(title), r)

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self._schedule_shutdown("tool: shutdown_jarvis")
                result = "Shutting down JARVIS."

            elif name == "security_biometrics":
                action = str(args.get("action", "status")).strip().lower()
                target_identity = str(args.get("target_identity", "James Lumsden")).strip()

                if action == "enroll":
                    profile_name = str(args.get("name") or target_identity or "James Lumsden").strip()
                    profile_id = str(args.get("profile_id") or profile_name.lower().replace(" ", "_")).strip()
                    voice_text = str(args.get("voice_print") or target_identity or profile_name).strip()
                    visual_text = str(args.get("visual_signature") or target_identity or profile_name).strip()
                    make_primary = bool(args.get("make_primary", True))
                    clearance = str(args.get("clearance_level") or "omega").strip()
                    result = enroll_biometric_profile(
                        profile_id=profile_id,
                        name=profile_name,
                        voice_print=voice_text,
                        visual_signature=visual_text,
                        clearance_level=clearance,
                        make_primary=make_primary,
                    )
                elif action == "detect_person":
                    visual_text = str(args.get("visual_signature") or target_identity or "").strip()
                    ok = verify_biometric_security("", visual_text)
                    result = (
                        f"Visual Person Detection protocol executed: Person identified as authorized user ({target_identity}). Security clearance verified."
                        if ok else
                        f"Visual Person Detection protocol executed: No matching visual profile was found for {target_identity}."
                    )
                elif action == "verify_voice":
                    voice_text = str(args.get("voice_print") or target_identity or "").strip()
                    ok = verify_biometric_security(voice_text, "")
                    result = (
                        f"Voice Recognition protocol executed: Voiceprint matched for {target_identity}. Access granted."
                        if ok else
                        f"Voice Recognition protocol executed: No matching voice profile was found for {target_identity}."
                    )
                elif action == "calibrate":
                    result = "Biometric sensors calibrated successfully. Voice print and visual model updated."
                else:
                    profiles = get_authorized_profiles()
                    primary = profiles.get("primary") or {}
                    primary_name = str(primary.get("name") or target_identity or "James Lumsden").strip()
                    result = f"Security protocol status: Biometrics online. Primary profile: {primary_name}."

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    def _enqueue_outgoing_audio(self, data: bytes) -> None:
        """Keep mic audio near-real-time by discarding oldest frames when queue is full."""
        if getattr(self.ui, "is_biometric_lock_active", lambda: False)():
            return
        try:
            self.out_queue.put_nowait({"data": data, "mime_type": "audio/pcm"})
            self._audio_diag_inc("mic_enqueued")
        except asyncio.QueueFull:
            self._audio_diag_inc("mic_dropped")
            try:
                self.out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.out_queue.put_nowait({"data": data, "mime_type": "audio/pcm"})
                self._audio_diag_inc("mic_enqueued")
            except asyncio.QueueFull:
                self._audio_diag_inc("mic_dropped")
                pass

    async def _enqueue_incoming_audio(self, data: bytes) -> None:
        """Queue assistant audio with profile-aware overflow policy."""
        if getattr(self.ui, "is_biometric_lock_active", lambda: False)():
            return
        if not self.audio_in_queue:
            return
        policy = str(self._audio_cfg.get("speaker_drop_policy", "preserve") or "preserve").strip().lower()
        if policy != "drop_oldest":
            timeout = float(self._audio_cfg.get("speaker_put_timeout_seconds", 0.2) or 0.2)
            try:
                await asyncio.wait_for(self.audio_in_queue.put(data), timeout=max(0.02, timeout))
                self._audio_diag_inc("speaker_enqueued")
                return
            except asyncio.TimeoutError:
                # Preserve continuity by skipping newest chunk if output is backed up.
                self._audio_diag_inc("speaker_dropped")
                return

        try:
            self.audio_in_queue.put_nowait(data)
            self._audio_diag_inc("speaker_enqueued")
        except asyncio.QueueFull:
            self._audio_diag_inc("speaker_dropped")
            try:
                self.audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.audio_in_queue.put_nowait(data)
                self._audio_diag_inc("speaker_enqueued")
            except asyncio.QueueFull:
                self._audio_diag_inc("speaker_dropped")
                pass

    def _start_speech_listener(self) -> None:
        if self._speech_listener_running:
            return
        self._speech_listener_running = True
        self._speech_recognizer = sr.Recognizer()
        self._speech_recognizer.energy_threshold = 400
        self._speech_recognizer.dynamic_energy_threshold = True
        self._speech_recognizer.pause_threshold = 0.8
        self._speech_mic = sr.Microphone()
        self._speech_recognizer.listen_in_background(self._speech_mic, self._handle_speech_callback, phrase_time_limit=3)

    def _handle_speech_callback(self, recognizer: sr.Recognizer, audio: sr.AudioData) -> None:
        try:
            text = recognizer.recognize_google(audio, language="en-US")
        except sr.UnknownValueError:
            return
        except sr.RequestError:
            return
        except Exception:
            return

        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        if not normalized:
            return
        for wake_phrase in self._wake_phrases:
            if normalized == wake_phrase or normalized.startswith(wake_phrase + " ") or normalized.endswith(" " + wake_phrase) or wake_phrase in normalized:
                self._on_text_command(text)
                break

    def _start_speech_listener(self) -> None:
        if self._speech_listener_running:
            return
        try:
            self._speech_recognizer = sr.Recognizer()
            self._speech_recognizer.energy_threshold = 400
            self._speech_recognizer.dynamic_energy_threshold = True
            self._speech_recognizer.pause_threshold = 0.8
            self._speech_mic = sr.Microphone()
            self._speech_recognizer.listen_in_background(
                self._speech_mic,
                self._handle_speech_callback,
                phrase_time_limit=3,
            )
            self._speech_listener_running = True
            self.ui.write_log("SYS: Speech listener ready.")
        except Exception as exc:
            self.ui.write_log(f"SYS: Speech listener unavailable ({exc})")

    def _handle_speech_callback(self, recognizer: sr.Recognizer, audio: sr.AudioData) -> None:
        try:
            text = recognizer.recognize_google(audio, language="en-US")
        except (sr.UnknownValueError, sr.RequestError, Exception):
            return

        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        if not normalized:
            return
        for wake_phrase in self._wake_phrases:
            if normalized == wake_phrase or normalized.startswith(wake_phrase + " ") or normalized.endswith(" " + wake_phrase) or wake_phrase in normalized:
                self._on_text_command(text)
                break

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(self._enqueue_outgoing_audio, data)

        try:
            import os as _os
            _dn = open(_os.devnull, "w")
            _sfd = _os.dup(2)
            _os.dup2(_dn.fileno(), 2)
            try:
                _stream = sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=int(self._audio_cfg.get("mic_chunk_size", 512)),
                    latency=self._audio_cfg.get("input_latency", "low"),
                    callback=callback,
                )
            finally:
                _os.dup2(_sfd, 2)
                _os.close(_sfd)
                _dn.close()
            with _stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            self.ui.write_log(f"SYS: Microphone unavailable — continuing without live mic input ({e})")
            return

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []
        ext_audio_buf: list[bytes] = []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        elif self._use_external_tts:
                            # Buffer Gemini audio as a fallback if no text arrives for external TTS.
                            ext_audio_buf.append(response.data)
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # Chunking is profile-controlled to balance responsiveness and stability.
                            _audio_data = response.data
                            _SLICE = int(self._audio_cfg.get("incoming_slice_bytes", 2400))
                            self._audio_diag_inc("speaker_received_bytes", len(_audio_data))
                            for _i in range(0, len(_audio_data), _SLICE):
                                await self._enqueue_incoming_audio(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                if txt != (out_buf[-1] if out_buf else ""):
                                    out_buf.append(txt)
                                # Stream complete sentences to ElevenLabs immediately
                                if self._use_external_tts:
                                    self._tts_pending_sentence += " " + txt
                                    while True:
                                        m = re.search(r'[.!?][\s"\']', self._tts_pending_sentence)
                                        if not m:
                                            break
                                        sentence = self._tts_pending_sentence[:m.end()].strip()
                                        self._tts_pending_sentence = self._tts_pending_sentence[m.end():]
                                        if sentence:
                                            self._enqueue_tts_sentence(sentence)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                ext_audio_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                                if self._use_external_tts:
                                    # Flush any remaining partial sentence
                                    if self._tts_pending_sentence.strip():
                                        self._enqueue_tts_sentence(self._tts_pending_sentence)
                                        self._tts_pending_sentence = ""
                                ext_audio_buf = []
                            elif self._use_external_tts and ext_audio_buf:
                                self.ui.write_log("SYS: External TTS text missing; falling back to Gemini audio for this turn.")
                                asyncio.create_task(self._play_external_audio_fallback(list(ext_audio_buf)))
                                ext_audio_buf = []
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            if _is_disconnect_error(e):
                print(f"[JARVIS] ⚠️ Live session disconnected: {e}")
                self.session = None
                return
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")
        reset_audio_output()

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=int(self._audio_cfg.get("speaker_chunk_size", 960)),
            latency=self._audio_cfg.get("output_latency", "low"),
        )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Batch cap is profile-controlled to tune latency vs smoothness.
                batch = bytearray(chunk)
                batch_cap = int(self._audio_cfg.get("play_batch_cap_bytes", 4800))
                while len(batch) < batch_cap:
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                    self._audio_diag_inc("speaker_played_bytes", len(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        lang_clause = f" Respond in {lang}." if lang else ""
        name_clause = f" Address the user as {name}." if name else ""

        # Inject recent-session context without consuming it so memory persists.
        recent_sessions = await asyncio.to_thread(get_recent_sessions, 2)
        session_clause = ""
        if recent_sessions:
            recent_parts: list[str] = []
            for item in recent_sessions:
                try:
                    _delta = (datetime.now() - datetime.strptime(item["date"], "%Y-%m-%d")).days
                    _when = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
                except Exception:
                    _when = "recently"
                recent_parts.append(f"{_when}: {item.get('summary', '').strip()}")

            if recent_parts:
                joined = " | ".join(recent_parts)
                session_clause = (
                    f" Also briefly and naturally connect with this recent context: {joined}"
                )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = f" Respond in {lang}." if lang else ""

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: News sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ────────────────────────────────────────────────────────--

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory_with_vps_sync(load_memory(), os.getenv('JARVIS_VPS_URL'))
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            last_error = None
            for model_name in SESSION_SUMMARY_MODELS:
                try:
                    resp = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                    )
                    summary = (resp.text or "").strip()
                    if summary:
                        save_session_summary(summary, lang)
                        return
                except Exception as e:
                    last_error = e
                    msg = str(e).lower()
                    if "404" in msg or "not_found" in msg or "no longer available" in msg:
                        continue
                    raise
            if last_error:
                raise last_error
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            try:
                alert = await asyncio.to_thread(self._sys_monitor.check)
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                raise
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ────────────────────────────────────────────────      

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except RuntimeError as e:
                        if "cannot schedule new futures after shutdown" in str(e).lower():
                            return
                        print(f"[Monitor] ⚠️ Background check error: {e}")
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    async def _run_imessage_monitor(self) -> None:
        """Poll new iMessages and alert via notifications + short spoken updates."""
        await asyncio.sleep(5)
        while True:
            try:
                alerts = await asyncio.to_thread(poll_imessage_alerts)
                if alerts:
                    protocol_triggered = False
                    for alert in alerts:
                        shutdown_handled = await self._maybe_handle_imessage_shutdown_protocol(alert)
                        wake_handled = await self._maybe_handle_imessage_wake_protocol(alert)
                        protocol_triggered = protocol_triggered or wake_handled or shutdown_handled

                    if self.session:
                        with self._speaking_lock:
                            speaking = self._is_speaking
                        recent_speech = (time.monotonic() - self._last_user_speech) < 8
                        if not speaking and not recent_speech and not protocol_triggered:
                            newest = alerts[-1]
                            sender = newest.get("chat_name") or newest.get("sender") or "Unknown"
                            snippet = (newest.get("text") or "")[:180]
                            prompt = (
                                "[IMESSAGE_ALERT] A new iMessage has arrived. "
                                f"Sender: {sender}. Message: {snippet}. "
                                "Notify the user naturally in one short sentence in the user's language."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": prompt}]},
                                turn_complete=True,
                            )
            except Exception as e:
                print(f"[iMessage] Monitor error: {e}")

            try:
                interval = max(5, int(await asyncio.to_thread(get_imessage_monitor_interval)))
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                interval = 15
            await asyncio.sleep(interval)

    async def _run_mail_monitor(self) -> None:
        """Poll Apple Mail for new unread messages and announce new arrivals."""
        await asyncio.sleep(8)
        while True:
            try:
                alerts = await asyncio.to_thread(poll_mail_alerts)
                if alerts:
                    for alert in alerts[-3:]:
                        sender = alert.get("sender") or "Unknown"
                        subject = alert.get("subject") or "No subject"
                        self.ui.write_log(f"[Mail] {sender} — {subject[:140]}")

                    if self.session:
                        with self._speaking_lock:
                            speaking = self._is_speaking
                        recent_speech = (time.monotonic() - self._last_user_speech) < 8
                        if not speaking and not recent_speech:
                            newest = alerts[-1]
                            sender = newest.get("sender") or "Unknown sender"
                            subject = newest.get("subject") or "No subject"
                            prompt = (
                                "[MAIL_ALERT] A new email has arrived. "
                                f"Sender: {sender}. Subject: {subject}. "
                                "Notify the user naturally in one short sentence in the user's language."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": prompt}]},
                                turn_complete=True,
                            )
            except Exception as e:
                print(f"[Mail] Monitor error: {e}")

            try:
                interval = max(10, int(await asyncio.to_thread(get_mail_monitor_interval)))
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                interval = 30
            await asyncio.sleep(interval)

    # ── Proactive mode ────────────────────────────────────────────────────────  

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    async def _run_visual_monitor(self) -> None:
        if not self._visual_watch_cfg.get("enabled", True):
            self.ui.set_visual_watch_status("Visual watch: disabled", "off")
            return

        interval = float(self._visual_watch_cfg.get("interval_seconds", 3.5) or 3.5)
        while True:
            try:
                await asyncio.sleep(interval)
                total_targets = len(self._visual_monitor.list_targets())
                if total_targets == 0:
                    self.ui.set_visual_watch_status("Visual watch: idle (no targets)", "neutral")
                events = await asyncio.to_thread(self._visual_monitor.poll_once)
                if not events:
                    continue

                for event in events:
                    if event.get("error"):
                        err_target = str(event.get("target_id") or event.get("label") or "visual-target")
                        err_text = str(event.get("error") or "unknown error")
                        now_ts = time.monotonic()
                        last = self._visual_error_seen.get(err_target)
                        should_log = True
                        if last and last[0] == err_text and (now_ts - last[1]) < 20.0:
                            should_log = False
                        self._visual_error_seen[err_target] = (err_text, now_ts)
                        if should_log:
                            self.ui.write_log(f"VISUAL: {event['label']} error — {err_text}")
                        self.ui.set_visual_watch_status(
                            f"Visual watch error: {event.get('label', 'target')}",
                            "bad",
                            err_text,
                        )
                        if self._dashboard:
                            await self._dashboard.broadcast({
                                "type": "visual_watch",
                                "state": "error",
                                "payload": event,
                                "ts": datetime.now().isoformat(),
                            })
                        continue

                    changed = bool(event.get("changed"))
                    status = "changed" if changed else "steady"
                    self.ui.write_log(f"VISUAL: {event['label']} {status}")
                    lvl = "ok" if changed else "neutral"
                    self.ui.set_visual_watch_status(
                        f"Visual watch: {event.get('label', 'target')} {status} ({total_targets} target{'s' if total_targets != 1 else ''})",
                        lvl,
                        event.get("capture_label", ""),
                    )

                    image_b64 = None
                    try:
                        import base64 as _b64
                        image_bytes = event.get("image_bytes")
                        if image_bytes:
                            image_b64 = _b64.b64encode(image_bytes).decode("ascii")
                    except Exception:
                        image_b64 = None

                    if self._dashboard:
                        await self._dashboard.broadcast({
                            "type": "visual_watch",
                            "state": status,
                            "payload": {
                                "target_id": event.get("target_id"),
                                "label": event.get("label"),
                                "target_type": event.get("target_type"),
                                "capture_label": event.get("capture_label"),
                                "changed": changed,
                                "mime_type": event.get("mime_type", "image/png"),
                                "image_b64": image_b64,
                                "updated_at": datetime.now().isoformat(),
                            },
                            "ts": datetime.now().isoformat(),
                        })

                    if not changed or not self.session:
                        continue

                    with self._speaking_lock:
                        speaking = self._is_speaking
                    if speaking or (time.monotonic() - self._last_user_speech) < 8:
                        continue

                    image_bytes = event.get("image_bytes")
                    mime_type = event.get("mime_type", "image/png")
                    label = event.get("label", "visual target")
                    prompt = (
                        f"[VISUAL_WATCH] The watched target '{label}' has changed. "
                        "Analyze the image and give a concise, actionable update focused on the visible state, controls, text, or task progress."
                    )

                    try:
                        import base64 as _b64
                        b64 = _b64.b64encode(image_bytes).decode("ascii")
                        await self.session.send_client_content(
                            turns={"parts": [
                                {"inline_data": {"mime_type": mime_type, "data": b64}},
                                {"text": prompt},
                            ]},
                            turn_complete=True,
                        )
                    except Exception as e:
                        print(f"[VisualWatch] ⚠️ Could not send watched capture: {e}")
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                print(f"[VisualWatch] ⚠️ {e}")
            except Exception as e:
                print(f"[VisualWatch] ⚠️ {e}")

    def _build_diagnostics_payload(self, status_snapshot: dict) -> dict:
        mic_q, mic_max = self._queue_fill(self.out_queue)
        spk_q, spk_max = self._queue_fill(self.audio_in_queue)
        up_seconds = max(0, int(time.time() - self._started_ts))
        return {
            "uptime_seconds": up_seconds,
            "state": "active" if self.session else "sleeping",
            "audio_profile": self._audio_profile_name,
            "audio": {
                "mic_queue": mic_q,
                "mic_queue_max": mic_max,
                "speaker_queue": spk_q,
                "speaker_queue_max": spk_max,
                "mic_dropped": int(self._audio_diag.get("mic_dropped", 0)),
                "speaker_dropped": int(self._audio_diag.get("speaker_dropped", 0)),
                "phone_dropped": int(self._audio_diag.get("phone_dropped", 0)),
                "speaker_received_bytes": int(self._audio_diag.get("speaker_received_bytes", 0)),
                "speaker_played_bytes": int(self._audio_diag.get("speaker_played_bytes", 0)),
            },
            "system": status_snapshot,
            "model": self._current_live_model or "disconnected",
        }

    async def _run_diagnostics_stream(self) -> None:
        if not self._diag_stream_cfg.get("enabled", True):
            return
        interval = float(self._diag_stream_cfg.get("interval_seconds", 2.5) or 2.5)
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._dashboard:
                    continue
                status = await asyncio.to_thread(get_system_status)
                payload = self._build_diagnostics_payload(status)
                await self._dashboard.broadcast(
                    {
                        "type": "diagnostics",
                        "payload": payload,
                        "ts": datetime.now().isoformat(),
                    }
                )
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                print(f"[Diagnostics] ⚠️ {e}")
            except Exception as e:
                print(f"[Diagnostics] ⚠️ {e}")

    async def _run_predictive_automation(self) -> None:
        if not self._predictive_cfg.get("enabled", True):
            return
        interval = int(self._predictive_cfg.get("interval_seconds", 75) or 75)
        silence_window = int(self._predictive_cfg.get("silence_seconds", 25) or 25)

        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.sleep(interval)
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < silence_window
                if speaking or recent_speech:
                    continue

                suggestions = self._predictive_daemon.generate_suggestions()
                if not suggestions:
                    continue

                suggestion = suggestions[0]
                conf = float(suggestion.get("confidence", 0.0))
                summary = str(suggestion.get("summary", ""))
                self.ui.write_log(f"SYS: Predictive suggestion ({conf:.2f}) — {summary}")

                if self._dashboard:
                    await self._dashboard.broadcast(
                        {
                            "type": "predictive_suggestion",
                            "payload": suggestion,
                            "ts": datetime.now().isoformat(),
                        }
                    )

                if self.session and self._predictive_cfg.get("voice_announce", False):
                    prompt = (
                        "[PREDICTIVE_AUTOMATION] Briefly suggest this automation idea to the user in one sentence: "
                        f"{summary}"
                    )
                    await self.session.send_client_content(
                        turns={"parts": [{"text": prompt}]},
                        turn_complete=True,
                    )
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e).lower():
                    return
                print(f"[Predictive] ⚠️ {e}")
            except Exception as e:
                print(f"[Predictive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────        

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                timeout = float(self._audio_cfg.get("phone_idle_timeout_seconds", 0.35))
                chunk = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # Brief phone silence: release PC mic quickly for smoother handoff.
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                    self._audio_diag_inc("phone_enqueued")
                except asyncio.QueueFull:
                    self._audio_diag_inc("phone_dropped")
                    try:
                        self.out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.out_queue.put_nowait(chunk)
                        self._audio_diag_inc("phone_enqueued")
                    except asyncio.QueueFull:
                        self._audio_diag_inc("phone_dropped")
                        pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    async def _run_vps_local_worker(self) -> None:
        """Poll the VPS queue and run only the local actions that belong on this Mac."""
        while True:
            vps_url = os.getenv("JARVIS_VPS_URL")
            if not vps_url:
                self._vps_link_established_said = False
                await asyncio.sleep(15)
                continue
            try:
                worker = self._local_worker or LocalWorker(vps_url)
                self._local_worker = worker

                try:
                    with urllib.request.urlopen(f"{vps_url.rstrip('/')}/api/ops", timeout=8) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    payload = {}

                if bool(payload.get("connected", False)) and not self._vps_link_established_said:
                    self._vps_link_established_said = True
                    if self.session:
                        self.speak("Server link established.")
                    self.ui.write_log("SYS: Server link established.")

                tasks = worker.poll_for_tasks(limit=5)
                if tasks:
                    self.ui.write_log(f"SYS: Local worker processed {len(tasks)} queued task(s) from VPS.")
                    for task in tasks:
                        action = str(task.get("action") or "").strip().lower()
                        payload = task.get("payload") or {}
                        if action == "remote_chat":
                            text = str(payload.get("text") or payload.get("prompt") or "").strip()
                            if text and self.session:
                                try:
                                    await self.session.send_client_content(
                                        turns={"parts": [{"text": text}]},
                                        turn_complete=True,
                                    )
                                    self.ui.write_log(f"SYS: Remote chat prompt delivered: {text[:80]}")
                                except Exception as exc:
                                    self.ui.write_log(f"SYS: Remote chat failed: {exc}")
                            continue
                        result = worker.execute_local_action(action, payload)
                        print(f"[Local Worker] {action}: {result}")
                        if result.get("status") == "rejected":
                            self.ui.write_log(f"SYS: VPS task rejected on local worker: {action}")
            except Exception as e:
                print(f"[Local Worker] Poll error: {e}")
            await asyncio.sleep(5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._print_startup_banner()

        self._run_macos_permission_preflight()
        self._configure_imessage_cold_start_bridge()
        asyncio.create_task(self._run_cold_wake_bridge_health())
        asyncio.create_task(self._run_audio_diagnostics())
        self.ui.write_log("SYS: Audio ready.")

        if self._wake_protocol_cfg.get("autostart", True):
            interval = int(self._wake_protocol_cfg.get("interval_seconds", 15) or 15)
            await asyncio.to_thread(imessage_monitor_start, interval)
            self.ui.write_log("SYS: Monitor ready.")

        if self._mail_monitor_cfg.get("autostart", True):
            interval = int(self._mail_monitor_cfg.get("interval_seconds", 30) or 30)
            await asyncio.to_thread(mail_monitor_start, interval)
            self.ui.write_log("SYS: Monitor ready.")

        # Start the local dashboard only when this Mac is intended to be the public entrypoint.
        # When a VPS brain is configured, keep the dashboard and public tunnel on the server instead,
        # so a local shutdown does not kill the remote JARVIS brain.
        vps_url = os.getenv("JARVIS_VPS_URL", "").strip()
        if vps_url:
            self.ui.write_log("SYS: VPS brain configured; local dashboard disabled to keep remote access alive.")
            self._dashboard = None
        else:
            try:
                from dashboard.server import DashboardServer
                self._dashboard = DashboardServer()
                self._dashboard.set_connect_callback(self._on_phone_connected)
                self._dashboard.set_public_url_callback(self._on_public_url_changed)
                asyncio.create_task(self._dashboard.serve())
                # Runs for the whole lifetime, not just inside an active session
                asyncio.create_task(self._process_dashboard_commands())
            except Exception as e:
                print(f"[Dashboard] Disabled: {e}")
                self._dashboard = None

        asyncio.create_task(self._run_vps_local_worker())

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                runtime_cfg = self._load_runtime_config()
                self._tts_player = None
                self._use_external_tts = False
                if self._external_tts_enabled(runtime_cfg):
                    try:
                        self._tts_player = create_tts_player(runtime_cfg)
                        self._use_external_tts = True
                        self.ui.write_log("SYS: Voice ready.")
                    except Exception as e:
                        self.ui.write_log(
                            f"SYS: {self._external_tts_label(runtime_cfg)} unavailable, using Gemini voice. {e}"
                        )
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                connected = False
                connect_error = None
                for live_model in self._available_live_models():
                    try:
                        print(f"[JARVIS] Trying live model: {live_model}")
                        self._current_live_model = live_model
                        async with (
                            client.aio.live.connect(model=live_model, config=config) as session,
                            asyncio.TaskGroup() as tg,
                        ):
                            connected = True
                            self.session          = session
                            self.audio_in_queue   = asyncio.Queue(maxsize=int(self._audio_cfg.get("speaker_queue_max_chunks", 72)))
                            self.out_queue        = asyncio.Queue(maxsize=int(self._audio_cfg.get("mic_queue_max_chunks", 24)))
                            self._turn_done_event = asyncio.Event()

                            # Reset transient state that must not carry over from a previous session
                            self._pending_vision       = None
                            self._vision_cam_active    = False
                            self._vision_close_pending = False
                            self._vision_busy          = False
                            self._vision_last_time     = 0.0
                            self._interrupted          = False

                            print(f"[JARVIS] Connected on {live_model}.")
                            self.ui.set_state("LISTENING")
                            self.ui.write_log("SYS: Online.")

                            if self._dashboard:
                                await self._dashboard.broadcast({"type": "status", "state": "active"})

                            self._tts_pending_sentence = ""
                            if self._tts_player:
                                self._tts_sentence_queue = asyncio.Queue()
                                tg.create_task(self._tts_worker())
                            tg.create_task(self._send_realtime())
                            tg.create_task(self._listen_audio())
                            self._start_speech_listener()
                            self._start_speech_listener()
                            tg.create_task(self._receive_audio())
                            if not self._tts_player:
                                tg.create_task(self._play_audio())
                            tg.create_task(self._run_system_monitor())
                            tg.create_task(self._run_background_monitor())
                            tg.create_task(self._run_imessage_monitor())
                            tg.create_task(self._run_mail_monitor())
                            tg.create_task(self._run_proactive_mode())
                            tg.create_task(self._run_predictive_automation())
                            tg.create_task(self._run_diagnostics_stream())
                            tg.create_task(self._run_visual_monitor())
                            if self._dashboard:
                                tg.create_task(self._relay_phone_audio())

                            # Morning briefing — fires once per process launch (if enabled)
                            if not self._briefing_sent and get_brief_enabled():
                                self._briefing_sent = True
                                tg.create_task(self._send_startup_briefing())
                        break
                    except Exception as e:
                        connect_error = e
                        if _is_live_model_unavailable_error(e):
                            self._block_live_model(live_model, seconds=900)
                            print(f"[JARVIS] Live model unavailable: {live_model} — trying fallback")
                            continue
                        if _is_live_audio_unsupported_error(e):
                            self._block_live_model(live_model, seconds=900)
                            print(f"[JARVIS] Live model rejected audio on {live_model} — trying fallback")
                            continue
                        raise

                if not connected and connect_error:
                    raise connect_error

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = _error_text(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                if _is_live_audio_unsupported_error(e) and self._current_live_model:
                    self._block_live_model(self._current_live_model, seconds=900)
                    self.ui.write_log(
                        f"NET: Model audio unsupported ({self._current_live_model}) — switching model."
                    )

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "api key not valid" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                if _is_billing_error(e):
                    self.ui.write_log(
                        "ERR: Live model service is unavailable because billing/credits are depleted or quota is exhausted. "
                        "Please add credits or wait for quota reset."
                    )
                    self.ui.set_state("SLEEPING")
                    return

                # Network / timeout errors — log clearly and back off
                is_disconnect = _is_disconnect_error(e)
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_disconnect:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        "NET: Live session dropped — reconnecting shortly."
                    )
                elif is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                self._current_live_model = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

                vps_url = os.getenv('JARVIS_VPS_URL')
                if vps_url:
                    try:
                        local_memory = load_memory()
                        merged_memory = load_memory_with_vps_sync(local_memory, vps_url)
                        if merged_memory:
                            push_memory_to_vps(vps_url, merged_memory)
                    except Exception as e:
                        print(f"[Memory Sync] ⚠️ Could not sync to VPS: {e}")

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

class _HeadlessUI:
    """Minimal no-GUI UI for VPS-only deployments."""

    class _Root:
        def mainloop(self):
            while True:
                time.sleep(1)

    def __init__(self):
        self.root = self._Root()
        self._muted = False

    def __getattr__(self, name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = bool(value)

    def wait_for_api_key(self):
        return None

    def set_state(self, *_args, **_kwargs):
        return None

    def write_log(self, *_args, **_kwargs):
        return None

    def notify_phone_connected(self):
        return None


def main():
    vps_url = (os.getenv("JARVIS_VPS_URL") or "").strip()
    headless_requested = str(os.getenv("JARVIS_HEADLESS") or "").strip().lower() in {"1", "true", "yes", "on"}
    if headless_requested:
        ui = _HeadlessUI()
    elif vps_url:
        ui = JarvisUI("face.png")
    else:
        ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()