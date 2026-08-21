"""Per-feature API usage and cost tracking.

Added because the continuous features now running unattended have no spend
visibility at all: the visual-context check alone fires a Gemini vision call
roughly every 60s (~1,400/day) at 4K, on top of per-sentence ElevenLabs TTS
and whatever the live session and dev_agent consume. Without this, a runaway
loop or a newly-enabled always-on feature is invisible until a bill arrives.

Deliberately estimate-based: providers do not return per-call cost, so this
records call counts plus units (tokens/characters/images) and applies a
static rate table. Treat totals as an order-of-magnitude guide, not billing
truth — the call COUNTS are exact, and those are what catch a runaway.
"""
import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR") or (BASE_DIR / "memory")).expanduser()
USAGE_PATH = DATA_DIR / "usage_log.jsonl"

MAX_ENTRIES = 20000

_lock = threading.Lock()

# Approximate published rates (USD). Kept obvious and adjustable rather than
# hidden in code — update when provider pricing changes.
RATES = {
    "gemini_image":     {"per_call": 0.00015},   # one vision frame, flash-tier
    "gemini_text":      {"per_call": 0.00005},
    "gemini_embedding": {"per_call": 0.00001},
    "elevenlabs_tts":   {"per_1k_chars": 0.30},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_cost(kind: str, units: int = 0) -> float:
    rate = RATES.get(kind)
    if not rate:
        return 0.0
    if "per_call" in rate:
        return float(rate["per_call"])
    if "per_1k_chars" in rate:
        return float(rate["per_1k_chars"]) * (max(0, units) / 1000.0)
    return 0.0


def record_usage(feature: str, kind: str, units: int = 0) -> None:
    """Log one API call. Best-effort and never raises — usage accounting must
    never be able to break the feature it is measuring."""
    try:
        entry = {
            "ts": _now_iso(),
            "day": _today(),
            "feature": feature,
            "kind": kind,
            "units": int(units),
            "est_cost_usd": round(estimate_cost(kind, units), 6),
        }
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with USAGE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _read_entries() -> list[dict]:
    if not USAGE_PATH.exists():
        return []
    entries = []
    with _lock:
        try:
            with USAGE_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
    return entries


def trim_if_needed() -> None:
    entries = _read_entries()
    if len(entries) <= MAX_ENTRIES:
        return
    keep = entries[-MAX_ENTRIES:]
    with _lock:
        with USAGE_PATH.open("w", encoding="utf-8") as f:
            for e in keep:
                f.write(json.dumps(e) + "\n")


def summarize(day: str | None = None) -> dict:
    """Aggregate usage for one day (default: today, UTC)."""
    target = day or _today()
    entries = [e for e in _read_entries() if e.get("day") == target]

    by_feature: dict[str, dict] = defaultdict(lambda: {"calls": 0, "est_cost_usd": 0.0})
    total_cost = 0.0
    for e in entries:
        feature = str(e.get("feature", "unknown"))
        cost = float(e.get("est_cost_usd", 0.0) or 0.0)
        by_feature[feature]["calls"] += 1
        by_feature[feature]["est_cost_usd"] += cost
        total_cost += cost

    return {
        "day": target,
        "total_calls": len(entries),
        "est_total_usd": round(total_cost, 4),
        "by_feature": {
            k: {"calls": v["calls"], "est_cost_usd": round(v["est_cost_usd"], 4)}
            for k, v in sorted(by_feature.items(), key=lambda kv: -kv[1]["est_cost_usd"])
        },
    }


def calls_since(feature: str, seconds: float) -> int:
    """How many calls this feature made in the last N seconds — the runaway
    check. Uses wall-clock timestamps so it survives a process restart."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    count = 0
    for e in _read_entries():
        if e.get("feature") != feature:
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            count += 1
    return count


def format_summary(summary: dict) -> str:
    if not summary["total_calls"]:
        return f"No API usage recorded for {summary['day']}, sir."
    lines = [
        f"API usage for {summary['day']}: {summary['total_calls']} calls, "
        f"roughly ${summary['est_total_usd']:.2f} (estimated)."
    ]
    for feature, stats in summary["by_feature"].items():
        lines.append(f"  {feature}: {stats['calls']} calls, ~${stats['est_cost_usd']:.2f}")
    lines.append("Costs are estimates from a static rate table, not billed amounts.")
    return "\n".join(lines)


def usage_report(parameters: dict | None = None, response=None, player=None,
                 session_memory=None, speak=None) -> str:
    p = parameters or {}
    day = str(p.get("day", "") or "").strip() or None
    return format_summary(summarize(day))
