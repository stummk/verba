#!/usr/bin/env bash
# Verba headless server installation (Linux, no GUI required).
#
# Run from the unpacked server zip (or a source checkout):
#   sudo ./deploy/install.sh            # installs to /opt/verba
#   sudo ./deploy/install.sh /srv/verba # custom target directory
#
# What it does:
#   1. checks for Python 3.11+
#   2. copies the application to the target directory
#   3. creates a virtualenv and installs the core dependencies
#   4. creates the system user "verba" and installs the systemd unit
#
# Heavy components (ffmpeg, Whisper, search) are installed afterwards through
# the in-app first-run setup — open http://<server>:8710 once Verba runs.

set -euo pipefail

TARGET="${1:-/opt/verba}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo/as root (installation into $TARGET)." >&2
    exit 1
fi

PYTHON="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    echo "Python 3.11+ is required (e.g. apt install python3 python3-venv)." >&2
    exit 1
fi
"$PYTHON" - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ is required, found: {sys.version.split()[0]}")
EOF

echo "Installing Verba into $TARGET ..."
mkdir -p "$TARGET"

SERVICE_WAS_ACTIVE=0
if command -v systemctl &>/dev/null && systemctl is-active --quiet verba 2>/dev/null; then
    SERVICE_WAS_ACTIVE=1
    systemctl stop verba
fi

# Replace only application files. Keep the virtualenv, runtime data and user
# workspaces so rerunning this script upgrades an existing installation.
for item in backend frontend docs requirements deploy run.py start.sh README.md; do
    rm -rf "$TARGET/$item"
    cp -r "$SOURCE_DIR/$item" "$TARGET/$item"
done

echo "Creating virtual environment ..."
"$PYTHON" -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python" -m pip install --quiet --upgrade pip
"$TARGET/.venv/bin/python" -m pip install --quiet -r "$TARGET/requirements/core.txt"

if ! id verba &>/dev/null; then
    useradd --system --create-home --home-dir "$TARGET" --shell /usr/sbin/nologin verba
fi
mkdir -p "$TARGET/data" "$TARGET/workspaces"
chown -R verba:verba "$TARGET"

if command -v systemctl &>/dev/null; then
    sed "s|/opt/verba|$TARGET|g" "$TARGET/deploy/verba.service" > /etc/systemd/system/verba.service
    systemctl daemon-reload
    if [[ $SERVICE_WAS_ACTIVE -eq 1 ]]; then
        systemctl enable --now verba
    else
        systemctl enable verba
    fi
    echo
    echo "Verba is running: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8710"
    echo "Status:  systemctl status verba"
    echo "Logs:    journalctl -u verba -f   (app logs: $TARGET/data/logs/)"
else
    echo
    echo "No systemd found — start manually with:"
    echo "  sudo -u verba $TARGET/.venv/bin/python $TARGET/run.py --server"
fi
echo
echo "Reverse proxy examples: $TARGET/deploy/nginx-verba.conf and $TARGET/deploy/Caddyfile"
echo "First-run setup: open the web interface — missing components are installed there."
