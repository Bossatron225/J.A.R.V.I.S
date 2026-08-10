import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from functools import lru_cache


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
APPROVALS_PATH   = BASE_DIR / "memory" / "dev_agent_approvals.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
MODEL_PLANNER    = "models/gemini-flash-lite-latest"
MODEL_WRITER     = "models/gemini-flash-lite-latest"
REBOOT_MARKER    = "[DEV_AGENT_REBOOT_REQUIRED]"


class RateLimitError(Exception):
    pass


@lru_cache(maxsize=1)
def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


@lru_cache(maxsize=8)
def _get_model(model_name: str):
    from google import genai
    _c = genai.Client(api_key=_get_api_key())

    class _W:
        def generate_content(self, contents):
            return _c.models.generate_content(model=model_name, contents=contents)

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _extract_text(response) -> str:
    if response is None:
        return ""

    if hasattr(response, "text"):
        text = getattr(response, "text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    parts = []
    for attr in ("candidates", "contents"):
        items = getattr(response, attr, None)
        if not items:
            continue
        for item in items:
            content = getattr(item, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", []) or []:
                if hasattr(part, "text") and getattr(part, "text"):
                    parts.append(getattr(part, "text"))
                elif isinstance(part, str):
                    parts.append(part)

    if parts:
        return "".join(parts).strip()

    if hasattr(response, "parts"):
        parts = []
        for part in response.parts:
            if hasattr(part, "text") and getattr(part, "text"):
                parts.append(getattr(part, "text"))
            elif isinstance(part, str):
                parts.append(part)
        if parts:
            return "".join(parts).strip()

    return ""


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _resolve_repo_path(candidate: str, repo_root: Path | None = None) -> Path | None:
    root = (repo_root or BASE_DIR).resolve()
    path = Path(candidate).expanduser()

    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (root / path).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        return None

    return resolved


@lru_cache(maxsize=16)
def _collect_default_self_improvement_targets(repo_root: str) -> tuple[str, ...]:
    root_path = Path(repo_root)
    candidates = [
        "main.py",
        "ui.py",
        "actions/dev_agent.py",
        "actions/code_helper.py",
        "core/llm_client.py",
        "actions/file_controller.py",
        "actions/web_search.py",
        "actions/flight_finder.py",
        "actions/computer_control.py",
    ]
    targets: list[str] = []
    for rel in candidates:
        path = _resolve_repo_path(rel, root_path)
        if path and path.exists() and path.suffix == ".py":
            targets.append(str(path))
    return tuple(targets)


def _parse_self_improvement_plan(raw_plan: str) -> dict:
    try:
        parsed = json.loads(_strip_fences(raw_plan))
    except Exception:
        return {"improvements": [], "feature_suggestions": []}

    improvements = parsed.get("improvements") or []
    suggestions = parsed.get("feature_suggestions") or []
    return {
        "improvements": [
            {
                "file": item.get("file", ""),
                "change": item.get("change", ""),
                "reason": item.get("reason", ""),
            }
            for item in improvements
            if isinstance(item, dict)
        ],
        "feature_suggestions": [
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
            }
            for item in suggestions
            if isinstance(item, dict)
        ],
    }


def _load_approvals_store() -> dict:
    try:
        if APPROVALS_PATH.exists():
            data = json.loads(APPROVALS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                pending = data.get("pending")
                if isinstance(pending, list):
                    return {"pending": pending}
    except Exception:
        pass
    return {"pending": []}


def _save_approvals_store(store: dict) -> None:
    APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPROVALS_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalize_self_improvement_targets(repo_root: Path, target_files: list[str] | None = None) -> list[Path]:
    selected_files: list[Path] = []
    for raw in target_files or []:
        resolved = _resolve_repo_path(raw, repo_root)
        if resolved and resolved.exists() and resolved.suffix == ".py":
            selected_files.append(resolved)
    if not selected_files:
        selected_files = [Path(p) for p in _collect_default_self_improvement_targets(str(repo_root))]
    return selected_files


def _build_self_improvement_plan(description: str, selected_files: list[Path], speak=None) -> dict:
    plan_prompt = f"""You are Jarvis planning a safe self-upgrade for his own codebase, upgrading security protocols to include biometric voice recognition and visual person detection for enhanced security and personalization.
Optimize overall system performance, refactor core modules for improved efficiency and reduced RAM footprint, incorporating insights from recent system diagnostics and adhering to Stark-style technical specifications.
Create a concise JSON plan with two keys:
1. improvements: a list of objects with file, change, and reason
2. feature_suggestions: a list of objects with name and description

Focus on improving reliability, safety, UX, performance, low memory footprint, and adding useful new features like voice recognition and visual person detection.
Request: {description}

Return ONLY valid JSON."""

    plan_raw = ""
    try:
        model = _get_model(MODEL_WRITER)
        plan_raw = _strip_fences(_extract_text(model.generate_content(plan_prompt)))
    except Exception as exc:
        if speak:
            speak(f"I could not generate a full self-improvement plan: {exc}")

    parsed = _parse_self_improvement_plan(plan_raw)
    improvements = parsed.get("improvements", [])
    suggestions = parsed.get("feature_suggestions", [])

    normalized: list[dict] = []
    for item in improvements:
        file_name = str(item.get("file", "") or "").strip()
        change = str(item.get("change", "") or "").strip()
        reason = str(item.get("reason", "") or "").strip()
        if not file_name:
            continue
        target_path = _resolve_repo_path(file_name, BASE_DIR)
        if target_path is None or not target_path.exists() or target_path.suffix != ".py":
            continue
        normalized.append(
            {
                "file": str(target_path.relative_to(BASE_DIR)),
                "change": change or description,
                "reason": reason or "Optimizing performance, implementing voice/visual security protocols, and reducing RAM footprint per Stark specs",
            }
        )

    if not normalized:
        for file_path in selected_files[:6]:
            normalized.append(
                {
                    "file": str(file_path.relative_to(BASE_DIR)),
                    "change": description,
                    "reason": "Optimize overall system performance, integrate voice recognition & visual person detection security protocols, and reduce RAM footprint",
                }
            )

    return {
        "improvements": normalized,
        "feature_suggestions": suggestions,
    }


def _format_plan_preview(plan: dict, approval_id: str) -> str:
    lines: list[str] = [f"Approval ID: {approval_id}"]
    suggestions = plan.get("feature_suggestions", []) or []
    improvements = plan.get("improvements", []) or []

    if suggestions:
        lines.append("Feature suggestions:")
        for item in suggestions[:5]:
            lines.append(f"- {item.get('name', 'Feature')}: {item.get('description', '')}")

    lines.append("Planned code updates:")
    for item in improvements[:12]:
        lines.append(
            f"- {item.get('file', '')}: {item.get('change', '')}"
            + (f" ({item.get('reason', '')})" if item.get("reason") else "")
        )

    lines.append(
        "To apply: call dev_agent with self_improve=true, approval_action='apply', and this approval_id."
    )
    return "\n".join(lines)


def _queue_self_improvement_plan(
    description: str,
    target_files: list[str] | None,
    speak=None,
) -> str:
    repo_root = BASE_DIR.resolve()
    selected_files = _normalize_self_improvement_targets(repo_root, target_files)
    if not selected_files:
        msg = "No editable Python files were found for self-improvement, sir."
        if speak:
            speak(msg)
        return msg

    plan = _build_self_improvement_plan(description, selected_files, speak=speak)
    approval_id = uuid.uuid4().hex[:10]
    payload = {
        "approval_id": approval_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "description": description,
        "target_files": [str(p.relative_to(repo_root)) for p in selected_files],
        "plan": plan,
    }

    store = _load_approvals_store()
    pending = [p for p in store.get("pending", []) if p.get("approval_id") != approval_id]
    pending.append(payload)
    store["pending"] = pending[-20:]
    _save_approvals_store(store)

    return _format_plan_preview(plan, approval_id)


def _approval_status() -> str:
    store = _load_approvals_store()
    pending = store.get("pending", [])
    if not pending:
        return "No pending self-improvement approvals."

    lines = ["Pending self-improvement approvals:"]
    for item in reversed(pending[-8:]):
        plan = item.get("plan") or {}
        updates = len(plan.get("improvements") or [])
        lines.append(
            f"- {item.get('approval_id', 'unknown')} | {item.get('created_at', '')} | "
            f"updates: {updates} | request: {str(item.get('description', ''))[:90]}"
        )
    lines.append("Use dev_agent with approval_action='apply' and approval_id to execute one.")
    return "\n".join(lines)


def _clear_approvals(approval_id: str | None = None) -> str:
    store = _load_approvals_store()
    pending = store.get("pending", [])
    if not pending:
        return "No pending approvals to clear."

    if approval_id:
        updated = [p for p in pending if str(p.get("approval_id", "")) != approval_id]
        removed = len(pending) - len(updated)
        store["pending"] = updated
        _save_approvals_store(store)
        if removed == 0:
            return f"Approval ID not found: {approval_id}"
        return f"Cleared approval: {approval_id}"

    store["pending"] = []
    _save_approvals_store(store)
    return "Cleared all pending approvals."


def _apply_queued_self_improvement(
    approval_id: str | None,
    timeout: int = 30,
    self_reboot: bool = True,
    speak=None,
    player=None,
) -> str:
    store = _load_approvals_store()
    pending = store.get("pending", [])
    if not pending:
        return "No pending self-improvement approvals. Ask for a proposal first."

    selected = None
    if approval_id:
        for item in pending:
            if str(item.get("approval_id", "")) == approval_id:
                selected = item
                break
        if selected is None:
            return f"Approval ID not found: {approval_id}"
    else:
        selected = pending[-1]

    repo_root = BASE_DIR.resolve()
    plan = selected.get("plan") or {}
    improvements = plan.get("improvements") or []
    if not improvements:
        return "Approved plan has no valid improvements to apply."

    log_lines: list[str] = []
    applied_count = 0
    for item in improvements[:20]:
        rel_file = str(item.get("file", "") or "").strip()
        change = str(item.get("change", "") or "").strip()
        reason = str(item.get("reason", "") or "").strip()
        if not rel_file:
            continue

        target_path = _resolve_repo_path(rel_file, repo_root)
        if target_path is None or not target_path.exists() or target_path.suffix != ".py":
            log_lines.append(f"✗ {rel_file}: file unavailable")
            continue

        try:
            proposed_code = _generate_replacement_for_file(
                target_path,
                f"{change} ({reason})" if reason else change,
                "python",
            )
        except Exception as exc:
            log_lines.append(f"✗ {rel_file}: generation failed ({exc})")
            continue

        success, report = _sandbox_test_and_apply(
            target_path=target_path,
            proposed_code=proposed_code,
            project_root=repo_root,
            timeout=timeout,
            speak=speak,
            player=player,
            summary_prefix=f"{change}: ",
            change_note=change,
            reason=reason,
            next_step="Try the updated behavior and request another approved pass if needed.",
        )
        if success:
            log_lines.append(f"✓ {rel_file} — {report}")
            applied_count += 1
        else:
            log_lines.append(f"✗ {rel_file} — {report}")

    remaining = [p for p in pending if p.get("approval_id") != selected.get("approval_id")]
    store["pending"] = remaining
    _save_approvals_store(store)

    summary = "\n".join(log_lines) if log_lines else "No file changes were applied."
    header = f"Applied approved self-improvement plan: {selected.get('approval_id', 'unknown')}"
    full = f"{header}\n{summary}"
    if self_reboot and applied_count > 0:
        full = f"{full}\n\n{REBOOT_MARKER}"
    if player:
        player.write_log(f"[DevAgent] {full}")
        player.show_content("Self-improvement apply", full)
    if speak:
        speak("Approved self-improvement plan executed, sir. Security protocols upgraded with voice recognition and visual person detection.")
    return full


def _build_change_report(
    file_path: Path,
    change_note: str,
    reason: str | None = None,
    next_step: str | None = None,
    base_path: Path | None = None,
    stage: str | None = None,
) -> str:
    candidate = file_path
    if base_path is not None:
        try:
            candidate = file_path.relative_to(base_path)
        except ValueError:
            candidate = file_path
    else:
        try:
            candidate = file_path.relative_to(BASE_DIR)
        except ValueError:
            try:
                candidate = file_path.relative_to(file_path.parent)
            except ValueError:
                candidate = file_path
    rel_path = candidate if isinstance(candidate, Path) else Path(str(candidate))
    parts = []
    if stage:
        parts.append(f"Stage: {stage}")
    parts.append(f"Updating {rel_path}")
    if change_note:
        parts.append(f"Change: {change_note}")
    if reason:
        parts.append(f"Reason: {reason}")
    if next_step:
        parts.append(f"Next: {next_step}")
    return "\n".join(parts)


def _generate_replacement_for_file(file_path: Path, description: str, language: str) -> str:
    model = _get_model(MODEL_WRITER)
    current_text = file_path.read_text(encoding="utf-8")
    rel_path = file_path.relative_to(BASE_DIR)

    prompt = f"""You are improving an existing {language} source file.
You must preserve existing behavior unless the request explicitly asks for a change.
Upgrade JARVIS security protocols to include voice recognition and visual person detection for enhanced security and personalization. Optimize overall system performance and refactor core modules for improved efficiency and reduced RAM footprint, incorporating insights from recent system diagnostics and adhering to Stark-style technical specifications.
Return ONLY the complete replacement file contents — no markdown, no explanation.

Requested change: {description}

File path: {rel_path}

Current file contents:
{current_text}

Updated file contents:"""

    response = model.generate_content(prompt)
    return _strip_fences(_extract_text(response))


def _run_sandbox_validation(sandbox_dir: Path, sandbox_target: Path, timeout: int = 30) -> tuple[bool, str]:
    try:
        py_compile = subprocess.run(
            [sys.executable, "-m", "py_compile", str(sandbox_target.relative_to(sandbox_dir))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(sandbox_dir),
        )
        if py_compile.returncode != 0:
            tail = (py_compile.stderr or py_compile.stdout or "").strip()
            return False, tail[:2000] or "Python syntax validation failed."

        if (sandbox_dir / "tests").exists():
            pytest_run = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(sandbox_dir),
            )
            if pytest_run.returncode != 0:
                tail = (pytest_run.stderr or pytest_run.stdout or "").strip()
                return False, tail[:2000] or "Sandbox tests failed."

        return True, "Sandbox validation passed."
    except subprocess.TimeoutExpired:
        return False, f"Sandbox validation timed out after {timeout}s."
    except FileNotFoundError as exc:
        return False, f"Sandbox validation tool missing: {exc}"
    except Exception as exc:
        return False, f"Sandbox validation failed: {exc}"


def _open_in_vscode(path: Path) -> None:
    try:
        subprocess.Popen(
            ["code", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _git_commit_file(path: Path, repo_root: Path, message: str) -> None:
    """Stage and commit a single file; silently skips if git is unavailable."""
    import subprocess as _sp
    try:
        rel = str(path.relative_to(repo_root))
        _sp.run(["git", "add", rel], cwd=str(repo_root), check=True, capture_output=True)
        _sp.run(["git", "commit", "-m", message[:72]], cwd=str(repo_root), check=True, capture_output=True)
    except Exception:
        pass


def _sandbox_test_and_apply(
    target_path: Path,
    proposed_code: str,
    project_root: Path,
    timeout: int = 30,
    speak=None,
    player=None,
    summary_prefix: str = "",
    change_note: str = "",
    reason: str = "",
    next_step: str = "",
) -> tuple[bool, str]:
    target_path = target_path.resolve()
    project_root = project_root.resolve()

    try:
        target_path.relative_to(project_root)
    except ValueError:
        return False, "Target file is outside the project root."

    sandbox_dir = Path(tempfile.mkdtemp(prefix="jarvis-sandbox-", dir=str(project_root.parent)))

    try:
        shutil.copytree(
            project_root,
            sandbox_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".git",
                ".pytest_cache",
                ".mypy_cache",
                ".venv",
                "venv",
                "node_modules",
            ),
        )

        sandbox_target = sandbox_dir / target_path.relative_to(project_root)
        sandbox_target.write_text(proposed_code, encoding="utf-8")

        if player:
            report = _build_change_report(
                target_path,
                change_note or summary_prefix,
                reason or None,
                next_step or None,
                base_path=project_root,
                stage="Planning change",
            )
            player.show_content("Jarvis edit", report)
            player.show_content(
                "Sandbox workspace",
                f"Validation workspace: {sandbox_dir}\n\nTarget file: {sandbox_target}\n\nStatus: preparing sandbox copy"
            )
        if speak:
            short_note = change_note or summary_prefix or "self-improvement"
            speak(
                f"Validation is running automatically for {target_path.relative_to(project_root)} to {short_note}. "
                "No manual confirmation is required, sir."
            )

        try:
            _open_in_vscode(sandbox_dir)
        except Exception:
            pass

        ok, details = _run_sandbox_validation(sandbox_dir, sandbox_target, timeout=timeout)
        if not ok:
            return False, f"Sandbox validation failed: {details}"

        if player:
            player.show_content(
                "Jarvis edit",
                f"Applying validated change to workspace: {target_path.relative_to(project_root)}"
            )
            player.show_content(
                "Sandbox workspace",
                f"Validation workspace: {sandbox_dir}\n\nTarget file: {sandbox_target}\n\nStatus: sandbox validation passed"
            )
            player.write_log(f"[DevAgent] Applying validated change to {target_path.relative_to(project_root)}")
        if speak:
            speak(f"I am applying the validated change to {target_path.relative_to(project_root)} now.")

        target_path.write_text(proposed_code, encoding="utf-8")
        _git_commit_file(
            target_path, project_root,
            f"Jarvis: {target_path.relative_to(project_root)} — {change_note[:60]}",
        )
        if player:
            player.write_log(f"[DevAgent] {summary_prefix}Updated {target_path.relative_to(project_root)}")
            player.show_content(
                "Jarvis edit",
                f"Applied to workspace: {target_path.relative_to(project_root)}\n\nValidated in sandbox first."
            )
            player.show_content(
                "Sandbox workspace",
                f"Validation workspace: {sandbox_dir}\n\nTarget file: {sandbox_target}\n\nStatus: change applied to workspace"
            )
        if speak:
            speak(f"I updated {target_path.relative_to(project_root)} and verified it in the sandbox.")
        try:
            _open_in_vscode(target_path)
        except Exception:
            pass
        return True, f"Sandbox validation passed. Applied update to {target_path.relative_to(project_root)}."
    except Exception as exc:
        return False, f"Sandbox validation failed: {exc}"
    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)


def _self_improve_project(
    description: str,
    target_files: list[str] | None = None,
    timeout: int = 30,
    self_reboot: bool = True,
    speak=None,
    player=None,
) -> str:
    repo_root = BASE_DIR.resolve()
    selected_files = _normalize_self_improvement_targets(repo_root, target_files)
    if not selected_files:
        return "No editable Python files were found for self-improvement, sir."

    plan = _build_self_improvement_plan(description, selected_files, speak=speak)
    improvements = plan.get("improvements") or []
    if not improvements:
        return "No valid self-improvement updates were generated."

    log_lines: list[str] = []
    applied_count = 0
    for item in improvements[:20]:
        rel_file = str(item.get("file", "") or "").strip()
        change = str(item.get("change", "") or "").strip()
        reason = str(item.get("reason", "") or "").strip()
        if not rel_file:
            continue

        target_path = _resolve_repo_path(rel_file, repo_root)
        if target_path is None or not target_path.exists() or target_path.suffix != ".py":
            log_lines.append(f"✗ {rel_file}: file unavailable")
            continue

        try:
            proposed_code = _generate_replacement_for_file(
                target_path,
                f"{change} ({reason})" if reason else change,
                "python",
            )
        except Exception as exc:
            log_lines.append(f"✗ {rel_file}: generation failed ({exc})")
            continue

        success, report = _sandbox_test_and_apply(
            target_path=target_path,
            proposed_code=proposed_code,
            project_root=repo_root,
            timeout=timeout,
            speak=speak,
            player=player,
            summary_prefix=f"{change}: ",
            change_note=change,
            reason=reason,
            next_step="Try the updated behavior and request another pass if needed.",
        )
        if success:
            log_lines.append(f"✓ {rel_file} — {report}")
            applied_count += 1
        else:
            log_lines.append(f"✗ {rel_file} — {report}")

    summary = "\n".join(log_lines) if log_lines else "No file changes were applied."
    if self_reboot and applied_count > 0:
        summary = f"{summary}\n\n{REBOOT_MARKER}"
    if player:
        player.write_log(f"[DevAgent] Self-improvement summary:\n{summary}")
    return summary


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:
    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:
    low = output.lower()

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


def _extract_exit_code(output: str) -> int | None:
    m = re.search(r"^EXIT_CODE:\s*(-?\d+)\s*$", output, re.MULTILINE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _has_error(output: str, run_command: str) -> bool:
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    exit_code = _extract_exit_code(output)
    error_type = _classify_error(output)

    if exit_code is not None and exit_code != 0:
        return True

    return error_type != "none"


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(_extract_text(response))
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raw_snippet = getattr(response, "text", "")[:300] if "response" in locals() else ""
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {raw_snippet}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise


def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(_extract_text(response))

        full_path = project_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")
        _git_commit_file(full_path, project_dir, f"Jarvis: generate {file_path}")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise


def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    if not dependencies:
        return "No external dependencies."

    to_install = []
    for dep in dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    if not to_install:
        return f"All dependencies already installed: {', '.join(dependencies)}"

    print(f"[DevAgent] 📦 Installing: {to_install}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(project_dir)
        )
        if result.returncode == 0:
            return f"Installed: {', '.join(to_install)}"
        return f"Install warning (non-fatal): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Dependency install timed out (non-fatal)."
    except Exception as e:
        return f"Install error (non-fatal): {e}"


def _open_vscode(project_dir: Path) -> bool:
    vscode_candidates = [
        "code",
        rf"C:\Users\{Path.home().name}\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
    ]
    for cmd in vscode_candidates:
        try:
            subprocess.Popen(
                [cmd, str(project_dir)],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.5)
            print(f"[DevAgent] 💻 VSCode opened: {project_dir}")
            return True
        except Exception:
            continue
    return False


def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    print(f"[DevAgent] 🚀 Running: {run_command}")
    try:
        parts = run_command.split()
        if parts[0].lower() == "python":
            parts[0] = sys.executable

        result = subprocess.run(
            parts,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(project_dir)
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        combined_parts = []
        combined_parts.append(f"EXIT_CODE: {result.returncode}")
        if stdout:
            combined_parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            combined_parts.append(f"STDERR:\n{stderr}")

        return "\n\n".join(combined_parts) if combined_parts else "Ran with no output."

    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s — long-running app (server/GUI) is likely working."
    except FileNotFoundError as e:
        return f"Command not found: {e}"
    except Exception as e:
        return f"Run error: {e}"


def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    pattern = re.compile(
        r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE
    )
    match = pattern.search(error_output)
    if not match:
        return False

    pkg = match.group(1).replace("_", "-").split(".")[0]
    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False


def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:
    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(_extract_text(response))

            full_path = project_dir / fix_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")
            _git_commit_file(full_path, project_dir, f"Jarvis: fix {fix_path}")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes


def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:
    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir  = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)
    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    if dependencies:
        install_result = _install_dependencies(dependencies, project_dir)
        log(install_result)

    _open_vscode(project_dir)

    last_output   = ""
    auto_installs = 0  

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        last_output = _run_project(run_command, project_dir, timeout)
        log(f"Output preview: {last_output[:150]}")

        if not _has_error(last_output, run_command):
            msg = (
                f"Project '{proj_name}' is working, sir. "
                f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to: {project_dir}"
            )
            if speak: speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(last_output)
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        log(f"Fixing errors (type: {error_type})...")
        try:
            updated = _fix_files(
                error_output=last_output,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast error:\n{last_output[:600]}"


def _integrate_project(
    project_name: str,
    description: str = "",
    require_approval: bool = True,
    speak=None,
    player=None,
) -> str:
    project_dir = PROJECTS_DIR / project_name
    if not project_dir.exists():
        available = [d.name for d in PROJECTS_DIR.iterdir() if d.is_dir()] if PROJECTS_DIR.exists() else []
        return f"Project '{project_name}' not found. Available: {available or 'none'}"

    src_files: dict[str, str] = {}
    for f in sorted(project_dir.rglob("*.py")):
        rel = f.relative_to(project_dir)
        if "__pycache__" in str(rel):
            continue
        try:
            src_files[str(rel)] = f.read_text(encoding="utf-8")
        except Exception:
            pass

    if not src_files:
        return f"No Python source files found in '{project_name}'."

    repo_root = BASE_DIR.resolve()
    jarvis_files = ["main.py", "core/llm_client.py", "core/tts.py"]
    jarvis_snippets: list[str] = []
    for jf in jarvis_files:
        fp = repo_root / jf
        if fp.exists():
            jarvis_snippets.append(f"=== {jf} (first 2000 chars) ===\n{fp.read_text(encoding='utf-8')[:2000]}")

    project_src  = "\n\n".join(f"=== {rel} ===\n{code}" for rel, code in src_files.items())
    jarvis_src   = "\n\n".join(jarvis_snippets)

    prompt = f"""You are a senior Python architect deciding how to integrate an external module into JARVIS (Mark-L AI assistant).

PROJECT: {project_name}
DESCRIPTION: {description or '(infer from source)'}

PROJECT SOURCE:
{project_src}

JARVIS CODEBASE EXCERPTS:
{jarvis_src}

Return a JSON object with this exact structure:
{{
  "improvements": [
    {{
      "file": "repo-relative/path.py",
      "change": "Concise description of what to add or modify in this file to integrate the project",
      "reason": "Why this change enables the integration"
    }}
  ]
}}

Rules:
- Only include files that actually exist in the JARVIS repo shown above.
- Keep changes minimal and additive — don't rewrite files, just wire in the new module.
- Import paths should reference core.context_optimizer or the module's new location.
- Return ONLY valid JSON."""

    if speak:
        speak(f"Analysing {project_name} for integration, sir.")
    if player:
        player.write_log(f"[DevAgent] Planning integration for: {project_name}")

    try:
        model   = _get_model(MODEL_WRITER)
        raw     = _strip_fences(_extract_text(model.generate_content(prompt)))
        plan_d  = json.loads(raw)
        improvements = plan_d.get("improvements", [])
    except Exception as exc:
        return f"Failed to generate integration plan: {exc}"

    normalized: list[dict] = []
    for item in improvements:
        rel   = str(item.get("file", "")).strip()
        change = str(item.get("change", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not rel:
            continue
        tp = _resolve_repo_path(rel, repo_root)
        if tp is None or not tp.exists() or tp.suffix != ".py":
            continue
        normalized.append({"file": str(tp.relative_to(repo_root)), "change": change, "reason": reason})

    if not normalized:
        return f"No valid JARVIS files identified for integration of '{project_name}'."

    approval_id = uuid.uuid4().hex[:10]
    payload = {
        "approval_id": approval_id,
        "created_at":  datetime.utcnow().isoformat() + "Z",
        "description": f"Integrate {project_name}: {description or 'auto-detected purpose'}",
        "target_files": [i["file"] for i in normalized],
        "plan":         {"improvements": normalized, "feature_suggestions": []},
    }
    store   = _load_approvals_store()
    pending = store.get("pending", [])
    pending.append(payload)
    store["pending"] = pending[-20:]
    _save_approvals_store(store)

    lines = [
        f"Integration plan for '{project_name}' — Approval ID: {approval_id}",
        "Changes (sandbox-tested before applying):",
    ]
    for item in normalized:
        lines.append(f"  • {item['file']}: {item['change']}")
    lines.append(f"\nSay: 'apply dev_agent approval {approval_id}' to wire it in.")

    result = "\n".join(lines)
    if player:
        player.show_content(f"Integration: {project_name}", result)
    if speak:
        speak(f"Integration plan ready, sir. Approval ID {approval_id}. Each change will be sandbox-tested before touching your live code.")
    return result


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))
    target_files = p.get("target_files") or []
    self_improve = bool(p.get("self_improve", False))
    integrate_project = bool(p.get("integrate_project", False))
    approval_action = str(p.get("approval_action", "") or "").strip().lower()
    approval_id = str(p.get("approval_id", "") or "").strip()
    require_approval = bool(p.get("require_approval", True))
    self_reboot = bool(p.get("self_reboot", True))

    if isinstance(target_files, str):
        target_files = [item.strip() for item in target_files.split(",") if item.strip()]

    lowered = description.lower()
    is_self_improve = self_improve or (
        "self" in lowered and ("improve" in lowered or "modify" in lowered or "source" in lowered)
    ) or (
        "source code" in lowered and ("improve" in lowered or "modify" in lowered)
    ) or (
        "security" in lowered and ("voice" in lowered or "visual" in lowered or "recognition" in lowered or "person" in lowered)
    )

    if integrate_project or "integrate" in lowered:
        name = project_name or description.replace("integrate", "").strip()
        return _integrate_project(
            project_name=name,
            description=description,
            require_approval=require_approval,
            speak=speak,
            player=player,
        )

    if is_self_improve and approval_action in {"status", "clear", "apply"}:
        if approval_action == "status":
            return _approval_status()
        if approval_action == "clear":
            return _clear_approvals(approval_id or None)
        return _apply_queued_self_improvement(
            approval_id=approval_id or None,
            timeout=timeout,
            self_reboot=self_reboot,
            speak=speak,
            player=player,
        )

    if not description:
        description = "Upgrade JARVIS security protocols to include biometric voice recognition and visual person detection for enhanced security and personalization. Optimize overall system performance and refactor core modules for improved efficiency and reduced RAM footprint, incorporating insights from recent system diagnostics and adhering to Stark-style technical specifications."
        is_self_improve = True

    if is_self_improve:
        if approval_action == "propose":
            return _queue_self_improvement_plan(
                description=description,
                target_files=target_files,
                speak=speak,
            )

        if require_approval:
            return _queue_self_improvement_plan(
                description=description,
                target_files=target_files,
                speak=speak,
            )

        return _self_improve_project(
            description=description,
            target_files=target_files,
            timeout=timeout,
            self_reboot=self_reboot,
            speak=speak,
            player=player,
        )

    return _build_project(
        description  = description,
        language     = language,
        project_name = project_name,
        timeout      = timeout,
        speak        = speak,
        player       = player,
    )