#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

if [ -f ".venv/bin/python" ]; then
    .venv/bin/python scripts/backup.py
elif command -v python3 &>/dev/null; then
    python3 scripts/backup.py
else
    python scripts/backup.py
fi
