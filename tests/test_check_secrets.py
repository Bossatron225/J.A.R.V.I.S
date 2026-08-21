"""Tests for the credential scanner.

check-secrets: allow-fixtures
  ^ This file intentionally contains credential-SHAPED strings (all fake) to
    prove the scanner catches them; without this marker it would flag itself.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = REPO_ROOT / "scripts" / "check_secrets.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_secrets  # noqa: E402


def _scan(tmp_path, name: str, content: str) -> list[str]:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return check_secrets.scan_file(path)


def test_detects_elevenlabs_key(tmp_path):
    findings = _scan(tmp_path, "cfg.json", '{"elevenlabs_api_key": "sk_' + "a1b2c3d4" * 6 + '"}')
    assert any("ElevenLabs" in f for f in findings)


def test_detects_google_oauth_style_key(tmp_path):
    findings = _scan(tmp_path, "cfg.json", '{"gemini_api_key": "AQ.Ab8RN6' + "X" * 40 + '"}')
    assert any("Google" in f for f in findings)


def test_detects_gemini_aiza_key(tmp_path):
    findings = _scan(tmp_path, "cfg.json", '{"key": "AIza' + "B" * 35 + '"}')
    assert any("Gemini" in f for f in findings)


def test_detects_token_assigned_to_credential_named_field(tmp_path):
    findings = _scan(tmp_path, "cfg.json", '{"local_worker_token": "' + "9b0a9254" * 8 + '"}')
    assert any("credential-named" in f for f in findings)


def test_detects_private_key_block(tmp_path):
    findings = _scan(tmp_path, "id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert any("Private key" in f for f in findings)


def test_bare_hex_is_not_flagged(tmp_path):
    """A 40-hex git SHA or file digest with no credential-ish key name next to
    it must not trip the scanner — otherwise it cries wolf on ordinary code
    and gets ignored or disabled."""
    findings = _scan(tmp_path, "notes.md", "commit 5f45f214ac6f129bd337e901ee839745314f67f9 fixed it")
    assert findings == []


def test_ordinary_source_file_is_clean(tmp_path):
    findings = _scan(tmp_path, "mod.py", "def add(a, b):\n    return a + b\n")
    assert findings == []


def test_binary_and_model_files_are_skipped(tmp_path):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"\x00\x01\x02" + b"sk_" + b"a" * 60)
    assert check_secrets.scan_file(path) == []


def test_scanner_does_not_flag_itself():
    """The scanner's own regexes look like the things they match."""
    assert check_secrets.scan_file(SCANNER) == []


def test_whole_tracked_repo_is_clean():
    """Regression guard for the real incident: a config backup containing live
    keys was committed because .gitignore only matched the exact filename."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=True,
    ).stdout.split()
    result = subprocess.run(
        [sys.executable, str(SCANNER), *tracked],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Secrets found in tracked files:\n{result.stderr}"


def test_cli_exits_nonzero_when_secret_present(tmp_path):
    bad = tmp_path / "leak.json"
    bad.write_text('{"elevenlabs_api_key": "sk_' + "f0e1d2c3" * 6 + '"}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCANNER), str(bad)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
