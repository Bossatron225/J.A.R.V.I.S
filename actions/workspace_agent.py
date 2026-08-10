import re
import shlex
import subprocess
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir().resolve()
_MAX_OUTPUT_CHARS = 8000

# Core Jarvis source files that must not be modified without going through the approval flow
_PROTECTED_PATHS = {
    "main.py", "ui.py", "setup.py",
    "core/tts.py", "core/stt.py", "core/llm_client.py", "core/installer.py",
    "actions/dev_agent.py", "actions/workspace_agent.py",
}

def _is_protected(path: Path) -> bool:
    """Return True if path is a core Jarvis source file that requires approval to modify."""
    try:
        rel = str(path.relative_to(BASE_DIR))
    except ValueError:
        return False
    return rel in _PROTECTED_PATHS or rel.startswith("actions/") or rel.startswith("core/")


_DENY_PATTERNS = [
    r"\bsudo\b",
    r"\brm\s+-rf\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+checkout\s+--\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bchmod\s+777\b",
]

_ALLOWED_COMMAND_PATTERNS = [
    r"^(python|python3|\.\/\.venv-1\/bin\/python)(\s|$)",
    r"^pytest(\s|$)",
    r"^ruff(\s|$)",
    r"^mypy(\s|$)",
    r"^rg(\s|$)",
    r"^git\s+(status|diff|log|show)(\s|$)",
    r"^(ls|cat|head|tail|wc|sed|awk)(\s|$)",
]


def _resolve_repo_path(path_text: str) -> Path | None:
    if not path_text:
        return None
    p = Path(path_text).expanduser()
    resolved = p.resolve() if p.is_absolute() else (BASE_DIR / p).resolve()
    try:
        resolved.relative_to(BASE_DIR)
    except ValueError:
        return None
    return resolved


def _read_lines(path: Path, start_line: int, end_line: int) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    if not lines:
        return ""

    start = max(1, start_line)
    end = min(len(lines), max(start, end_line))
    out = []
    for i in range(start, end + 1):
        out.append(f"{i:>5}: {lines[i - 1]}")
    return "\n".join(out)


def _is_command_allowed(command: str) -> tuple[bool, str]:
    cmd = (command or "").strip()
    if not cmd:
        return False, "Empty command."

    lowered = cmd.lower()
    for pat in _DENY_PATTERNS:
        if re.search(pat, lowered):
            return False, f"Command blocked by safety policy: {pat}"

    for pat in _ALLOWED_COMMAND_PATTERNS:
        if re.match(pat, cmd):
            return True, ""

    return False, "Command not in allowlist."


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "\n\n[Truncated output]"


def _run_shell(command: str, timeout: int = 60) -> str:
    ok, reason = _is_command_allowed(command)
    if not ok:
        return f"Blocked: {reason}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5, int(timeout or 60)),
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        parts = [f"exit_code: {result.returncode}"]
        if stdout:
            parts.append("stdout:\n" + stdout)
        if stderr:
            parts.append("stderr:\n" + stderr)
        return _truncate("\n\n".join(parts))
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except Exception as exc:
        return f"Run error: {exc}"


def _search_text(query: str, include: str = "") -> str:
    if not query.strip():
        return "Please provide a query."

    cmd = ["rg", "-n", "--hidden", "--glob", "!.git", query]
    if include.strip():
        cmd.extend(["-g", include.strip()])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if not out:
            if err:
                return _truncate(err)
            return "No matches found."
        return _truncate(out)
    except FileNotFoundError:
        pattern = include.strip() or "**/*"
        found = []
        for path in BASE_DIR.glob(pattern):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(BASE_DIR)
                if ".git" in rel.parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for i, line in enumerate(text.splitlines(), start=1):
                if query.lower() in line.lower():
                    found.append(f"{rel}:{i}:{line}")
                    if len(found) >= 200:
                        return _truncate("\n".join(found))

        return _truncate("\n".join(found)) if found else "No matches found."
    except Exception as exc:
        return f"Search error: {exc}"


def _list_files(glob: str = "") -> str:
    cmd = ["rg", "--files", "--hidden", "--glob", "!.git"]
    if glob.strip():
        cmd.extend(["-g", glob.strip()])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        out = (result.stdout or "").strip()
        if not out:
            return "No files found."
        return _truncate(out)
    except FileNotFoundError:
        files = [str(p.relative_to(BASE_DIR)) for p in BASE_DIR.rglob("*") if p.is_file()]
        files.sort()
        return _truncate("\n".join(files)) if files else "No files found."
    except Exception as exc:
        return f"List error: {exc}"


