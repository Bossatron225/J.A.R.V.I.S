"""Standing goals — objectives Jarvis owns over time rather than one-shot requests.

Everything else Jarvis does is request-scoped: you ask, he acts, it ends. A
goal is stated once ("find me a flat under €1800 in Dublin 8") and pursued on
its own cadence until you close it.

Three things make that safe enough to leave running unattended:

* Goals are READ-ONLY by default. A goal may look things up; it may not send,
  buy, book, or change anything unless `allow_actions` is set explicitly, and
  even then consequential steps go through the approval queue.
* Every goal has hard caps — a minimum interval and a daily check budget — so
  an autonomous loop cannot quietly burn API spend.
* Anything surfaced is remembered, so the same listing is never shown twice,
  and anything you dismiss is remembered as a rejection so the goal sharpens
  instead of repeating itself.
"""
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
GOALS_PATH = DATA_DIR / "goals.json"

# Floor on how often a goal may run, regardless of what it asks for. Guards
# against a goal configured (or mis-planned) into a tight expensive loop.
MIN_INTERVAL_HOURS = 1.0
DEFAULT_INTERVAL_HOURS = 6.0

# Per-goal daily ceiling on checks — the cost guard for unattended running.
DEFAULT_DAILY_CHECK_BUDGET = 8

MAX_GOALS = 25
MAX_REMEMBERED_ITEMS = 200

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def item_key(text: str) -> str:
    """Stable identity for a surfaced item, so the same result is not shown
    twice across checks even if wording shifts slightly."""
    normalized = " ".join(str(text or "").lower().split())[:300]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _load() -> dict:
    if not GOALS_PATH.exists():
        return {"goals": []}
    try:
        data = json.loads(GOALS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("goals"), list):
            return data
    except Exception:
        pass
    return {"goals": []}


def _save(store: dict) -> None:
    try:
        GOALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOALS_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def list_goals(include_closed: bool = False) -> list[dict]:
    goals = _load().get("goals", [])
    if include_closed:
        return goals
    return [g for g in goals if g.get("status") != "closed"]


def get_goal(goal_id: str) -> dict | None:
    for goal in _load().get("goals", []):
        if goal.get("id") == goal_id:
            return goal
    return None


def add_goal(objective: str, criteria: str = "", interval_hours: float = DEFAULT_INTERVAL_HOURS,
             allow_actions: bool = False, daily_budget: int = DEFAULT_DAILY_CHECK_BUDGET) -> dict:
    objective = (objective or "").strip()
    if not objective:
        raise ValueError("a goal needs an objective")

    with _lock:
        store = _load()
        active = [g for g in store["goals"] if g.get("status") != "closed"]
        if len(active) >= MAX_GOALS:
            raise ValueError(f"too many active goals (max {MAX_GOALS}) — close one first")

        goal = {
            "id": uuid.uuid4().hex[:8],
            "objective": objective,
            "criteria": (criteria or "").strip(),
            # Clamped, never taken at face value.
            "interval_hours": max(MIN_INTERVAL_HOURS, float(interval_hours or DEFAULT_INTERVAL_HOURS)),
            "allow_actions": bool(allow_actions),
            "daily_budget": max(1, int(daily_budget or DEFAULT_DAILY_CHECK_BUDGET)),
            "status": "active",
            "created": _iso(),
            "last_checked": None,
            "checks_today": 0,
            "checks_day": "",
            "surfaced": [],
            "rejected": [],
            "findings": 0,
        }
        store["goals"].append(goal)
        _save(store)
        return goal


def update_goal(goal_id: str, **changes) -> dict | None:
    with _lock:
        store = _load()
        for goal in store["goals"]:
            if goal.get("id") == goal_id:
                goal.update(changes)
                _save(store)
                return goal
    return None


def set_status(goal_id: str, status: str) -> dict | None:
    if status not in {"active", "paused", "closed"}:
        raise ValueError("status must be active, paused or closed")
    return update_goal(goal_id, status=status)


def is_due(goal: dict, now: datetime | None = None) -> bool:
    """Only active goals within budget and past their interval are due."""
    if goal.get("status") != "active":
        return False
    now = now or _now()

    if _budget_remaining(goal, now) <= 0:
        return False

    last = _parse(goal.get("last_checked") or "")
    if last is None:
        return True
    interval = max(MIN_INTERVAL_HOURS, float(goal.get("interval_hours", DEFAULT_INTERVAL_HOURS)))
    return now - last >= timedelta(hours=interval)


