import platform
import sqlite3
import subprocess
import threading
from pathlib import Path


_MONITOR_LOCK = threading.Lock()
_MONITOR_STATE = {
    "enabled": False,
    "interval_seconds": 15,
    "last_seen_rowid": None,
}


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def _messages_db_path() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


def _connect_messages_db() -> sqlite3.Connection:
    db_path = _messages_db_path()
    if not db_path.exists():
        raise RuntimeError("Messages database not found at ~/Library/Messages/chat.db")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _run_applescript(lines: list[str], args: list[str] | None = None) -> str:
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    if args:
        cmd.append("--")
        cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Unknown AppleScript error").strip()
        raise RuntimeError(err)
    return (proc.stdout or "").strip()


def send_imessage(receiver: str, message_text: str) -> str:
    if not _is_mac():
        return "iMessage integration is only available on macOS."

    receiver = (receiver or "").strip()
    message_text = (message_text or "").strip()
    if not receiver:
        return "Please specify a recipient for iMessage."
    if not message_text:
        return "Please specify the iMessage text."

    script = [
        "on run argv",
        "set targetText to item 1 of argv",
        "set outgoingText to item 2 of argv",
        "tell application \"Messages\"",
        "set targetService to first service whose service type = iMessage",
        "try",
        "set targetBuddy to buddy targetText of targetService",
        "send outgoingText to targetBuddy",
        "return \"sent\"",
        "on error",
        "try",
        "set targetChat to first chat whose name contains targetText",
        "send outgoingText to targetChat",
        "return \"sent\"",
        "on error errMsg",
        "return \"error: \" & errMsg",
        "end try",
        "end try",
        "end tell",
        "end run",
    ]

    try:
        out = _run_applescript(script, [receiver, message_text])
    except Exception as e:
        return (
            "Could not send iMessage. macOS may require Automation permission for "
            f"Terminal/Python -> Messages. Details: {e}"
        )

    if out.lower().startswith("error:"):
        return f"Could not send iMessage: {out[6:].strip()}"
    return f"iMessage sent to {receiver}."


