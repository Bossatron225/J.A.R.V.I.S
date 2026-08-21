import os
from datetime import datetime
from pathlib import Path


_SECTIONS = {
    "identity": "## Identity",
    "preferences": "## Preferences",
    "projects": "## Projects / Goals",
    "relationships": "## Relationships",
    "wishes": "## Wishes / Plans",
    "notes": "## Notes",
}


def _vault_path() -> Path:
    configured = os.getenv("JARVIS_OBSIDIAN_VAULT_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.path.expanduser("~/Documents/JarvisMemory")).resolve()


def _profile_file() -> Path:
    return _vault_path() / "USER_PROFILE.md"


def _normalize_category(category: str) -> str:
    cleaned = (category or "notes").strip().lower()
    aliases = {
        "identity": "identity",
        "profile": "identity",
        "preference": "preferences",
        "preferences": "preferences",
        "project": "projects",
        "projects": "projects",
        "goal": "projects",
        "goals": "projects",
        "relationship": "relationships",
        "relationships": "relationships",
        "wish": "wishes",
        "wishes": "wishes",
        "plan": "wishes",
        "plans": "wishes",
        "note": "notes",
        "notes": "notes",
    }
    return aliases.get(cleaned, "notes")


def _ensure_header(profile_file: Path) -> None:
    if profile_file.exists():
        return
    header = (
        "# Jarvis Long-Term Memory\n\n"
        "This file is maintained by Jarvis and reflects durable facts about the user.\n"
        "The detailed history is preserved below, and a compact dashboard summary stays at the top.\n\n"
        "## Dashboard\n"
        "- Current priorities: capture durable preferences, plans, and important relationships.\n"
        "- Recent facts: add new details here as they become relevant.\n"
        "- Full history: preserved in the detailed sections below.\n\n"
        "## Summary\n"
        "- Keep entries concise and factual.\n"
        "- Preserve the full history in the detailed sections below.\n\n"
    )
    profile_file.write_text(header, encoding="utf-8")


def _update_summary(profile_file: Path, category: str, new_fact: str) -> None:
    text = profile_file.read_text(encoding="utf-8")
    if "## Summary" not in text:
        return
    summary_block = "## Summary"
    if summary_block not in text:
        return

    lines = text.splitlines()
    summary_idx = next((idx for idx, line in enumerate(lines) if line.strip() == summary_block), None)
    if summary_idx is None:
        return

    bullet = f"- [{category}] {new_fact}"
    existing_summary = [line for line in lines[summary_idx + 1:] if line.strip() and not line.startswith("## ")]
    filtered = [line for line in existing_summary if not line.startswith(f"- [{category}] ")]
    filtered.insert(0, bullet)
    if len(filtered) > 8:
        filtered = filtered[:8]

    new_lines = []
    for idx, line in enumerate(lines):
        if idx == summary_idx:
            new_lines.append(summary_block)
            new_lines.extend(filtered)
        else:
            new_lines.append(line)

    profile_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _looks_similar(existing: str, new_fact: str) -> bool:
    cleaned_new = " ".join((new_fact or "").lower().split())
    if not cleaned_new:
        return True
    for line in existing.splitlines():
        if "**[" not in line:
            continue
        body = line.split("**", 2)[-1].strip()
        if not body:
            continue
        if cleaned_new in body.lower():
            return True
    return False


def remember_user_fact(fact_category: str, new_fact: str) -> str:
    """Append a structured fact to the local Obsidian memory vault."""
    vault_path = _vault_path()
    profile_file = _profile_file()
    try:
        vault_path.mkdir(parents=True, exist_ok=True)
        _ensure_header(profile_file)
        category = _normalize_category(fact_category)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        existing = profile_file.read_text(encoding="utf-8")
        section_header = _SECTIONS[category]
        if section_header not in existing:
            existing = existing.rstrip() + f"\n\n{section_header}\n"
            profile_file.write_text(existing, encoding="utf-8")

        lines = existing.splitlines()
        if _looks_similar("\n".join(lines), new_fact):
            return f"Obsidian memory already contains a similar fact under {category}."

        entry = f"- **[{timestamp}]** {new_fact}"
        with profile_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{entry}")
        _update_summary(profile_file, category, new_fact)
        return f"Successfully saved to Obsidian memory under {category}."
    except Exception as exc:  # pragma: no cover - defensive path
        return f"Failed to write to memory: {exc}"


def build_personal_memory_context(json_memory: object, obsidian_profile: object) -> str:
    """Combine JSON-backed memory and Obsidian-backed memory into one prompt block."""
    parts: list[str] = []

    if isinstance(json_memory, dict):
        from memory.memory_manager import format_memory_for_prompt

        json_text = format_memory_for_prompt(json_memory).strip()
    else:
        json_text = str(json_memory or "").strip()

    if json_text:
        parts.append(json_text.rstrip())

    obsidian_text = str(obsidian_profile or "").strip()
    if obsidian_text and "No permanent long-term profile data" not in obsidian_text and "Failed to read memory file" not in obsidian_text:
        parts.append("[LOCAL OBSIDIAN MEMORY]\n" + obsidian_text.strip())

    if not parts:
        return ""

    return "\n\n".join(parts).rstrip() + "\n"


def recall_personal_memory(query: str) -> str:
    """Search every memory store by meaning.

    Previously this substring-matched against format_memory_for_prompt()'s
    output, which is capped for prompt injection — so any fact past that cap
    was unreachable, and a question phrased differently from the stored key
    ("who is my partner" vs `girlfriend_name`) found nothing. Now delegates to
    semantic_recall.search_memory(), which ranks the FULL stores by meaning and
    falls back to substring matching when embeddings are unavailable."""
    query_text = (query or "").strip()
    if not query_text:
        return "No query provided."

    try:
        from memory.semantic_recall import format_recall, search_memory
        return format_recall(query_text, search_memory(query_text))
    except Exception as exc:
        # Never let recall hard-fail — fall back to the old direct scan.
        from memory.memory_manager import format_memory_for_prompt, load_memory

        lowered = query_text.lower()
        hits: list[str] = []
        json_text = format_memory_for_prompt(load_memory()).strip()
        obsidian_text = recall_user_profile().strip()
        if json_text and lowered in json_text.lower():
            hits.append("JSON memory: " + json_text[:700].strip())
        if obsidian_text and lowered in obsidian_text.lower():
            hits.append("Obsidian memory: " + obsidian_text[:700].strip())
        if not hits:
            return f"No matching memory found for '{query}' ({exc})."
        return "\n\n".join(hits[:2])


def recall_user_profile() -> str:
    """Read the permanent user profile from the local Obsidian vault."""
    profile_file = _profile_file()
    if not profile_file.exists():
        return "No permanent long-term profile data exists yet."
    try:
        return profile_file.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive path
        return f"Failed to read memory file: {exc}"
