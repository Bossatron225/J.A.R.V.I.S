"""Multi-step task execution with per-step verification.

Jarvis exposes ~40 tools but effectively runs them one at a time. A request
like "find a flight, put it in my calendar, and text Nevaeh the details" is
three dependent tool calls, and nothing sequences them, threads results
between them, or checks that each step actually did what it claimed.

That last part is the important one here. The recurring failure mode in this
system has been subsystems reporting success while doing nothing — a monitor
"engaged" with no camera open, a poller that had silently stopped, a live
session that dropped every command. So this planner treats a step's own return
value as a claim to be checked, not as proof: a step that reports success but
whose output looks like a failure is marked failed, and dependent steps are
skipped rather than run on top of a broken prerequisite.

Plan-only by default. Execution runs through the caller's own tool dispatcher,
so every existing permission and headless guard still applies.
"""
import json
import re

MAX_STEPS = 8

# Phrases that mean a tool returned "fine" while describing a failure. Checked
# case-insensitively against a step's output before it is accepted as done.
_FAILURE_MARKERS = (
    "unavailable",
    "not available",
    "failed",
    "error",
    "could not",
    "couldn't",
    "unable to",
    "no results",
    "not found",
    "timed out",
    "timeout",
    "mac_offline",
    "did not respond",
    "access denied",
    "permission denied",
    "no matching",
    "unknown tool",
)


class PlanError(Exception):
    """A plan could not be produced or is not runnable."""


def verify_step_output(output) -> tuple[bool, str]:
    """Decide whether a step's output actually represents success.

    Deliberately conservative about trusting a tool's own word: several tools
    in this codebase return a plain string describing a failure, which naive
    handling would treat as a successful result."""
    if output is None:
        return False, "step produced no output"

    text = output if isinstance(output, str) else str(output)
    stripped = text.strip()
    if not stripped:
        return False, "step produced empty output"

    lowered = stripped.lower()
    for marker in _FAILURE_MARKERS:
        if marker in lowered:
            return False, f"output reports failure ({marker!r}): {stripped[:160]}"
    return True, stripped[:160]


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def build_plan(request: str, tool_names: list[str], generate) -> list[dict]:
    """Decompose a request into ordered steps. `generate` is a callable taking
    a prompt and returning text, so this module stays free of any specific
    model client."""
    request = (request or "").strip()
    if not request:
        raise PlanError("empty request")

    prompt = f"""Break this request into the smallest ordered sequence of tool calls that accomplishes it.

Request: {request}

Available tools: {", ".join(sorted(tool_names))}

Return ONLY valid JSON, no markdown:
{{"steps": [{{"tool": "tool_name", "arguments": {{}}, "intent": "what this step achieves", "depends_on": []}}]}}

Rules:
1. Use ONLY tools from the list above.
2. "depends_on" holds zero-based indices of earlier steps whose result this step needs.
3. Maximum {MAX_STEPS} steps. Prefer fewer.
4. If the request needs only one tool, return exactly one step.
5. If no listed tool can accomplish it, return {{"steps": []}}.

JSON:"""

    try:
        parsed = json.loads(_strip_fences(generate(prompt)))
    except Exception as exc:
        raise PlanError(f"planner did not return valid JSON: {exc}")

    steps_raw = parsed.get("steps")
    if not isinstance(steps_raw, list):
        raise PlanError("planner returned no steps list")

    steps: list[dict] = []
    for index, raw in enumerate(steps_raw[:MAX_STEPS]):
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool", "") or "").strip()
        if tool not in tool_names:
            raise PlanError(f"step {index + 1} uses unknown tool {tool!r}")
        depends = [d for d in (raw.get("depends_on") or []) if isinstance(d, int) and 0 <= d < index]
        steps.append({
            "tool": tool,
            "arguments": raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {},
            "intent": str(raw.get("intent", "") or tool),
            "depends_on": depends,
        })
    return steps


def format_plan(request: str, steps: list[dict]) -> str:
    if not steps:
        return f"I can't accomplish '{request}' with the tools I have, sir."
    lines = [f"Plan for '{request}' — {len(steps)} step(s):"]
    for index, step in enumerate(steps, 1):
        suffix = ""
        if step["depends_on"]:
            suffix = f" (needs step {', '.join(str(d + 1) for d in step['depends_on'])})"
        lines.append(f"  {index}. {step['intent']} [{step['tool']}]{suffix}")
    return "\n".join(lines)


def execute_plan(steps: list[dict], run_tool) -> dict:
    """Run each step via `run_tool(tool_name, arguments) -> output`, verifying
    as it goes. A step whose prerequisite failed is skipped rather than run
    against a broken precondition."""
    results: list[dict] = []
    failed_indices: set[int] = set()

    for index, step in enumerate(steps):
        blocked = [d for d in step["depends_on"] if d in failed_indices]
        if blocked:
            results.append({
                "step": index + 1, "tool": step["tool"], "intent": step["intent"],
                "status": "skipped",
                "detail": f"prerequisite step {', '.join(str(b + 1) for b in blocked)} failed",
            })
            failed_indices.add(index)
            continue

        try:
            output = run_tool(step["tool"], dict(step["arguments"]))
        except Exception as exc:
            results.append({
                "step": index + 1, "tool": step["tool"], "intent": step["intent"],
                "status": "failed", "detail": f"raised {type(exc).__name__}: {exc}",
            })
            failed_indices.add(index)
            continue

        ok, detail = verify_step_output(output)
        results.append({
            "step": index + 1, "tool": step["tool"], "intent": step["intent"],
            "status": "done" if ok else "failed", "detail": detail,
        })
        if not ok:
            failed_indices.add(index)

    return {
        "ok": not failed_indices,
        "completed": sum(1 for r in results if r["status"] == "done"),
        "total": len(steps),
        "results": results,
    }


def format_execution(report: dict) -> str:
    if not report["results"]:
        return "Nothing to run, sir."
    header = (
        f"All {report['total']} step(s) completed, sir."
        if report["ok"]
        else f"{report['completed']} of {report['total']} step(s) completed — the rest did not, sir."
    )
    lines = [header]
    for item in report["results"]:
        mark = {"done": "OK", "failed": "FAIL", "skipped": "SKIP"}.get(item["status"], "?")
        lines.append(f"  [{mark}] {item['step']}. {item['intent']} — {item['detail']}")
    return "\n".join(lines)