def _replace_text(path: Path, old_text: str, new_text: str, replace_all: bool) -> str:
    current = path.read_text(encoding="utf-8", errors="ignore")
    if old_text not in current:
        return "Old text not found."

    if replace_all:
        updated = current.replace(old_text, new_text)
    else:
        updated = current.replace(old_text, new_text, 1)

    path.write_text(updated, encoding="utf-8")
    return "Replace complete."


def _status_text() -> str:
    return (
        "Workspace agent ready.\n"
        "Actions: status, list_files, search, read_file, write_file, append_file, "
        "replace_text, run_command, improve_self.\n"
        "Safety: repo-scoped paths only + command allowlist + dangerous command blocks.\n"
        "Self-improve supports approval_action=propose|apply|status|clear with optional approval_id and self_reboot."
    )


def workspace_agent(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p = parameters or {}
    action = str(p.get("action", "status")).strip().lower()

    if action == "status":
        return _status_text()

    if action == "list_files":
        return _list_files(str(p.get("glob", "")))

    if action == "search":
        return _search_text(str(p.get("query", "")), str(p.get("include", "")))

    if action == "read_file":
        path = _resolve_repo_path(str(p.get("path", "")))
        if not path:
            return "Invalid path (must be inside workspace)."
        if not path.exists() or not path.is_file():
            return f"File not found: {p.get('path', '')}"

        start_line = int(p.get("start_line", 1) or 1)
        end_line = int(p.get("end_line", start_line + 200) or (start_line + 200))
        return _read_lines(path, start_line, end_line)

    if action == "write_file":
        path = _resolve_repo_path(str(p.get("path", "")))
        if not path:
            return "Invalid path (must be inside workspace)."
        if _is_protected(path):
            return (
                f"BLOCKED: '{path.relative_to(BASE_DIR)}' is a protected Jarvis source file. "
                "Use dev_agent with self_improve=true and the approval flow to modify core files safely."
            )
        overwrite = bool(p.get("overwrite", False))
        if path.exists() and not overwrite:
            return "Refusing to overwrite existing file without overwrite=true."
        content = str(p.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote file: {path.relative_to(BASE_DIR)}"

    if action == "append_file":
        path = _resolve_repo_path(str(p.get("path", "")))
        if not path:
            return "Invalid path (must be inside workspace)."
        if _is_protected(path):
            return (
                f"BLOCKED: '{path.relative_to(BASE_DIR)}' is a protected Jarvis source file. "
                "Use dev_agent with self_improve=true and the approval flow to modify core files safely."
            )
        content = str(p.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended file: {path.relative_to(BASE_DIR)}"

    if action == "replace_text":
        path = _resolve_repo_path(str(p.get("path", "")))
        if not path:
            return "Invalid path (must be inside workspace)."
        if _is_protected(path):
            return (
                f"BLOCKED: '{path.relative_to(BASE_DIR)}' is a protected Jarvis source file. "
                "Use dev_agent with self_improve=true and the approval flow to modify core files safely."
            )
        if not path.exists() or not path.is_file():
            return f"File not found: {p.get('path', '')}"

        old_text = str(p.get("old_text", ""))
        new_text = str(p.get("new_text", ""))
        if not old_text:
            return "Please provide old_text."
        replace_all = bool(p.get("replace_all", False))
        return _replace_text(path, old_text, new_text, replace_all)

    if action == "run_command":
        command = str(p.get("command", ""))
        timeout = int(p.get("timeout", 60) or 60)
        return _run_shell(command, timeout=timeout)

    if action == "improve_self":
        description = str(p.get("description", "")).strip()
        approval_action = str(p.get("approval_action", "") or "").strip().lower()
        if not description and approval_action not in {"status", "apply", "clear"}:
            return "Please provide description for improve_self."
        target_files = p.get("target_files") or []
        if isinstance(target_files, str):
            target_files = [x.strip() for x in target_files.split(",") if x.strip()]

        from actions.dev_agent import dev_agent

        return dev_agent(
            parameters={
                "description": description,
                "self_improve": True,
                "target_files": target_files,
                "sandbox": True,
                "timeout": int(p.get("timeout", 45) or 45),
                "require_approval": bool(p.get("require_approval", True)),
                "approval_action": approval_action,
                "approval_id": str(p.get("approval_id", "") or "").strip(),
                "self_reboot": bool(p.get("self_reboot", True)),
            },
            response=response,
            player=player,
            session_memory=session_memory,
            speak=None,
        )

    return (
        "Unknown action. Use one of: status, list_files, search, read_file, write_file, "
        "append_file, replace_text, run_command, improve_self."
    )