def _fetch_messages(limit: int = 5, unread_only: bool = False, newer_than_rowid: int | None = None) -> list[dict]:
    limit = max(1, min(int(limit or 5), 50))

    where = [
        "m.is_from_me = 0",
        "m.text IS NOT NULL",
        "LENGTH(TRIM(m.text)) > 0",
    ]
    params: list[object] = []

    if unread_only:
        where.append("m.is_read = 0")
    if newer_than_rowid is not None:
        where.append("m.ROWID > ?")
        params.append(int(newer_than_rowid))

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
            m.ROWID,
            COALESCE(c.display_name, c.chat_identifier, h.id, 'Unknown') AS chat_name,
            COALESCE(h.id, c.chat_identifier, 'Unknown') AS sender,
            m.text,
            datetime(
                (
                    CASE
                        WHEN m.date > 1000000000000 THEN (m.date / 1000000000)
                        ELSE m.date
                    END
                ) + 978307200,
                'unixepoch',
                'localtime'
            ) AS local_time
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {where_sql}
        ORDER BY m.ROWID DESC
        LIMIT ?
    """
    params.append(limit)

    with _connect_messages_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    data = []
    for rowid, chat_name, sender, text, local_time in rows:
        data.append(
            {
                "rowid": int(rowid),
                "chat_name": (chat_name or "Unknown").strip(),
                "sender": (sender or "Unknown").strip(),
                "text": (text or "").strip(),
                "time": (local_time or "").strip(),
            }
        )
    return data


def _peek_latest_rowid() -> int | None:
    with _connect_messages_db() as conn:
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def _notify_mac(title: str, body: str) -> None:
    # Escape for AppleScript string literal.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    script = [f'display notification "{safe_body}" with title "{safe_title}"']
    try:
        _run_applescript(script)
    except Exception:
        pass


def imessage_monitor_start(interval_seconds: int = 15) -> str:
    if not _is_mac():
        return "iMessage monitor is only available on macOS."
    interval_seconds = max(5, min(int(interval_seconds or 15), 300))

    try:
        last_rowid = _peek_latest_rowid()
    except Exception as e:
        return (
            "Could not access Messages database. Give Full Disk Access to Terminal/Python "
            f"in macOS Privacy & Security. Details: {e}"
        )

    with _MONITOR_LOCK:
        _MONITOR_STATE["enabled"] = True
        _MONITOR_STATE["interval_seconds"] = interval_seconds
        _MONITOR_STATE["last_seen_rowid"] = last_rowid
    return f"iMessage monitor enabled (every {interval_seconds}s)."


def imessage_monitor_stop() -> str:
    with _MONITOR_LOCK:
        _MONITOR_STATE["enabled"] = False
    return "iMessage monitor disabled."


def imessage_monitor_status() -> str:
    with _MONITOR_LOCK:
        enabled = _MONITOR_STATE["enabled"]
        interval_seconds = _MONITOR_STATE["interval_seconds"]
        last_seen_rowid = _MONITOR_STATE["last_seen_rowid"]
    state = "enabled" if enabled else "disabled"
    return (
        f"iMessage monitor is {state}. Interval: {interval_seconds}s. "
        f"Last seen rowid: {last_seen_rowid}."
    )


def get_imessage_monitor_interval() -> int:
    with _MONITOR_LOCK:
        return int(_MONITOR_STATE["interval_seconds"])


def poll_imessage_alerts() -> list[dict]:
    with _MONITOR_LOCK:
        enabled = bool(_MONITOR_STATE["enabled"])
        last_seen = _MONITOR_STATE["last_seen_rowid"]

    if not enabled or not _is_mac():
        return []

    try:
        fresh = _fetch_messages(limit=20, unread_only=False, newer_than_rowid=last_seen)
    except Exception:
        return []

    if not fresh:
        return []

    fresh_sorted = sorted(fresh, key=lambda m: m["rowid"])
    latest_rowid = fresh_sorted[-1]["rowid"]
    with _MONITOR_LOCK:
        _MONITOR_STATE["last_seen_rowid"] = latest_rowid

    alerts = []
    for item in fresh_sorted:
        title = f"New iMessage from {item['chat_name']}"
        body = item["text"][:120]
        _notify_mac(title=title, body=body)
        alerts.append(item)
    return alerts


def imessage_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower() or "read_latest"

    if action == "send":
        receiver = str(params.get("receiver", "")).strip()
        message_text = str(params.get("message_text", "")).strip()
        result = send_imessage(receiver, message_text)
        if player:
            player.write_log(f"[imessage] {result}")
        return result

    if action in {"read_latest", "read_unread"}:
        limit = int(params.get("limit", 5) or 5)
        unread_only = action == "read_unread"
        try:
            items = _fetch_messages(limit=limit, unread_only=unread_only)
        except Exception as e:
            return (
                "Could not read iMessages. Give Full Disk Access to Terminal/Python in "
                f"macOS Privacy & Security. Details: {e}"
            )
        if not items:
            return "No iMessages found for that request."

        lines = []
        for msg in items:
            lines.append(
                f"[{msg['time']}] {msg['chat_name']} ({msg['sender']}): {msg['text']}"
            )
        header = "Unread iMessages:" if unread_only else "Latest iMessages:"
        return header + "\n" + "\n".join(lines)

    if action == "monitor_start":
        interval = int(params.get("interval_seconds", 15) or 15)
        result = imessage_monitor_start(interval)
        if player:
            player.write_log(f"[imessage] {result}")
        return result

    if action == "monitor_stop":
        result = imessage_monitor_stop()
        if player:
            player.write_log(f"[imessage] {result}")
        return result

    if action == "monitor_status":
        return imessage_monitor_status()

    return "Unknown iMessage action. Use send, read_latest, read_unread, monitor_start, monitor_stop, or monitor_status."