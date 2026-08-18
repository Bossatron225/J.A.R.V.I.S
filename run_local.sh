#!/bin/bash
# Launch the full local interactive JARVIS (mic, wake word, GUI) on this Mac.
# Still linked to the VPS (memory sync, remote task servicing) via JARVIS_VPS_URL.
# Preempts the always-on background worker (JarvisWorker.app) if it's holding the
# lock; launchd brings the worker back automatically once this session exits.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export JARVIS_MODE=interactive
export JARVIS_VPS_URL="${JARVIS_VPS_URL:-http://161.35.38.152:8000}"

exec "./.venv-1/bin/python" "main.py"
