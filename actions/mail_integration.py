import json
import platform
import re
import subprocess
import threading
import time
from pathlib import Path


_MONITOR_LOCK = threading.Lock()
_MONITOR_STATE = {
    "enabled": False,
    "interval_seconds": 30,
    "seen_ids": [],
}
_CONTACT_CACHE_LOCK = threading.Lock()
_CONTACT_CACHE = {
    "loaded_at": 0.0,
    "entries": set(),
}

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
_PROMO_SUBJECT_PATTERNS = (
    "sale",
    "discount",
    "offer",
    "deal",
    "promo",
    "promotion",
    "newsletter",
    "quote",
    "clearance",
    "black friday",
    "cyber monday",
    "% off",
)
_PROMO_SENDER_PATTERNS = (
    "no-reply",
    "noreply",
    "newsletter",
    "marketing",
    "promo",
    "offers",
    "deal",
    "sale",
    "mailer",
    "servicemail",
)


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _extract_email(value: str) -> str:
    match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", value or "", re.IGNORECASE)
    return (match.group(1) if match else "").strip().lower()


def _load_mail_rules() -> dict:
    cfg = {
        "priority_senders": [],
        "announce_promotional": False,
        "known_contacts_only": False,
        "announce_unknown_senders": True,
    }
    try:
        raw = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        priority = raw.get("mail_priority_senders", cfg["priority_senders"])
        if isinstance(priority, list):
            cfg["priority_senders"] = [str(item).strip() for item in priority if str(item).strip()]
        cfg["announce_promotional"] = bool(raw.get("mail_announce_promotional", cfg["announce_promotional"]))
        cfg["known_contacts_only"] = bool(raw.get("mail_known_contacts_only", cfg["known_contacts_only"]))
        cfg["announce_unknown_senders"] = bool(raw.get("mail_announce_unknown_senders", cfg["announce_unknown_senders"]))
    except Exception:
        pass
    return cfg


def _sender_matches_rule(sender: str, rule: str) -> bool:
    sender_text = (sender or "").strip()
    rule_text = (rule or "").strip()
    if not sender_text or not rule_text:
        return False

    sender_email = _extract_email(sender_text)
    rule_email = _extract_email(rule_text)
    if sender_email and rule_email:
        return sender_email == rule_email

    sender_norm = _normalize_text(sender_text)
    rule_norm = _normalize_text(rule_text)
    return bool(sender_norm and rule_norm and (rule_norm in sender_norm or sender_norm in rule_norm))


def _fetch_contacts() -> set[str]:
    script = r'''
function run() {
  const app = Application("Contacts");
  const people = app.people();
  const rows = [];

  for (let index = 0; index < people.length; index += 1) {
    const person = people[index];
    try {
      const fullName = String(person.name() || "").trim();
      if (fullName) {
        rows.push(fullName);
      }
    } catch (_) {}

    try {
      const emails = person.emails();
      for (let emailIndex = 0; emailIndex < emails.length; emailIndex += 1) {
        try {
          const value = String(emails[emailIndex].value() || "").trim();
          if (value) {
            rows.push(value);
          }
        } catch (_) {}
      }
    } catch (_) {}
  }

  return JSON.stringify(rows);
}
'''
    raw = _run_jxa(script)
    parsed = json.loads(raw or "[]")
    entries = set()
    for item in parsed:
        text = str(item or "").strip()
        if not text:
            continue
        entries.add(_normalize_text(text))
        email = _extract_email(text)
        if email:
            entries.add(email)
    return entries


def _get_cached_contacts() -> set[str]:
    with _CONTACT_CACHE_LOCK:
        age = time.time() - float(_CONTACT_CACHE["loaded_at"])
        if _CONTACT_CACHE["entries"] and age < 600:
            return set(_CONTACT_CACHE["entries"])

    try:
        entries = _fetch_contacts()
    except Exception:
        return set()

    with _CONTACT_CACHE_LOCK:
        _CONTACT_CACHE["loaded_at"] = time.time()
        _CONTACT_CACHE["entries"] = set(entries)
    return set(entries)


def _is_known_contact(sender: str) -> bool:
    sender_text = (sender or "").strip()
    if not sender_text:
        return False

    contacts = _get_cached_contacts()
    if not contacts:
        return False

    sender_norm = _normalize_text(sender_text)
    sender_email = _extract_email(sender_text)
    if sender_email and sender_email in contacts:
        return True
    return bool(sender_norm and sender_norm in contacts)


def _looks_promotional(item: dict) -> bool:
    sender = str(item.get("sender") or "").lower()
    subject = str(item.get("subject") or "").lower()
    snippet = str(item.get("snippet") or "").lower()
    email = _extract_email(sender)
    sender_tokens = [sender, email]
    if any(pattern in token for token in sender_tokens for pattern in _PROMO_SENDER_PATTERNS if token):
        return True
    combined_text = f"{subject} {snippet}"
    return any(pattern in combined_text for pattern in _PROMO_SUBJECT_PATTERNS)


