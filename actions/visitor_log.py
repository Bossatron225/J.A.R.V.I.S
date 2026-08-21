import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None


def _get_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


BASE_DIR         = _get_base_dir()
DATA_DIR         = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
LOG_PATH         = DATA_DIR / "visitor_log.jsonl"
CLUSTERS_PATH    = DATA_DIR / "visitor_clusters.json"
SNAPSHOTS_DIR    = DATA_DIR / "visitor_snapshots"
WATCH_STATE_PATH = DATA_DIR / "visitor_watch_state.json"

MAX_LOG_ENTRIES     = 2000
MAX_SNAPSHOT_FILES  = 500
DEFAULT_CLUSTER_WINDOW_DAYS = 30

_lock = threading.Lock()

# Runtime start/stop state for the "Nanny-cam protocol" — checked by main.py's
# continuous monitor thread every cycle (in-memory, so that read is cheap),
# and set here so it's reachable identically whether this module's
# visitor_log() is called directly on the Mac or delegated from the VPS
# dashboard via local_worker.py — both paths call this exact function.
_watch_state_lock = threading.Lock()
_watch_active: bool | None = None  # lazily loaded from WATCH_STATE_PATH on first check


def is_watch_active() -> bool:
    global _watch_active
    with _watch_state_lock:
        if _watch_active is None:
            _watch_active = _load_watch_state()
        return _watch_active


def set_watch_active(active: bool) -> None:
    global _watch_active
    with _watch_state_lock:
        _watch_active = bool(active)
        _save_watch_state(_watch_active)


