#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_SECONDS = 10


def run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def ensure_git_identity():
    try:
        run(["git", "config", "user.name"], cwd=REPO_ROOT, check=False)
    except subprocess.CalledProcessError:
        pass

    name = run(["git", "config", "--get", "user.name"], cwd=REPO_ROOT, check=False).stdout.strip()
    email = run(["git", "config", "--get", "user.email"], cwd=REPO_ROOT, check=False).stdout.strip()
    if not name:
        run(["git", "config", "user.name", "Jarvis Auto Commit"], cwd=REPO_ROOT)
    if not email:
        run(["git", "config", "user.email", "jarvis-auto@local"], cwd=REPO_ROOT)


def main():
    if not (REPO_ROOT / ".git").exists():
        print("No git repository found.", file=sys.stderr)
        sys.exit(1)

    ensure_git_identity()
    print(f"Auto-commit watcher started for {REPO_ROOT}")
    last_status = ""

    while True:
        try:
            status = run(["git", "status", "--porcelain"], cwd=REPO_ROOT).stdout
            if status.strip():
                if status != last_status:
                    run(["git", "add", "-A"], cwd=REPO_ROOT)
                    message = f"chore: auto-commit {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    commit_result = run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=False)
                    if commit_result.returncode == 0:
                        print(f"Committed changes: {message}")
                    else:
                        print(commit_result.stderr.strip() or commit_result.stdout.strip())
                    
                    # Push to origin (GitHub)
                    push_origin = run(["git", "push", "origin", "HEAD"], cwd=REPO_ROOT, check=False)
                    if push_origin.returncode == 0:
                        print("Pushed to origin (GitHub)")
                    else:
                        print(f"Origin push failed: {push_origin.stderr.strip()}")
                        
                    # Push to vps if remote exists
                    remotes = run(["git", "remote"], cwd=REPO_ROOT, check=False).stdout.split()
                    if "vps" in remotes:
                        push_vps = run(["git", "push", "vps", "HEAD"], cwd=REPO_ROOT, check=False)
                        if push_vps.returncode == 0:
                            print("Pushed to vps (VPS server)")
                        else:
                            print(f"VPS push failed: {push_vps.stderr.strip()}")

                    last_status = status
                else:
                    time.sleep(POLL_SECONDS)
                    continue
            else:
                last_status = ""
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Auto-commit watcher stopped.")
            break
        except Exception as exc:
            print(f"Auto-commit watcher error: {exc}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