def _budget_remaining(goal: dict, now: datetime | None = None) -> int:
    now = now or _now()
    today = now.strftime("%Y-%m-%d")
    if goal.get("checks_day") != today:
        return int(goal.get("daily_budget", DEFAULT_DAILY_CHECK_BUDGET))
    used = int(goal.get("checks_today", 0))
    return max(0, int(goal.get("daily_budget", DEFAULT_DAILY_CHECK_BUDGET)) - used)


def record_check(goal_id: str, now: datetime | None = None) -> None:
    now = now or _now()
    today = now.strftime("%Y-%m-%d")
    with _lock:
        store = _load()
        for goal in store["goals"]:
            if goal.get("id") != goal_id:
                continue
            if goal.get("checks_day") != today:
                goal["checks_day"] = today
                goal["checks_today"] = 0
            goal["checks_today"] = int(goal.get("checks_today", 0)) + 1
            goal["last_checked"] = _iso(now)
            _save(store)
            return


def already_seen(goal: dict, text: str) -> bool:
    key = item_key(text)
    return key in (goal.get("surfaced") or []) or key in (goal.get("rejected") or [])


def record_surfaced(goal_id: str, texts: list[str]) -> None:
    with _lock:
        store = _load()
        for goal in store["goals"]:
            if goal.get("id") != goal_id:
                continue
            seen = list(goal.get("surfaced") or [])
            seen.extend(item_key(t) for t in texts)
            goal["surfaced"] = seen[-MAX_REMEMBERED_ITEMS:]
            goal["findings"] = int(goal.get("findings", 0)) + len(texts)
            _save(store)
            return


def record_rejection(goal_id: str, text: str) -> None:
    """Remember something the user dismissed so the goal sharpens rather than
    surfacing the same unwanted result again."""
    with _lock:
        store = _load()
        for goal in store["goals"]:
            if goal.get("id") != goal_id:
                continue
            rejected = list(goal.get("rejected") or [])
            rejected.append(item_key(text))
            goal["rejected"] = rejected[-MAX_REMEMBERED_ITEMS:]
            _save(store)
            return


def format_goals(goals: list[dict]) -> str:
    if not goals:
        return "You have no standing goals, sir."
    lines = [f"{len(goals)} standing goal(s):"]
    for goal in goals:
        last = goal.get("last_checked") or "never"
        mode = "can act" if goal.get("allow_actions") else "read-only"
        lines.append(
            f"  [{goal['id']}] {goal['objective']} "
            f"({goal.get('status')}, every {goal.get('interval_hours')}h, {mode}, "
            f"{goal.get('findings', 0)} finding(s), last checked {last})"
        )
    return "\n".join(lines)


def goals_tool(parameters: dict | None = None, response=None, player=None,
               session_memory=None, speak=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "list") or "list").strip().lower()

    if action in {"add", "create", "new"}:
        try:
            goal = add_goal(
                objective=str(p.get("objective", "") or ""),
                criteria=str(p.get("criteria", "") or ""),
                interval_hours=float(p.get("interval_hours", DEFAULT_INTERVAL_HOURS) or DEFAULT_INTERVAL_HOURS),
                allow_actions=bool(p.get("allow_actions", False)),
            )
        except ValueError as exc:
            return f"Could not create that goal, sir: {exc}"
        mode = "It may take actions." if goal["allow_actions"] else "It will only look things up, not act."
        return (
            f"Goal [{goal['id']}] set, sir: {goal['objective']}. "
            f"I'll check roughly every {goal['interval_hours']:.0f} hours. {mode}"
        )

    if action in {"pause", "resume", "close"}:
        goal_id = str(p.get("goal_id", "") or "").strip()
        if not goal_id:
            return "Which goal, sir? Give me its id."
        status = {"pause": "paused", "resume": "active", "close": "closed"}[action]
        goal = set_status(goal_id, status)
        return f"Goal [{goal_id}] {status}." if goal else f"No goal with id {goal_id}, sir."

    if action in {"reject", "dismiss"}:
        goal_id = str(p.get("goal_id", "") or "").strip()
        text = str(p.get("text", "") or "").strip()
        if not goal_id or not text:
            return "I need the goal id and what to dismiss, sir."
        record_rejection(goal_id, text)
        return "Noted — I won't raise that one again, sir."

    return format_goals(list_goals(include_closed=bool(p.get("include_closed", False))))
