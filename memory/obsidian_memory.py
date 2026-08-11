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
        "The detailed history is preserved below, and a compact summary is kept at the top.\n\n"
        "## Summary\n"
        "- Keep entries concise and factual.\n"
        "- Preserve the full history in the detailed sections below.\n\n"
    )
    profile_file.write_text(header, encoding="utf-8")


def _update_summary(profile_file: Path, category: str, new_fact: str) -> None:
    text = profile_file.read_text(encoding="utf-8")
    if "## Summary" not in text:
        return
    summary_block = "## Summary\n"
    if summary_block not in text:
        return
    intro, rest = text.split(summary_block, 1)
    lines = [line for line in rest.splitlines() if line.strip()]
    bullet = f"- [{category}] {new_fact}"
    if bullet in lines:
        return
    lines = [line for line in lines if not line.startswith(f"- [{category}] ")]
    lines.insert(0, bullet)
    if len(lines) > 12:
        lines = lines[:12]
    updated = intro + summary_block + "\n".join(lines) + "\n\n" + rest.splitlines()[0] if False else ""
    # Preserve the full history and keep a concise summary list at the top.
    summary_lines = ["## Summary"] + lines
    updated = intro + "\n\n" + "\n".join(summary_lines) + "\n\n" + rest.split("\n", 1)[1] if "\n" in rest else intro + "\n\n" + "\n".join(summary_lines) + "\n"
    profile_file.write_text(updated, encoding="utf-8")


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


def recall_user_profile() -> str:
    """Read the permanent user profile from the local Obsidian vault."""
    profile_file = _profile_file()
    if not profile_file.exists():
        return "No permanent long-term profile data exists yet."
    try:
        return profile_file.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive path
        return f"Failed to read memory file: {exc}"
