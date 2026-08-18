import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
STATE_DIR = Path.home() / "Library" / "Application Support" / "JARVIS"
STATE_PATH = STATE_DIR / "imessage_wake_state.json"
LOG_PATH = STATE_DIR / "imessage_wake_bridge.log"
JARVIS_LAUNCH_LOG_PATH = STATE_DIR / "jarvis_cold_start.log"
HEALTH_PATH = STATE_DIR / "imessage_wake_bridge_health.json"
WATCHDOG_STALL_SECONDS = 180


_heartbeat_lock = threading.Lock()
_last_heartbeat = time.monotonic()


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _write_health(status: str, **extra) -> None:
    payload = {
        "ts": time.time(),
        "pid": os.getpid(),
        "status": status,
    }
    payload.update(extra)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def _touch_heartbeat() -> None:
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.monotonic()


def _seconds_since_heartbeat() -> float:
    with _heartbeat_lock:
        return time.monotonic() - _last_heartbeat


def _sleep_with_heartbeat(seconds: int | float) -> None:
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        _touch_heartbeat()
        time.sleep(min(1.0, remaining))
    _touch_heartbeat()


def _start_watchdog() -> None:
    def _watchdog_loop() -> None:
        while True:
            time.sleep(5)
            stalled_for = _seconds_since_heartbeat()
            if stalled_for > WATCHDOG_STALL_SECONDS:
                _log(f"watchdog: bridge stalled for {stalled_for:.1f}s; exiting for launchd restart")
                _write_health("stalled", stalled_for=round(stalled_for, 1))
                os._exit(1)

    threading.Thread(target=_watchdog_loop, name="wake-bridge-watchdog", daemon=True).start()


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        _log(f"config read error: {e}")
    return {}


def _preferred_python_exec(cfg: dict | None = None) -> str:
    project_candidates: list[str] = []
    for rel in (".venv-1/bin/python", ".venv/bin/python", ".venv-1/bin/python3", ".venv/bin/python3"):
        candidate = str((BASE_DIR / rel).expanduser())
        if candidate:
            project_candidates.append(candidate)

    for candidate in project_candidates:
        try:
            p = Path(candidate).expanduser()
            if p.exists():
                return str(p)
        except Exception:
            continue

    if cfg is not None:
        for key in ("imessage_cold_start_python", "jarvis_python_exec"):
            value = str(cfg.get(key, "") or "").strip()
            if value:
                try:
                    p = Path(value).expanduser()
                    if p.exists():
                        return str(p)
                except Exception:
                    continue

    return str(Path(sys.executable).resolve())


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"state load error: {e}")
    return {
        "last_seen_rowid": None,
        "last_wake_rowid": 0,
        "last_wake_ts": 0.0,
    }


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        _log(f"state save error: {e}")


def _fence_startup_cursor(state: dict, latest_rowid: int | None) -> bool:
    """Ignore messages that predate this bridge process."""
    if latest_rowid is None:
        return False
    previous = state.get("last_seen_rowid")
    previous_value = int(previous) if previous is not None else 0
    latest_value = int(latest_rowid)
    if latest_value <= previous_value:
        return False
    state["last_seen_rowid"] = latest_value
    return True


