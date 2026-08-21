#!/usr/bin/env python3
"""Refuse to commit files that contain credential-shaped strings.

Written after a timestamped backup of config/api_keys.json (created by some
maintenance step, and NOT matched by the bare `config/api_keys.json` ignore
rule) was swept into a commit by the auto-commit watcher's `git add -A`,
putting a live Gemini key, ElevenLabs key, and worker token into git history.
Broadening .gitignore fixes that one filename; this catches the whole class,
including files nobody thought to add a rule for.

Usage:
    python scripts/check_secrets.py                # scan files staged in git
    python scripts/check_secrets.py FILE [FILE...] # scan specific paths

Exit code 0 = clean, 1 = secrets found (callers should refuse to commit).
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each pattern targets a credential format actually used by this project, so a
# hit is a real finding rather than a generic high-entropy guess.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Google/Gemini API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Google OAuth-style key", re.compile(r"\bAQ\.[0-9A-Za-z_\-]{30,}\b")),
    ("ElevenLabs API key", re.compile(r"\bsk_[0-9a-f]{40,}\b")),
    ("OpenAI-style API key", re.compile(r"\bsk-[0-9A-Za-z]{32,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# A 64-hex worker/shared token is only a finding when it sits next to a
# credential-ish key name — bare 64-hex also matches git SHAs, file digests,
# and model checksums, which would make this scanner cry wolf constantly.
TOKEN_NEAR_KEYNAME = re.compile(
    r"[\"']?\w*(?:token|secret|password|passwd|api_key)\w*[\"']?\s*[:=]\s*[\"'][0-9a-fA-F]{32,}[\"']",
    re.IGNORECASE,
)

# Binary/model/media files can trip hex patterns by chance and are never where
# credentials live in this project.
SKIP_SUFFIXES = {
    ".onnx", ".pth", ".index", ".jpg", ".jpeg", ".png", ".gif", ".ico",
    ".wav", ".mp3", ".mp4", ".pyc", ".pdf", ".zip", ".xml",
}

# Deliberate escape hatch for files whose *purpose* is to contain
# credential-shaped strings — i.e. this scanner's own tests. Modelled on
# `# noqa` / `# nosec`: it takes a visible, reviewable line in the file, so it
# cannot be applied accidentally, and a real config file gaining this marker
# would stand out immediately in a diff.
ALLOW_MARKER = "check-secrets: allow-fixtures"


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def scan_file(path: Path) -> list[str]:
    """Return a list of human-readable findings for one file."""
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    # Never scan this scanner: its own patterns would match themselves.
    try:
        if path.resolve() == Path(__file__).resolve():
            return []
    except OSError:
        pass

    try:
        raw = path.read_bytes()
    except (OSError, IsADirectoryError):
        return []
    if _looks_binary(raw):
        return []

    text = raw.decode("utf-8", errors="replace")
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    if TOKEN_NEAR_KEYNAME.search(text):
        findings.append("Token/secret assigned to a credential-named field")
    return findings


def staged_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / line.strip() for line in out.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]] if len(argv) > 1 else staged_files()
    paths = [p for p in paths if p.is_file()]

    offenders: dict[str, list[str]] = {}
    for path in paths:
        findings = scan_file(path)
        if findings:
            try:
                rel = str(path.relative_to(REPO_ROOT))
            except ValueError:
                rel = str(path)
            offenders[rel] = findings

    if not offenders:
        return 0

    print("BLOCKED: credential-shaped strings found — refusing to commit.", file=sys.stderr)
    for rel, findings in offenders.items():
        print(f"  {rel}: {', '.join(findings)}", file=sys.stderr)
    print(
        "\nIf this is a real secret: remove the file from staging "
        "(git rm --cached <file>) and add it to .gitignore.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
