from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DEFAULT_CREDENTIALS_PATH = BASE_DIR / "config" / "google_calendar_credentials.json"
DEFAULT_TOKEN_PATH = BASE_DIR / "config" / "google_calendar_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _load_runtime_config() -> dict:
    cfg = {
        "enabled": True,
        "credentials_path": str(DEFAULT_CREDENTIALS_PATH),
        "token_path": str(DEFAULT_TOKEN_PATH),
        "calendar_id": "primary",
        "timezone": "local",
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cfg["enabled"] = bool(raw.get("google_calendar_enabled", cfg["enabled"]))
        cfg["credentials_path"] = str(raw.get("google_calendar_credentials_path", cfg["credentials_path"]) or cfg["credentials_path"]).strip()
        cfg["token_path"] = str(raw.get("google_calendar_token_path", cfg["token_path"]) or cfg["token_path"]).strip()
        cfg["calendar_id"] = str(raw.get("google_calendar_id", cfg["calendar_id"]) or cfg["calendar_id"]).strip()
        cfg["timezone"] = str(raw.get("google_calendar_timezone", cfg["timezone"]) or cfg["timezone"]).strip()
    except Exception:
        pass
    return cfg


def _paths() -> tuple[Path, Path, dict]:
    cfg = _load_runtime_config()
    credentials_path = Path(cfg["credentials_path"]).expanduser()
    token_path = Path(cfg["token_path"]).expanduser()
    return credentials_path, token_path, cfg


def _deps_ready() -> tuple[bool, str]:
    try:
        import google.auth.transport.requests  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def _friendly_setup_instructions(credentials_path: Path) -> str:
    return (
        "Google Calendar is not ready yet. "
        "Create a Google Cloud OAuth Desktop credential, download the JSON file, "
        f"and place it at: {credentials_path}. Then run the calendar setup action again."
    )


def _is_calendar_api_disabled_error(error: Exception | BaseException) -> bool:
    message = str(error).lower()
    return any(token in message for token in (
        "access_not_configured",
        "calendar api has not been used in project",
        "google calendar api has not been used in project",
        "api has not been used in project",
        "disabled in the google cloud console",
        "the api is disabled",
    ))


def _calendar_api_disabled_message() -> str:
    return (
        "Google Calendar API access is disabled for the Google Cloud project behind this credential. "
        "Open Google Cloud Console, select the same project used to create the OAuth client, enable the Google Calendar API, "
        "then wait a minute or two for propagation and run calendar setup again."
    )


def _build_service(force_reauth: bool = False):
    deps_ok, dep_err = _deps_ready()
    if not deps_ok:
        raise RuntimeError(
            "Google Calendar dependencies are missing. Install: "
            "google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2"
            f" ({dep_err})"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path, token_path, cfg = _paths()
    if not credentials_path.exists():
        raise FileNotFoundError(_friendly_setup_instructions(credentials_path))

    creds = None
    if token_path.exists() and not force_reauth:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    if not creds or not creds.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            if _is_calendar_api_disabled_error(e):
                raise RuntimeError(_calendar_api_disabled_message()) from e
            raise

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return service, cfg, credentials_path, token_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _end_from_start(start_dt: datetime, duration_minutes: int) -> datetime:
    return start_dt + timedelta(minutes=max(1, int(duration_minutes or 60)))


def _parse_event_datetime(date_str: str, time_str: str) -> datetime:
    raw = f"{(date_str or '').strip()} {(time_str or '').strip()}".strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("Please use date=YYYY-MM-DD and time=HH:MM.")


def _fmt_event(item: dict) -> str:
    start = item.get("start", {}) or {}
    start_raw = start.get("dateTime") or start.get("date") or "unscheduled"
    summary = str(item.get("summary") or "Untitled event").strip()
    location = str(item.get("location") or "").strip()
    parts = [f"- {summary} @ {start_raw}"]
    if location:
        parts.append(f" ({location})")
    return "".join(parts)


def _status_text(service, cfg: dict, credentials_path: Path, token_path: Path) -> str:
    calendar_id = cfg.get("calendar_id", "primary") or "primary"
    try:
        cal = service.calendars().get(calendarId=calendar_id).execute()
        summary = str(cal.get("summary") or calendar_id)
        zone = str(cal.get("timeZone") or cfg.get("timezone") or "local")
        return (
            "Google Calendar connected. "
            f"Calendar: {summary}. Time zone: {zone}. "
            f"Credentials: {credentials_path.name}. Token: {token_path.name}."
        )
    except Exception as e:
        if _is_calendar_api_disabled_error(e):
            return _calendar_api_disabled_message()
        return f"Google Calendar authentication exists, but calendar lookup failed: {e}"


def google_calendar(parameters: dict, response=None, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "status") or "status").strip().lower()

    credentials_path, token_path, cfg = _paths()
    if not cfg.get("enabled", True):
        return "Google Calendar integration is disabled in config."

    if action == "help":
        return _friendly_setup_instructions(credentials_path)

    if action == "open":
        webbrowser.open("https://calendar.google.com")
        return "Opened Google Calendar in your browser."

    force_reauth = action in {"setup", "reauth"}
    try:
        service, cfg, credentials_path, token_path = _build_service(force_reauth=force_reauth)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        if _is_calendar_api_disabled_error(e):
            return _calendar_api_disabled_message()
        return f"Google Calendar setup failed: {e}"

    calendar_id = str(parameters.get("calendar_id", cfg.get("calendar_id", "primary")) or "primary").strip()

    if action in {"setup", "reauth", "status"}:
        return _status_text(service, {**cfg, "calendar_id": calendar_id}, credentials_path, token_path)

    if action in {"today", "upcoming", "list"}:
        max_results = max(1, min(int(parameters.get("max_results", 8) or 8), 25))
        time_min = _now_iso()
        kwargs = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_results,
        }
        if action == "today":
            start_local = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
            kwargs["timeMin"] = start_local.astimezone(timezone.utc).isoformat()
            kwargs["timeMax"] = end_local.astimezone(timezone.utc).isoformat()
        try:
            items = service.events().list(**kwargs).execute().get("items", [])
        except Exception as e:
            if _is_calendar_api_disabled_error(e):
                return _calendar_api_disabled_message()
            raise
        if not items:
            return "No calendar events found for that window."
        label = "Today's events" if action == "today" else "Upcoming calendar events"
        return label + ":\n" + "\n".join(_fmt_event(item) for item in items)

    if action in {"create", "add"}:
        title = str(parameters.get("title", "") or "").strip()
        date_str = str(parameters.get("date", "") or "").strip()
        time_str = str(parameters.get("time", "") or "").strip()
        duration_minutes = int(parameters.get("duration_minutes", 60) or 60)
        location = str(parameters.get("location", "") or "").strip()
        description = str(parameters.get("description", "") or "").strip()
        if not title or not date_str or not time_str:
            return "To create a Google Calendar event I need title, date, and time."

        try:
            start_dt = _parse_event_datetime(date_str, time_str)
        except ValueError as e:
            return str(e)
        end_dt = _end_from_start(start_dt, duration_minutes)
        event = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat()},
            "end": {"dateTime": end_dt.isoformat()},
        }
        if location:
            event["location"] = location
        if description:
            event["description"] = description
        try:
            created = service.events().insert(calendarId=calendar_id, body=event).execute()
        except Exception as e:
            if _is_calendar_api_disabled_error(e):
                return _calendar_api_disabled_message()
            raise
        created_start = ((created.get("start") or {}).get("dateTime") or date_str)
        return f"Google Calendar event created: {title} at {created_start}."

    if action in {"delete", "remove"}:
        event_id = str(parameters.get("event_id", "") or "").strip()
        if not event_id:
            return "To delete a Google Calendar event I need event_id."
        try:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            if _is_calendar_api_disabled_error(e):
                return _calendar_api_disabled_message()
            raise
        return f"Deleted Google Calendar event {event_id}."

    return "Unknown Google Calendar action. Use: help, setup, status, open, today, upcoming, create, or delete."