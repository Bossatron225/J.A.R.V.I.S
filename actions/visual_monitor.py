from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from actions.browser_control import capture_browser_tab
from actions.screen_processor import _capture_targeted_visual


@dataclass
class VisualTarget:
    target_id: str
    target_type: str
    label: str
    browser: str = ""
    target: str = ""
    index: int | None = None
    window_title: str = ""
    app_name: str = ""
    window_id: int | None = None
    interval_seconds: float = 3.5
    enabled: bool = True


class VisualMonitorRegistry:
    def __init__(self, config_path: Path):
        self._config_path = Path(config_path)
        self._targets: dict[str, VisualTarget] = {}
        self._last_hash: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        # target_ids created by add_all() — the only ones resync_all() touches,
        # so it never adds/drops targets the user registered individually.
        self._auto_group: set[str] = set()

    @staticmethod
    def _normalize_label(value: str) -> str:
        return " ".join((value or "").strip().split())

    @staticmethod
    def _macos_app_inventory() -> list[dict]:
        """
        Every visible window of every app, front-to-back, each window carrying
        a stable CGWindowID — not just window titles, which collide whenever
        an app has multiple similarly-named windows (e.g. two Terminal tabs
        both called "bash"). The window ID is what lets individual windows be
        targeted and captured reliably, even when occluded by other windows.
        """
        if platform.system() != "Darwin":
            return []
        try:
            import Quartz
            wlist = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            ) or []
        except Exception:
            return VisualMonitorRegistry._macos_app_inventory_applescript()

        items: list[dict] = []
        by_name: dict[str, dict] = {}
        for w in wlist:
            try:
                if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                    continue  # skip menu bar / dock / overlay layers, keep normal app windows
                owner = str(w.get("kCGWindowOwnerName") or "").strip()
                wid = w.get("kCGWindowNumber")
                if not owner or not wid:
                    continue
                wname = str(w.get("kCGWindowName") or "").strip() or "(untitled)"
                obj = by_name.get(owner)
                if not obj:
                    # CGWindowListCopyWindowInfo returns windows front-to-back,
                    # so the first window we see overall is the frontmost app.
                    obj = {"app_name": owner, "frontmost": not items, "windows": []}
                    by_name[owner] = obj
                    items.append(obj)
                obj["windows"].append({"title": wname, "window_id": int(wid)})
            except Exception:
                continue
        return items

    @staticmethod
    def _macos_app_inventory_applescript() -> list[dict]:
        """Fallback when Quartz/PyObjC is unavailable — no window IDs, titles only."""
        script = r'''
set outputLines to {}
tell application "System Events"
    repeat with p in application processes
        try
            if background only of p is false then
                set pName to name of p
                set isFront to frontmost of p
                set end of outputLines to "APP" & tab & pName & tab & (isFront as text)
                repeat with w in windows of p
                    try
                        set wName to name of w
                        if wName is missing value or (wName as text) is "" then set wName to "(untitled)"
                        set end of outputLines to "WIN" & tab & pName & tab & (wName as text)
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
if (count of outputLines) is 0 then return ""
set AppleScript's text item delimiters to linefeed
return outputLines as text
'''
        try:
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        except Exception:
            return []
        raw = (proc.stdout or proc.stderr or "").strip()
        if not raw:
            return []

        items: list[dict] = []
        by_name: dict[str, dict] = {}
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            rec = parts[0]
            if rec == "APP" and len(parts) >= 3:
                name = parts[1].strip()
                if not name:
                    continue
                obj = by_name.get(name)
                if not obj:
                    obj = {"app_name": name, "frontmost": parts[2].strip().lower() == "true", "windows": []}
                    by_name[name] = obj
                    items.append(obj)
                else:
                    obj["frontmost"] = parts[2].strip().lower() == "true"
            elif rec == "WIN" and len(parts) >= 3:
                name = parts[1].strip()
                if not name:
                    continue
                obj = by_name.get(name)
                if not obj:
                    obj = {"app_name": name, "frontmost": False, "windows": []}
                    by_name[name] = obj
                    items.append(obj)
                wname = parts[2].strip()
                if wname and wname not in [w.get("title") for w in obj["windows"]]:
                    obj["windows"].append({"title": wname, "window_id": None})
        return items

    def add_target(self, params: dict) -> VisualTarget:
        target_type = str(params.get("target_type", params.get("angle", "screen")) or "screen").strip().lower()
        browser = str(params.get("browser", "") or "").strip()
        target = str(params.get("target", "") or "").strip()
        window_title = str(params.get("window_title", "") or "").strip()
        app_name = str(params.get("app_name", "") or "").strip()
        app_index = _coerce_index(params.get("app_index"))
        window_id = _coerce_index(params.get("window_id"))
        label = str(params.get("label", "") or "").strip()
        target_id = str(params.get("target_id", "") or "").strip()

        if target_type in {"app", "application", "window"} and app_index and not app_name:
            inv = self._macos_app_inventory()
            idx = app_index - 1
            if 0 <= idx < len(inv):
                chosen = inv[idx]
                app_name = str(chosen.get("app_name", "") or "").strip()
                if target_type == "window" and not window_title:
                    windows = chosen.get("windows") or []
                    if windows:
                        window_title = str(windows[0].get("title") or "")
                        window_id = windows[0].get("window_id")
        if not target_id:
            basis = [target_type, browser, target, window_title, app_name, index_or_blank(window_id)]
            slug = "|".join(part for part in basis if part)
            target_id = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:12]

        if not label:
            if target_type in {"tab", "browser", "browser_tab", "browser-tab"}:
                label = f"tab:{browser or 'active'}:{target or index_or_blank(params.get('index'))}"
            elif target_type in {"window", "app", "application"}:
                label = self._normalize_label(window_title or app_name or target or "window")
            else:
                label = target_type

        interval = float(params.get("interval_seconds", 3.5) or 3.5)
        target_obj = VisualTarget(
            target_id=target_id,
            target_type=target_type,
            label=label,
            browser=browser,
            target=target,
            index=_coerce_index(params.get("index")),
            window_title=window_title,
            app_name=app_name,
            window_id=window_id,
            interval_seconds=max(1.0, min(interval, 60.0)),
            enabled=bool(params.get("enabled", True)),
        )
        with self._lock:
            self._targets[target_obj.target_id] = target_obj
        return target_obj

    def remove_target(self, target_id: str) -> bool:
        with self._lock:
            removed = self._targets.pop(target_id, None) is not None
            self._last_hash.pop(target_id, None)
            self._last_seen.pop(target_id, None)
        return removed

    def list_targets(self) -> list[dict]:
        with self._lock:
            return [asdict(t) for t in self._targets.values()]

    def clear(self) -> None:
        with self._lock:
            self._targets.clear()
            self._last_hash.clear()
            self._last_seen.clear()

    def _capture(self, target: VisualTarget) -> tuple[bytes, str, str]:
        return _capture_targeted_visual(
            target.target_type,
            browser=target.browser,
            target=target.target,
            index=target.index,
            window_title=target.window_title,
            app_name=target.app_name,
            window_id=target.window_id,
        )

    def poll_once(self) -> list[dict]:
        now = time.time()
        events: list[dict] = []
        with self._lock:
            targets = list(self._targets.values())
        for target in targets:
            if not target.enabled:
                continue
            last_seen = self._last_seen.get(target.target_id, 0.0)
            if (now - last_seen) < target.interval_seconds:
                continue
            try:
                image_bytes, mime_type, label = self._capture(target)
                digest = hashlib.sha256(image_bytes).hexdigest()
                changed = digest != self._last_hash.get(target.target_id)
                self._last_hash[target.target_id] = digest
                self._last_seen[target.target_id] = now
                events.append({
                    "target_id": target.target_id,
                    "label": target.label,
                    "target_type": target.target_type,
                    "capture_label": label,
                    "changed": changed,
                    "image_bytes": image_bytes,
                    "mime_type": mime_type,
                })
            except Exception as e:
                self._last_seen[target.target_id] = now
                events.append({
                    "target_id": target.target_id,
                    "label": target.label,
                    "target_type": target.target_type,
                    "error": str(e),
                })
        return events


def _coerce_index(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def index_or_blank(value) -> str:
    try:
        return str(int(value))
    except Exception:
        return ""