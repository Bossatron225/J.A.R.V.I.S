"""Deciding what a standing goal found and whether it is worth interrupting for.

Kept separate from the store and from main.py's execution wiring so the
judgement logic is testable without a model, a network, or a running Jarvis.

The bar for surfacing is deliberately high. A goal that reports everything it
finds is just a search on a timer, and becomes noise the user tunes out —
which is how the visitor alerts nearly went wrong too. Better to stay silent
than to interrupt with a near-miss.
"""
import json
import re

# Tools a goal may use when it is NOT permitted to act. Anything that sends,
# spends, books, changes the machine, or rewrites code is excluded — an
# unattended loop must not be able to do those without explicit opt-in.
READ_ONLY_TOOLS = {
    "web_search",
    "weather_report",
    "get_current_time",
    "system_status",
    "flight_finder",
    "google_calendar",
    "visitor_log",
    "personal_memory",
    "usage_report",
    "activity_report",
    "open_loops",
    "recall_conversation",
    "document_memory",
}


def allowed_tools(all_tool_names: list[str], allow_actions: bool) -> list[str]:
    """Which tools this goal may plan with."""
    if allow_actions:
        # Even an action-permitted goal never gets to modify Jarvis itself or
        # shut him down unattended.
        blocked = {"dev_agent", "workspace_agent", "shutdown_jarvis", "multi_step_task"}
        return [t for t in all_tool_names if t not in blocked]
    return [t for t in all_tool_names if t in READ_ONLY_TOOLS]


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def evaluate_findings(objective: str, criteria: str, raw_output: str, generate,
                      already_rejected_samples: list[str] | None = None) -> list[dict]:
    """Judge a check's raw output against the goal, returning only items worth
    raising: [{"summary", "why", "confidence"}].

    `generate` takes a prompt and returns text, so this module stays free of
    any particular model client."""
    raw_output = (raw_output or "").strip()
    if not raw_output:
        return []

    rejected_note = ""
    if already_rejected_samples:
        joined = "; ".join(already_rejected_samples[:5])
        rejected_note = (
            f"\nThe user previously dismissed things like: {joined}\n"
            "Do not surface anything similar to those.\n"
        )

    prompt = f"""You are filtering results for a standing goal. Be strict: it is better to
return nothing than to interrupt the user with a near-miss.

Goal: {objective}
Must satisfy: {criteria or "(no explicit criteria — use judgement about what the goal implies)"}
{rejected_note}
Findings to assess:
{raw_output[:6000]}

Return ONLY valid JSON, no markdown:
{{"items": [{{"summary": "one line describing the find", "why": "why it meets the goal", "confidence": 0.0}}]}}

Rules:
1. Include an item ONLY if it clearly satisfies the goal and criteria.
2. confidence is 0.0-1.0; use below 0.6 if unsure — those are filtered out.
3. If nothing qualifies, return {{"items": []}}. That is a normal, good answer.
4. Never invent details that are not in the findings.

JSON:"""

    try:
        parsed = json.loads(_strip_fences(generate(prompt)))
    except Exception:
        return []

    items = parsed.get("items")
    if not isinstance(items, list):
        return []

    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary", "") or "").strip()
        if not summary:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        if confidence < 0.6:
            continue
        results.append({
            "summary": summary,
            "why": str(item.get("why", "") or "").strip(),
            "confidence": round(confidence, 2),
        })
    return results


def filter_unseen(goal: dict, items: list[dict]) -> list[dict]:
    """Drop anything already surfaced or previously dismissed, so a goal never
    repeats itself."""
    from actions.goals import already_seen
    return [item for item in items if not already_seen(goal, item["summary"])]


def format_findings(goal: dict, items: list[dict]) -> str:
    if not items:
        return ""
    lines = [f"Update on your goal “{goal.get('objective', '')}”, sir:"]
    for item in items:
        line = f"  - {item['summary']}"
        if item.get("why"):
            line += f" ({item['why']})"
        lines.append(line)
    lines.append(f"Say “dismiss that for goal {goal.get('id')}” if it's not what you wanted.")
    return "\n".join(lines)
