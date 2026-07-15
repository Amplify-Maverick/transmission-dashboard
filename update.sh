#!/bin/bash
# Apply pushed changes on the server: pull, reinstall deps, restart.
#
# Safe to run any time — your config, database and runtime state
# (.env, .flask_secret, dashboard.db*, media_config.json, copy_state.json,
# scan_state.json) are all gitignored, so `git pull` never touches them.
set -euo pipefail

# Run from the repo root regardless of where this is invoked from.
cd "$(dirname "$(readlink -f "$0")")"

SERVICE="transmission-dashboard"

echo "==> Updating $(pwd)"

if [ ! -d .git ]; then
    echo "Error: this is not a git checkout — nothing to pull." >&2
    exit 1
fi

echo "==> git pull --ff-only"
# Fast-forward only: if local commits have diverged, stop rather than create
# a merge — the operator should sort that out by hand.
git pull --ff-only

echo "==> Installing dependencies into .venv"
if [ ! -x .venv/bin/pip ]; then
    echo "    (creating virtualenv at .venv)"
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# Restart the service if the systemd unit is installed; otherwise tell the
# operator to restart however they run it.
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}\.service"; then
    echo "==> Restarting ${SERVICE} service"
    sudo systemctl restart "${SERVICE}"
    echo "==> Done. Tail logs with: journalctl -u ${SERVICE} -f"
else
    echo "==> Update complete."
    echo "    No '${SERVICE}' systemd unit found — restart the app manually"
    echo "    (e.g. kill and re-run gunicorn) to pick up the new code."
fi