def _load_watch_state() -> bool:
    try:
        if WATCH_STATE_PATH.exists():
            data = json.loads(WATCH_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "active" in data:
                return bool(data["active"])
    except Exception:
        pass
    return True  # default: engaged, matching visitor_watch_enabled's own default


def _save_watch_state(active: bool) -> None:
    try:
        WATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATCH_STATE_PATH.write_text(json.dumps({"active": active}), encoding="utf-8")
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_all_entries() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    entries: list[dict] = []
    with _lock:
        try:
            with LOG_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(entry, dict) and entry.get("id"):
                        entries.append(entry)
        except Exception as e:
            print(f"[VisitorLog] ⚠️ Read error: {e}")
            return []
    return entries


def _write_all_entries(entries: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_entry(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _trim_log_if_needed() -> None:
    entries = _read_all_entries()
    if len(entries) <= MAX_LOG_ENTRIES:
        return
    entries.sort(key=lambda e: str(e.get("ts", "")))
    _write_all_entries(entries[-MAX_LOG_ENTRIES:])


def _load_clusters() -> dict:
    if not CLUSTERS_PATH.exists():
        return {}
    try:
        data = json.loads(CLUSTERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_clusters(clusters: dict) -> None:
    CLUSTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLUSTERS_PATH.write_text(json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8")


def _cosine(a, b) -> float:
    if a is None or b is None or np is None:
        return 0.0
    a = np.asarray(a, dtype=float).reshape(-1)
    b = np.asarray(b, dtype=float).reshape(-1)
    if a.shape != b.shape:
        return 0.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _match_cluster(embedding, clusters: dict, window_days: int, threshold: float) -> str | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    best_id = None
    best_score = -1.0
    for visitor_id, cluster in clusters.items():
        last_seen = str(cluster.get("last_seen", ""))
        try:
            last_seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except Exception:
            continue
        if last_seen_dt < cutoff:
            continue
        score = _cosine(embedding, cluster.get("representative_embedding"))
        if score > best_score:
            best_score = score
            best_id = visitor_id
    if best_id is not None and best_score >= threshold:
        return best_id
    return None


def _save_snapshot(frame, visitor_id: str) -> str | None:
    if frame is None or cv2 is None:
        return None
    try:
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SNAPSHOTS_DIR / f"{stamp}_{visitor_id}.jpg"
        cv2.imwrite(str(path), frame)
        return str(path)
    except Exception:
        return None


def _prune_snapshots_if_needed() -> None:
    if not SNAPSHOTS_DIR.exists():
        return
    files = sorted(SNAPSHOTS_DIR.glob("*.jpg"), key=lambda p: p.name)
    excess = len(files) - MAX_SNAPSHOT_FILES
    for path in files[:max(0, excess)]:
        try:
            path.unlink()
        except OSError:
            pass


def record_unknown_sighting(
    embedding,
    frame=None,
    score: float = 0.0,
    camera_index: int = 0,
    cluster_window_days: int = DEFAULT_CLUSTER_WINDOW_DAYS,
    match_threshold: float = 0.363,
) -> dict:
    """Cluster-dedupe an unrecognized face against recent unknown-visitor clusters,
    append a sighting entry, and save a snapshot if a frame was captured."""
    clusters = _load_clusters()
    visitor_id = _match_cluster(embedding, clusters, cluster_window_days, match_threshold)
    now = _now_iso()

    if visitor_id is None:
        visitor_id = uuid.uuid4().hex[:8]
        clusters[visitor_id] = {
            "representative_embedding": (embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)),
            "first_seen": now,
            "last_seen": now,
            "sighting_count": 1,
        }
    else:
        clusters[visitor_id]["last_seen"] = now
        clusters[visitor_id]["sighting_count"] = int(clusters[visitor_id].get("sighting_count", 1)) + 1
    _save_clusters(clusters)

    snapshot_path = _save_snapshot(frame, visitor_id)
    entry = {
        "id": uuid.uuid4().hex,
        "ts": now,
        "visitor_id": visitor_id,
        "sighting_count_at_time": clusters[visitor_id]["sighting_count"],
        "score": round(float(score), 4),
        "snapshot_path": snapshot_path,
        "camera_index": camera_index,
    }
    _append_entry(entry)
    _trim_log_if_needed()
    _prune_snapshots_if_needed()
    return entry


def list_recent_sightings(limit: int = 10, since: str | None = None) -> list[dict]:
    entries = _read_all_entries()
    if since:
        entries = [e for e in entries if str(e.get("ts", "")) > since]
    entries.sort(key=lambda e: str(e.get("ts", "")))
    return entries[-limit:] if limit else entries


def _format_sightings_summary(entries: list[dict]) -> str:
    if not entries:
        return "No unrecognized visitors have been logged, sir."

    by_visitor: dict[str, dict] = {}
    for entry in entries:
        vid = str(entry.get("visitor_id", "unknown"))
        by_visitor[vid] = entry  # keep the most recent entry per visitor (entries are time-sorted)

    parts = [f"{len(entries)} unrecognized visitor sighting{'s' if len(entries) != 1 else ''} logged recently:"]
    saved = 0
    for vid, entry in by_visitor.items():
        ts = str(entry.get("ts", ""))
        count = entry.get("sighting_count_at_time", 1)
        # Surface the snapshot. Without this the tool reported only IDs and
        # timestamps, so Jarvis had no idea stills existed and told the user
        # it had no camera captures at all — while 10 JPEGs sat on disk.
        snapshot = str(entry.get("snapshot_path") or "").strip()
        line = f"- Visitor {vid}: last seen {ts}, seen {count} time{'s' if count != 1 else ''} total."
        if snapshot:
            saved += 1
            line += f" Photo saved: {snapshot}"
        parts.append(line)

    if saved:
        parts.append(
            f"{saved} still image{'s are' if saved != 1 else ' is'} on disk in {SNAPSHOTS_DIR} "
            "— these are timestamped photographs, not video (no video is ever recorded)."
        )
    return "\n".join(parts)


def list_snapshots(limit: int = 10) -> list[str]:
    """Paths of the most recent saved visitor stills, newest last."""
    if not SNAPSHOTS_DIR.exists():
        return []
    files = sorted(SNAPSHOTS_DIR.glob("*.jpg"), key=lambda p: p.name)
    return [str(p) for p in (files[-limit:] if limit else files)]


def _format_snapshots(paths: list[str]) -> str:
    if not paths:
        return (
            "No visitor photographs have been saved yet, sir. Stills are only captured "
            "when an unrecognized face is detected."
        )
    lines = [
        f"{len(paths)} visitor photograph{'s' if len(paths) != 1 else ''} on disk "
        "(timestamped stills — no video is recorded):"
    ]
    lines.extend(f"  {p}" for p in paths)
    lines.append(f"Folder: {SNAPSHOTS_DIR}")
    return "\n".join(lines)


def visitor_log(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p = parameters or {}
    action = str(p.get("action", "recent") or "recent").strip().lower()
    limit = int(p.get("limit", 10) or 10)

    if action in {"start_watch", "start", "engage", "activate", "resume"}:
        set_watch_active(True)
        return "Nanny-cam protocol engaged, sir. I'm watching the camera and will let you know if anyone unrecognized shows up."

    if action in {"stop_watch", "stop", "disengage", "deactivate", "pause"}:
        set_watch_active(False)
        return "Nanny-cam protocol disengaged, sir. I've stopped watching the camera."

    if action in {"watch_status", "engaged_status"}:
        state = "engaged" if is_watch_active() else "disengaged"
        return f"Nanny-cam protocol is currently {state}, sir."

    if action in {"snapshots", "photos", "footage", "images", "pictures"}:
        return _format_snapshots(list_snapshots(limit=limit))

    if action in {"recent", "status", "list"}:
        entries = list_recent_sightings(limit=limit)
        return _format_sightings_summary(entries)

    return f"Unknown visitor_log action: {action}"