def _should_announce_mail(item: dict) -> tuple[bool, str]:
    rules = _load_mail_rules()
    sender = str(item.get("sender") or "").strip()

    for rule in rules["priority_senders"]:
        if _sender_matches_rule(sender, rule):
            return True, "priority_sender"

    if _is_known_contact(sender):
        return True, "known_contact"

    if _looks_promotional(item) and not rules["announce_promotional"]:
        return False, "promotional"

    if rules["known_contacts_only"]:
        return False, "unknown_sender"

    if rules["announce_unknown_senders"]:
        return True, "unknown_non_promotional"

    return False, "unknown_sender"


def _is_timeout_error(exc: Exception) -> bool:
    return "timed out" in str(exc).lower()


def _run_jxa(script: str, args: list[str] | None = None, timeout_seconds: int = 25) -> str:
    cmd = ["osascript", "-l", "JavaScript", "-e", script]
    if args:
        cmd.append("--")
        cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(timeout_seconds or 25)))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Unknown Mail AppleScript error").strip()
        raise RuntimeError(err)
    return (proc.stdout or "").strip()


def _fetch_mail(
    limit: int = 5,
    unread_only: bool = False,
    include_preview: bool = False,
    scan_limit: int | None = None,
    timeout_seconds: int = 25,
) -> list[dict]:
    limit = max(1, min(int(limit or 5), 50))
    if scan_limit is None:
        if unread_only:
            scan_limit = max(limit * 4, 60)
        else:
            scan_limit = max(limit * 3, 30)
    scan_limit = max(limit, min(int(scan_limit), 500))

    script = r'''
function run(argv) {
  const limit = Math.max(1, Math.min(parseInt(argv[0] || "5", 10), 50));
  const unreadOnly = String(argv[1] || "false") === "true";
  const includePreview = String(argv[2] || "false") === "true";
  const parsedScanLimit = parseInt(argv[3] || "0", 10);
  const defaultScan = unreadOnly
    ? Math.max(limit * (includePreview ? 3 : 4), 60)
    : Math.max(limit * (includePreview ? 2 : 3), 30);
  const scanLimit = Math.max(limit, Math.min(Number.isFinite(parsedScanLimit) && parsedScanLimit > 0 ? parsedScanLimit : defaultScan, 500));
  const app = Application("Mail");
  const inbox = app.inbox();
  const messages = inbox.messages();
  const rows = [];

  for (let index = 0; index < messages.length; index += 1) {
    if (index >= scanLimit) {
      break;
    }
    const message = messages[index];
    let read = false;
    try {
      read = Boolean(message.readStatus());
    } catch (_) {}
    if (unreadOnly && read) {
      continue;
    }

    let received = "";
    try {
      const dateValue = message.dateReceived();
      if (dateValue) {
        received = new Date(dateValue).toISOString();
      }
    } catch (_) {}

    let body = "";
        if (includePreview) {
            try {
                body = String(message.content() || "");
            } catch (_) {}
        }

        rows.push({
      id: Number(message.id()),
      subject: String(message.subject() || ""),
      sender: String(message.sender() || ""),
            read,
      received,
      body,
        });

        if (rows.length >= limit) {
            break;
    }
    }

    rows.sort((left, right) => {
    const leftTime = left.received ? Date.parse(left.received) : 0;
    const rightTime = right.received ? Date.parse(right.received) : 0;
    return rightTime - leftTime;
  });

    return JSON.stringify(rows.slice(0, limit));
}
'''

    raw = _run_jxa(
        script,
                [
                        str(limit),
                        "true" if unread_only else "false",
                        "true" if include_preview else "false",
                        str(scan_limit),
                ],
                timeout_seconds=timeout_seconds,
    )
    parsed = json.loads(raw or "[]")
    items = []
    for row in parsed:
        body = " ".join(str(row.get("body", "")).split())
        items.append(
            {
                "id": int(row.get("id") or 0),
                "subject": str(row.get("subject") or "").strip(),
                "sender": str(row.get("sender") or "Unknown").strip(),
                "read": bool(row.get("read")),
                "received": str(row.get("received") or "").strip(),
                "snippet": body[:220],
            }
        )
    return items


def _notify_mac(title: str, body: str) -> None:
    safe_title = json.dumps(title)
    safe_body = json.dumps(body)
    script = f'Application.currentApplication(); app = Application.currentApplication(); app.includeStandardAdditions = true; app.displayNotification({safe_body}, {{withTitle: {safe_title}}});'
    try:
        _run_jxa(script)
    except Exception:
        pass


