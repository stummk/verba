#!/usr/bin/env bash
# Start Verba (desktop: ./start.sh — server: ./start.sh --server)
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ was not found. Please install it via your package manager." >&2
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment ..."
    "$PYTHON" -m venv .venv
fi

# run.py installs missing core dependencies itself
exec .venv/bin/python run.py "$@"