def _digits_only(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _phrase_in_text(phrase: str, text: str) -> bool:
    phrase_n = _normalize_match_text(phrase)
    text_n = _normalize_match_text(text)
    if not phrase_n or not text_n:
        return False
    return f" {phrase_n} " in f" {text_n} "


def _phone_variants(value: str) -> set[str]:
    d = _digits_only(value)
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


def _resolve_vps_url() -> str:
    env_value = (os.getenv("JARVIS_VPS_URL") or "").strip()
    if env_value:
        return env_value.rstrip("/")

    try:
        cfg_path = BASE_DIR / "config" / "api_keys.json"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in ("JARVIS_VPS_URL", "JARVIS_VPS_BASE_URL", "JARVIS_PUBLIC_URL", "PUBLIC_ENTRY_URL"):
                    value = str(data.get(key, "") or "").strip()
                    if value:
                        return value.rstrip("/")
    except Exception:
        pass

    return ""


def _messages_db_path() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


def _connect_messages_db() -> sqlite3.Connection:
    db_path = _messages_db_path()
    if not db_path.exists():
        raise RuntimeError("Messages database not found")
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "unable to open database file" in msg:
            raise RuntimeError(
                "Messages DB access denied. Grant Full Disk Access to the Python binary used by launchd. "
                f"Expected DB path: {db_path}"
            ) from e
        raise


def _run_sqlite_query_via_osascript(sql: str) -> list[str]:
    db_path = _messages_db_path()
    if not db_path.exists():
        raise RuntimeError("Messages database not found")

    sql_esc = sql.replace('"', '\\"')
    shell_cmd = f"sqlite3 -readonly -separator '||' '{db_path}' \"{sql_esc}\""
    shell_cmd_esc = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    apple_line = f'do shell script "{shell_cmd_esc}"'

    proc = subprocess.run(
        ["osascript", "-e", apple_line],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Unknown osascript sqlite error").strip()
        if "authorization denied" in err.lower():
            raise RuntimeError(
                "Apple sqlite query denied. Grant Full Disk Access to /usr/bin/osascript "
                "(and Terminal/VS Code), then restart the wake bridge."
            )
        raise RuntimeError(f"Apple sqlite query failed: {err}")

    out = (proc.stdout or "").strip()
    if not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _peek_latest_rowid_apple() -> int | None:
    rows = _run_sqlite_query_via_osascript("SELECT IFNULL(MAX(ROWID), 0) FROM message;")
    if not rows:
        return None
    raw = rows[0].strip()
    if not raw or raw == "0":
        return None
    return int(raw)


def _peek_latest_rowid() -> int | None:
    with _connect_messages_db() as conn:
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def _fetch_new_messages(after_rowid: int | None) -> list[dict]:
    where = [
        "m.is_from_me = 0",
        "m.text IS NOT NULL",
        "LENGTH(TRIM(m.text)) > 0",
    ]
    params: list[object] = []
    if after_rowid is not None:
        where.append("m.ROWID > ?")
        params.append(int(after_rowid))

    sql = f"""
        SELECT
            m.ROWID,
            COALESCE(c.display_name, c.chat_identifier, h.id, 'Unknown') AS chat_name,
            COALESCE(h.id, c.chat_identifier, 'Unknown') AS sender,
            m.text
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {' AND '.join(where)}
        ORDER BY m.ROWID ASC
        LIMIT 50
    """

    with _connect_messages_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    out = []
    for rowid, chat_name, sender, text in rows:
        out.append(
            {
                "rowid": int(rowid),
                "chat_name": (chat_name or "").strip(),
                "sender": (sender or "").strip(),
                "text": (text or "").strip(),
            }
        )
    return out


def _fetch_new_messages_apple(after_rowid: int | None) -> list[dict]:
    where = [
        "m.is_from_me = 0",
        "m.text IS NOT NULL",
        "LENGTH(TRIM(m.text)) > 0",
    ]
    if after_rowid is not None:
        where.append(f"m.ROWID > {int(after_rowid)}")

    sql = f"""
SELECT
  m.ROWID,
  COALESCE(c.display_name, c.chat_identifier, h.id, 'Unknown') AS chat_name,
  COALESCE(h.id, c.chat_identifier, 'Unknown') AS sender,
  REPLACE(REPLACE(m.text, CHAR(10), ' '), CHAR(13), ' ') AS text
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE {' AND '.join(where)}
ORDER BY m.ROWID ASC
LIMIT 50;
""".strip()

    rows = _run_sqlite_query_via_osascript(sql)
    out: list[dict] = []
    for line in rows:
        parts = line.split("||", 3)
        if len(parts) != 4:
            continue
        rowid_s, chat_name, sender, text = parts
        try:
            rowid = int((rowid_s or "").strip())
        except Exception:
            continue
        out.append(
            {
                "rowid": rowid,
                "chat_name": (chat_name or "").strip(),
                "sender": (sender or "").strip(),
                "text": (text or "").strip(),
            }
        )
    return out


def _sender_matches(expected_sender: str, sender: str, chat_name: str) -> bool:
    expected_sender = (expected_sender or "").strip()
    if not expected_sender:
        return True
    expected_variants = _phone_variants(expected_sender)
    expected_norm = _normalize_match_text(expected_sender)
    if not expected_variants and not expected_norm:
        return True
    incoming = [sender, chat_name]

    # Primary matching for phone-number based sender IDs.
    for candidate in incoming:
        candidate_variants = _phone_variants(candidate)
        if not candidate_variants:
            continue
        for cv in candidate_variants:
            for ev in expected_variants:
                if cv == ev or cv.endswith(ev) or ev.endswith(cv):
                    return True

    # Fallback matching for email/contact-name sender IDs.
    for candidate in incoming:
        candidate_norm = _normalize_match_text(candidate)
        if not candidate_norm or not expected_norm:
            continue
        if (
            candidate_norm == expected_norm
            or candidate_norm.endswith(expected_norm)
            or expected_norm.endswith(candidate_norm)
        ):
            return True
    return False


def _is_jarvis_running(target_script: Path) -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", str(target_script)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0 and bool((proc.stdout or "").strip())
    except Exception:
        return False


def _python_app_bundle_from_exec(python_exec: str) -> str | None:
    p = Path(python_exec)
    s = str(p)
    marker = "/Frameworks/Python.framework/Versions/"
    if marker not in s:
        return None
    try:
        head, tail = s.split(marker, 1)
        version = tail.split("/", 1)[0]
        app_bundle = (
            Path(head)
            / "Frameworks"
            / "Python.framework"
            / "Versions"
            / version
            / "Resources"
            / "Python.app"
        )
        if app_bundle.exists():
            return str(app_bundle)
    except Exception:
        return None
    return None


def _launch_jarvis(python_exec: str, target_script: Path, extra_env: dict | None = None) -> bool:
    launch_env = {**os.environ, **(extra_env or {})}
    for attempt in range(1, 4):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(JARVIS_LAUNCH_LOG_PATH, "a", encoding="utf-8") as launch_log:
                launch_log.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launching {target_script} (attempt {attempt})\n"
                )
                launch_log.flush()

                proc = subprocess.Popen(
                    [python_exec, str(target_script)],
                    cwd=str(BASE_DIR),
                    stdin=subprocess.DEVNULL,
                    stdout=launch_log,
                    stderr=launch_log,
                    start_new_session=True,
                    close_fds=True,
                    env=launch_env,
                )

                _log(f"launch requested (attempt={attempt}, pid={proc.pid}, python_exec={python_exec})")

                time.sleep(2)
                if _is_jarvis_running(target_script):
                    _log(f"launch confirmed (attempt={attempt}, pid={proc.pid})")
                    try:
                        _run_applescript = [
                            "-e",
                            f'tell application "System Events" to set frontmost of first process whose unix id is {proc.pid} to true'
                        ]
                        subprocess.run(["osascript", *_run_applescript], capture_output=True, text=True, timeout=5)
                    except Exception as e:
                        _log(f"frontmost activation failed: {e}")
                    return True

                py_app_bundle = _python_app_bundle_from_exec(python_exec)
                if py_app_bundle:
                    _log(f"direct launch not running; trying app-bundle fallback (attempt={attempt}, bundle={py_app_bundle})")
                    subprocess.Popen(
                        ["open", "-n", "-a", py_app_bundle, "--args", str(target_script)],
                        cwd=str(BASE_DIR),
                        stdout=launch_log,
                        stderr=launch_log,
                    )
                    time.sleep(4)
                    if _is_jarvis_running(target_script):
                        _log(f"launch confirmed (fallback attempt={attempt})")
                        return True

                _log(f"launch attempt {attempt} did not stay running")
        except Exception as e:
            _log(f"launch failed on attempt {attempt}: {e}")

        time.sleep(2)

    _log("launch failed after retries")
    return False


def _send_imessage(receiver: str, message_text: str) -> None:
    script = [
        "on run argv",
        "set targetText to item 1 of argv",
        "set outgoingText to item 2 of argv",
        "tell application \"Messages\"",
        "set targetService to first service whose service type = iMessage",
        "try",
        "set targetBuddy to buddy targetText of targetService",
        "send outgoingText to targetBuddy",
        "on error",
        "try",
        "set targetChat to first chat whose name contains targetText",
        "send outgoingText to targetChat",
        "end try",
        "end try",
        "end tell",
        "end run",
    ]
    cmd = ["osascript"]
    for line in script:
        cmd.extend(["-e", line])
    cmd.extend(["--", receiver, message_text])
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        _log(f"ack send failed: {e}")


def _refresh_remote_access_snapshot() -> tuple[str, str, str]:
    def _read_config_url() -> str:
        try:
            cfg_path = BASE_DIR / "config" / "api_keys.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for key in ("public_remote_url", "JARVIS_PUBLIC_URL", "PUBLIC_ENTRY_URL"):
                    value = str(data.get(key, "") or "").strip()
                    if value:
                        return value.rstrip("/")
        except Exception:
            pass
        for key in ("JARVIS_PUBLIC_URL", "PUBLIC_ENTRY_URL"):
            value = (os.getenv(key) or "").strip()
            if value:
                return value.rstrip("/")
        return ""

    def _generate_key() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(__import__("secrets").choice(alphabet) for _ in range(6))

    url = _read_config_url()
    key = _generate_key()
    os.environ["JARVIS_REMOTE_KEY"] = key

    if url:
        for probe in (url, f"{url.rstrip('/')}/api/status", f"{url.rstrip('/')}/api/health"):
            try:
                req = urllib.request.Request(probe, headers={"User-Agent": "JARVIS-wake-bridge/1.0"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    payload = resp.read(2000)
                if payload:
                    try:
                        parsed = json.loads(payload.decode("utf-8", errors="replace"))
                    except Exception:
                        parsed = {}
                    if isinstance(parsed, dict):
                        remote_url = str(parsed.get("public_entry") or parsed.get("url") or parsed.get("dashboard_url") or url).strip()
                        if remote_url:
                            url = remote_url.rstrip("/")
                    break
            except Exception:
                continue

    if not url:
        url = "remote dashboard unavailable"

    auto_login = f"{url}/auto-login?key={key}" if url and key and "remote dashboard unavailable" not in url else ""
    return url, key, auto_login


def _build_remote_wake_notice(url: str, key: str = "", auto_login: str = "") -> str:
    url = (url or os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "remote dashboard unavailable").strip()
    lines = [
        "JARVIS wake accepted",
    ]
    if url and "remote dashboard unavailable" not in url:
        lines.append(f"Open: {url}")
    else:
        lines.append("Open: remote dashboard unavailable")
        lines.append("Power is off or the Mac is offline; remote access is unavailable until the Mac boots or the VPS owns the wake path.")
    if key:
        lines.append(f"Key: {key}")
    if auto_login:
        lines.append(f"Auto: {auto_login}")
    if url and "remote dashboard unavailable" not in url:
        lines.append("Status: VPS remote voice active")
        lines.append("SECURITY: PUBLIC=ON | PIN=OFF")
    else:
        lines.append("Status: remote access unavailable while the Mac is off")
    return "\n".join(lines)


def _fetch_vps_remote_access_snapshot(vps_url: str) -> tuple[str, str, str]:
    try:
        if not vps_url:
            return "", "", ""
        url = f"{vps_url.rstrip('/')}/api/remote_access"
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-wake-bridge/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = resp.read(4096)
        if not payload:
            return "", "", ""
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return "", "", ""
        public_url = str(data.get("url") or "").strip()
        key = str(data.get("key") or "").strip()
        auto_login = str(data.get("auto_login_url") or "").strip()
        return public_url, key, auto_login
    except Exception:
        return "", "", ""


def _fetch_vps_remote_uplink(vps_url: str) -> tuple[str, str, str, str]:
    try:
        if not vps_url:
            return "", "", "", ""
        url = f"{vps_url.rstrip('/')}/api/remote_uplink"
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-wake-bridge/1.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = resp.read(4096)
        if not payload:
            return "", "", "", ""
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return "", "", "", ""
        public_url = str(data.get("url") or "").strip()
        key = str(data.get("key") or "").strip()
        auto_login = str(data.get("auto_login_url") or "").strip()
        message = str(data.get("message") or "").strip()
        return public_url, key, auto_login, message
    except Exception:
        return "", "", "", ""


def main() -> int:
    if platform.system() != "Darwin":
        return 0

    _log("bridge started")
    _touch_heartbeat()
    _write_health("starting")
    _start_watchdog()

    state = _load_state()
    mode = "db"
    try:
        mode = str(_read_config().get("imessage_cold_start_mode", "db") or "db").strip().lower()
        if mode not in {"db", "apple"}:
            mode = "db"
        latest_rowid = _peek_latest_rowid_apple() if mode == "apple" else _peek_latest_rowid()
        if _fence_startup_cursor(state, latest_rowid):
            _save_state(state)
            _log(f"startup cursor fenced at rowid {latest_rowid}; older messages ignored")
    except Exception as e:
        _log(f"initial rowid read failed: {e}")

    db_error_logged = False

    while True:
        _touch_heartbeat()
        cfg = _read_config()
        mode = str(cfg.get("imessage_cold_start_mode", "db") or "db").strip().lower()
        if mode not in {"db", "apple"}:
            mode = "db"
        enabled = bool(cfg.get("imessage_cold_start_enabled", True))
        _write_health("running", mode=mode, enabled=enabled)
        if not enabled:
            _write_health("disabled", mode=mode)
            _sleep_with_heartbeat(10)
            continue

        sender_allowed = str(cfg.get("imessage_wake_sender", "") or "").strip()
        phrase = str(cfg.get("imessage_wake_phrase", "jarvis wake") or "jarvis wake").strip().lower()
        remote_uplink_phrase = str(cfg.get("imessage_remote_uplink_phrase", "remote uplink") or "remote uplink").strip().lower()
        secret = str(cfg.get("imessage_wake_secret", "") or "").strip().lower()
        interval = max(5, min(int(cfg.get("imessage_monitor_interval_seconds", 15) or 15), 300))
        wake_cooldown = max(15, min(int(cfg.get("imessage_wake_cooldown_seconds", 120) or 120), 3600))

        python_exec = _preferred_python_exec(cfg)
        target_raw = str(cfg.get("imessage_cold_start_target", "") or "").strip()
        target_script = Path(target_raw) if target_raw else None
        if target_script is None:
            target_script = BASE_DIR / "main.py"
        if not target_script.is_absolute():
            target_script = (BASE_DIR / target_script).resolve()

        try:
            last = state.get("last_seen_rowid")
            msgs = _fetch_new_messages_apple(last) if mode == "apple" else _fetch_new_messages(last)
            if msgs:
                state["last_seen_rowid"] = msgs[-1]["rowid"]
                _save_state(state)

            for msg in msgs:
                rowid = int(msg.get("rowid") or 0)
                last_wake_rowid = int(state.get("last_wake_rowid") or 0)
                if rowid and rowid <= last_wake_rowid:
                    continue

                text = str(msg.get("text") or "")
                match_remote_uplink = _phrase_in_text(remote_uplink_phrase, text)
                match_wake = _phrase_in_text(phrase, text)
                if not (match_wake or match_remote_uplink):
                    continue
                trigger_label = "remote uplink" if match_remote_uplink else "wake phrase"
                _log(
                    f"{trigger_label} matched "
                    f"(rowid={msg.get('rowid')}, sender={msg.get('sender')}, chat={msg.get('chat_name')})"
                )

                now = time.time()
                last_wake_ts = float(state.get("last_wake_ts") or 0.0)
                if (now - last_wake_ts) < wake_cooldown:
                    _log(f"{trigger_label} suppressed (cooldown)")
                    state["last_wake_rowid"] = rowid
                    _save_state(state)
                    continue

                text_l = text.strip().lower()
                if secret and secret not in text_l:
                    _log(f"{trigger_label} matched but secret mismatched")
                    continue
                sender_ok = _sender_matches(sender_allowed, msg.get("sender", ""), msg.get("chat_name", ""))
                if not sender_ok:
                    _log(
                        f"{trigger_label} matched but sender unauthorized "
                        f"(allowed={sender_allowed!r}, sender={msg.get('sender')!r}, chat={msg.get('chat_name')!r})"
                    )
                    continue

                state["last_wake_rowid"] = rowid
                state["last_wake_ts"] = now
                _save_state(state)

                if match_remote_uplink:
                    target = msg.get("sender") or msg.get("chat_name") or sender_allowed
                    vps_url = _resolve_vps_url()
                    if vps_url:
                        try:
                            health_url = f"{vps_url.rstrip('/')}/api/health"
                            req = urllib.request.Request(health_url, headers={"User-Agent": "JARVIS-wake-bridge/1.0"})
                            with urllib.request.urlopen(req, timeout=4) as resp:
                                payload = resp.read(2048)
                            data = json.loads(payload.decode("utf-8", errors="replace")) if payload else {}
                            if isinstance(data, dict) and data.get("ok") is not False:
                                _log(f"remote uplink triggered against VPS at {vps_url}; skipping local launch")
                                public_url, key, auto, message = _fetch_vps_remote_uplink(vps_url)
                                if not public_url:
                                    public_url, key, auto = _fetch_vps_remote_access_snapshot(vps_url)
                                    if not public_url:
                                        public_url, key, auto = _refresh_remote_access_snapshot()
                                notice = message or _build_remote_wake_notice(public_url, key, auto)
                                _send_imessage(target, notice)
                                continue
                        except Exception:
                            pass
                    public_url, key, auto = _refresh_remote_access_snapshot()
                    _send_imessage(target, _build_remote_wake_notice(public_url, key, auto))
                    continue

                if _is_jarvis_running(target_script):
                    _log("wake command received but jarvis already running")
                    # Avoid chat spam while already running; cooldown above already records this wake.
                    continue

                vps_url = _resolve_vps_url()
                if vps_url:
                    try:
                        health_url = f"{vps_url.rstrip('/')}/api/health"
                        req = urllib.request.Request(health_url, headers={"User-Agent": "JARVIS-wake-bridge/1.0"})
                        with urllib.request.urlopen(req, timeout=4) as resp:
                            payload = resp.read(2048)
                        data = json.loads(payload.decode("utf-8", errors="replace")) if payload else {}
                        if isinstance(data, dict) and data.get("ok") is not False:
                            _log(f"VPS active at {vps_url}; skipping local launch to keep wake remote-first")
                            target = msg.get("sender") or msg.get("chat_name") or sender_allowed
                            public_url, key, auto = _fetch_vps_remote_access_snapshot(vps_url)
                            if not public_url:
                                public_url, key, auto = _refresh_remote_access_snapshot()
                            notice = _build_remote_wake_notice(public_url, key, auto)
                            _send_imessage(target, notice)
                            continue
                    except Exception:
                        pass

                launched = _launch_jarvis(python_exec, target_script)
                target = msg.get("sender") or msg.get("chat_name") or sender_allowed
                if launched:
                    if not target:
                        target = sender_allowed
                    public_url, key, auto = _refresh_remote_access_snapshot()
                    notice = _build_remote_wake_notice(public_url, key, auto)
                    _send_imessage(target, notice)
                else:
                    _send_imessage(target, "Wake received, but I could not launch JARVIS.")
        except Exception as e:
            err = str(e)
            _write_health("error", mode=mode, error=err[:220])
            if "Messages DB access denied" in err:
                if not db_error_logged:
                    _log(err)
                    db_error_logged = True
            else:
                _log(f"loop error: {e}")

        _sleep_with_heartbeat(interval)


if __name__ == "__main__":
    raise SystemExit(main())