def mail_monitor_start(interval_seconds: int = 30) -> str:
    if not _is_mac():
        return "Mail monitor is only available on macOS."

    interval_seconds = max(10, min(int(interval_seconds or 30), 900))

    try:
        existing = _fetch_mail(
            limit=20,
            unread_only=True,
            include_preview=False,
            scan_limit=80,
            timeout_seconds=12,
        )
    except Exception as e:
        if _is_timeout_error(e):
            try:
                existing = _fetch_mail(
                    limit=10,
                    unread_only=True,
                    include_preview=False,
                    scan_limit=30,
                    timeout_seconds=8,
                )
            except Exception as e2:
                return (
                    "Could not start Apple Mail monitor: Mail query timed out. "
                    "Open Mail so it can finish syncing, then try again. "
                    f"Details: {e2}"
                )
        else:
            return (
                "Could not access Apple Mail. macOS may require Automation permission for "
                f"Terminal/Python -> Mail. Details: {e}"
            )

    if existing is None:
        return (
            "Could not start Apple Mail monitor due to an unexpected error. "
            "Please try again."
        )

    seen_ids = [item["id"] for item in existing if item.get("id")]
    with _MONITOR_LOCK:
        _MONITOR_STATE["enabled"] = True
        _MONITOR_STATE["interval_seconds"] = interval_seconds
        _MONITOR_STATE["seen_ids"] = seen_ids[-200:]
    return f"Mail monitor enabled (every {interval_seconds}s)."


def mail_monitor_stop() -> str:
    with _MONITOR_LOCK:
        _MONITOR_STATE["enabled"] = False
    return "Mail monitor disabled."


def mail_monitor_status() -> str:
    with _MONITOR_LOCK:
        enabled = _MONITOR_STATE["enabled"]
        interval_seconds = _MONITOR_STATE["interval_seconds"]
        seen_count = len(_MONITOR_STATE["seen_ids"])
    state = "enabled" if enabled else "disabled"
    return f"Mail monitor is {state}. Interval: {interval_seconds}s. Seen mail IDs cached: {seen_count}."


def get_mail_monitor_interval() -> int:
    with _MONITOR_LOCK:
        return int(_MONITOR_STATE["interval_seconds"])


def poll_mail_alerts() -> list[dict]:
    with _MONITOR_LOCK:
        enabled = bool(_MONITOR_STATE["enabled"])
        seen_ids = set(int(v) for v in _MONITOR_STATE["seen_ids"] if v)

    if not enabled or not _is_mac():
        return []

    try:
        unread = _fetch_mail(
            limit=15,
            unread_only=True,
            include_preview=False,
            scan_limit=60,
            timeout_seconds=10,
        )
    except Exception:
        return []

    if not unread:
        return []

    fresh = [item for item in unread if item.get("id") and int(item["id"]) not in seen_ids]
    if not fresh:
        return []

    fresh_sorted = list(reversed(fresh))
    updated_seen = list(seen_ids)
    for item in unread:
        mail_id = int(item.get("id") or 0)
        if mail_id:
            updated_seen.append(mail_id)
    deduped_seen = []
    seen_tracker = set()
    for mail_id in updated_seen:
        if mail_id in seen_tracker:
            continue
        seen_tracker.add(mail_id)
        deduped_seen.append(mail_id)

    with _MONITOR_LOCK:
        _MONITOR_STATE["seen_ids"] = deduped_seen[-200:]

    announceable = []
    for item in fresh_sorted:
        allowed, _reason = _should_announce_mail(item)
        if not allowed:
            continue
        announceable.append(item)
        sender = item.get("sender") or "Unknown sender"
        subject = item.get("subject") or "No subject"
        _notify_mac(title=f"New mail from {sender}", body=subject[:140])

    return announceable


def mail_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = str(params.get("action", "") or "read_latest").strip().lower()

    if action in {"read_latest", "read_unread"}:
        limit = int(params.get("limit", 5) or 5)
        unread_only = action == "read_unread"
        try:
            items = _fetch_mail(
                limit=limit,
                unread_only=unread_only,
                include_preview=True,
                scan_limit=max(limit * 3, 40),
                timeout_seconds=25,
            )
        except Exception as e:
            if _is_timeout_error(e):
                return (
                    "Could not read Apple Mail in time (query timed out). "
                    "Try a smaller limit or reopen Mail and retry. "
                    f"Details: {e}"
                )
            return (
                "Could not read Apple Mail. macOS may require Automation permission for "
                f"Terminal/Python -> Mail. Details: {e}"
            )

        if not items:
            return "No mail found for that request."

        lines = []
        for item in items:
            sender = item.get("sender") or "Unknown"
            subject = item.get("subject") or "No subject"
            received = item.get("received") or "unknown time"
            snippet = item.get("snippet") or ""
            line = f"[{received}] {sender} | Subject: {subject}"
            if snippet:
                line += f" | Preview: {snippet}"
            lines.append(line)
        header = "Unread mail:" if unread_only else "Latest mail:"
        return header + "\n" + "\n".join(lines)

    if action == "monitor_start":
        interval = int(params.get("interval_seconds", 30) or 30)
        result = mail_monitor_start(interval)
        if player:
            player.write_log(f"[mail] {result}")
        return result

    if action == "monitor_stop":
        result = mail_monitor_stop()
        if player:
            player.write_log(f"[mail] {result}")
        return result

    if action == "monitor_status":
        return mail_monitor_status()

    return "Unknown mail action. Use read_latest, read_unread, monitor_start, monitor_stop, or monitor_status."