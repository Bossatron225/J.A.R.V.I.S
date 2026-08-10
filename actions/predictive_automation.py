"""
Predictive automation daemon.

Learns recurring user/tool patterns and emits high-confidence suggestions
without executing actions autonomously.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


class PredictiveAutomationDaemon:
    def __init__(self, config_path: Path):
        self._config_path = Path(config_path)
        self._events: list[dict] = []
        self._last_emit_by_key: dict[str, float] = {}

    def _load_config(self) -> dict:
        cfg = {
            "enabled": True,
            "lookback_minutes": 240,
            "min_pattern_repeats": 3,
            "min_confidence": 0.72,
            "emit_cooldown_seconds": 900,
            "max_events": 1200,
        }
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg["enabled"] = bool(raw.get("predictive_automation_enabled", cfg["enabled"]))
            cfg["lookback_minutes"] = max(30, min(int(raw.get("predictive_lookback_minutes", cfg["lookback_minutes"]) or 240), 1440))
            cfg["min_pattern_repeats"] = max(2, min(int(raw.get("predictive_min_pattern_repeats", cfg["min_pattern_repeats"]) or 3), 10))
            cfg["min_confidence"] = max(0.5, min(float(raw.get("predictive_min_confidence", cfg["min_confidence"]) or 0.72), 0.99))
            cfg["emit_cooldown_seconds"] = max(60, min(int(raw.get("predictive_emit_cooldown_seconds", cfg["emit_cooldown_seconds"]) or 900), 86400))
            cfg["max_events"] = max(200, min(int(raw.get("predictive_max_events", cfg["max_events"]) or 1200), 10000))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower())).strip()

    def _append_event(self, event: dict) -> None:
        cfg = self._load_config()
        self._events.append(event)
        max_events = int(cfg["max_events"])
        if len(self._events) > max_events:
            self._events = self._events[-max_events:]

    def record_text_command(self, text: str) -> None:
        norm = self._normalize_text(text)
        if not norm:
            return
        preview = norm[:80]
        key = f"text:{preview}"
        self._append_event({
            "ts": time.time(),
            "key": key,
            "kind": "text",
            "preview": preview,
        })

    def record_tool_call(self, name: str, args: dict | None = None) -> None:
        tool = (name or "").strip().lower()
        if not tool:
            return
        action = ""
        if isinstance(args, dict):
            action = str(args.get("action", "") or "").strip().lower()
        preview = f"{tool}:{action}" if action else tool
        key = f"tool:{preview}"
        self._append_event({
            "ts": time.time(),
            "key": key,
            "kind": "tool",
            "preview": preview,
        })

    @staticmethod
    def _score_pattern(count: int, timestamps: list[float]) -> float:
        if count <= 1:
            return 0.0
        intervals = [max(1.0, timestamps[i] - timestamps[i - 1]) for i in range(1, len(timestamps))]
        if not intervals:
            return 0.0
        mean = sum(intervals) / len(intervals)
        var = sum((x - mean) ** 2 for x in intervals) / len(intervals)
        std = var ** 0.5
        stability = max(0.0, min(1.0, 1.0 - (std / mean)))
        recurrence = min(1.0, count / 6.0)
        score = 0.45 + (0.35 * recurrence) + (0.20 * stability)
        return max(0.0, min(0.99, score))

    def generate_suggestions(self) -> list[dict]:
        cfg = self._load_config()
        if not cfg["enabled"]:
            return []

        now = time.time()
        lookback = int(cfg["lookback_minutes"]) * 60
        min_repeats = int(cfg["min_pattern_repeats"])
        min_conf = float(cfg["min_confidence"])
        emit_cooldown = int(cfg["emit_cooldown_seconds"])

        window = [e for e in self._events if (now - float(e.get("ts", 0.0))) <= lookback]
        if not window:
            return []

        groups: dict[str, list[dict]] = {}
        for event in window:
            groups.setdefault(str(event.get("key", "")), []).append(event)

        suggestions: list[dict] = []
        for key, items in groups.items():
            if len(items) < min_repeats:
                continue
            last_emit = self._last_emit_by_key.get(key, 0.0)
            if (now - last_emit) < emit_cooldown:
                continue

            items = sorted(items, key=lambda x: float(x.get("ts", 0.0)))
            stamps = [float(x.get("ts", 0.0)) for x in items]
            confidence = self._score_pattern(len(items), stamps)
            if confidence < min_conf:
                continue

            kind = str(items[-1].get("kind", ""))
            preview = str(items[-1].get("preview", ""))
            if kind == "tool":
                title = f"Recurring tool usage: {preview}"
                summary = f"Detected {len(items)} uses of {preview} in the recent activity window."
                action_hint = "Create a proactive macro for this tool flow"
            else:
                title = "Recurring request pattern detected"
                summary = f"Detected {len(items)} similar command requests: '{preview}'."
                action_hint = "Offer a scheduled reminder or one-tap automation"

            suggestion = {
                "key": key,
                "title": title,
                "summary": summary,
                "confidence": round(confidence, 2),
                "count": len(items),
                "action_hint": action_hint,
                "preview": preview,
            }
            suggestions.append(suggestion)

        suggestions.sort(key=lambda s: float(s.get("confidence", 0.0)), reverse=True)

        if suggestions:
            self._last_emit_by_key[suggestions[0]["key"]] = now
            return [suggestions[0]]
        return []
