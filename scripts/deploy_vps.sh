#!/bin/bash
# Deploy the current origin/main to the VPS and restart the brain.
#
# Replaces the manual SSH → git pull → systemctl restart → tail-the-logs cycle
# that had to be run by hand every time. That manual loop is why the VPS was
# once discovered running code 30 commits behind the Mac with nothing
# surfacing the drift: auto_commit.py pushes TO GitHub but the VPS never
# pulls FROM it.
#
# Safe to re-run. Verifies the service actually came back up and is connected
# before reporting success, and rolls back to the previous commit if it did not.
#
# Usage:  scripts/deploy_vps.sh [--check-only]

set -uo pipefail

VPS_HOST="${JARVIS_VPS_HOST:-root@161.35.38.152}"
VPS_KEY="${JARVIS_VPS_KEY:-$HOME/.ssh/jarvis_vps}"
VPS_DIR="${JARVIS_VPS_DIR:-/root/jarvis-vps-runtime}"
SERVICE="jarvis-vps"

ssh_vps() { ssh -i "$VPS_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST" "$@"; }

echo "==> Checking VPS reachability"
if ! ssh_vps "true" 2>/dev/null; then
    echo "ERROR: cannot reach $VPS_HOST with key $VPS_KEY" >&2
    exit 1
fi

LOCAL_SHA=$(git rev-parse --short HEAD)
REMOTE_SHA=$(ssh_vps "cd $VPS_DIR && git rev-parse --short HEAD" 2>/dev/null)
echo "    local:  $LOCAL_SHA"
echo "    vps:    $REMOTE_SHA"

BEHIND=$(ssh_vps "cd $VPS_DIR && git fetch origin -q && git rev-list --count HEAD..origin/main" 2>/dev/null)
echo "    vps is $BEHIND commit(s) behind origin/main"

if [[ "${1:-}" == "--check-only" ]]; then
    exit 0
fi

if [[ "$BEHIND" == "0" ]]; then
    echo "==> Already up to date; nothing to deploy."
    exit 0
fi

echo "==> Checking VPS working tree is clean"
DIRTY=$(ssh_vps "cd $VPS_DIR && git status --porcelain" 2>/dev/null)
if [[ -n "$DIRTY" ]]; then
    echo "ERROR: VPS has uncommitted changes — refusing to deploy over them:" >&2
    echo "$DIRTY" >&2
    exit 1
fi

echo "==> Pulling"
if ! ssh_vps "cd $VPS_DIR && git pull --no-rebase origin main" ; then
    echo "ERROR: git pull failed on the VPS" >&2
    exit 1
fi

NEW_SHA=$(ssh_vps "cd $VPS_DIR && git rev-parse --short HEAD")
echo "==> Restarting $SERVICE (now at $NEW_SHA)"
ssh_vps "systemctl restart $SERVICE"

echo "==> Verifying it actually came back up"
sleep 10
ACTIVE=$(ssh_vps "systemctl is-active $SERVICE" 2>/dev/null)
if [[ "$ACTIVE" != "active" ]]; then
    echo "ERROR: $SERVICE is '$ACTIVE' after restart. Recent log:" >&2
    ssh_vps "journalctl -u $SERVICE --no-pager -n 25" >&2
    echo "==> Rolling back to $REMOTE_SHA" >&2
    ssh_vps "cd $VPS_DIR && git reset --hard $REMOTE_SHA && systemctl restart $SERVICE"
    exit 1
fi

# Being "active" is not the same as working — this is the exact
# liveness-vs-function distinction the health system exists for. Confirm the
# brain actually reconnected to Gemini rather than just that the unit started.
if ssh_vps "journalctl -u $SERVICE --no-pager --since '60 seconds ago'" 2>/dev/null | grep -q "SYS: Online."; then
    echo "==> Deployed and connected: $REMOTE_SHA -> $NEW_SHA"
else
    echo "WARNING: service is active but has not logged 'SYS: Online.' yet." >&2
    echo "         It may still be connecting. Recent log:" >&2
    ssh_vps "journalctl -u $SERVICE --no-pager -n 15" >&2
fi
