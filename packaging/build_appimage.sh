#!/usr/bin/env bash
# Build the Verba Linux desktop AppImage from the PyInstaller one-dir output.
# Run from the project root (CI does this after `pyinstaller packaging/verba.spec`):
#
#   ./packaging/build_appimage.sh 1.0.0
#
# Result: dist/Verba-<version>-x86_64.AppImage — double-click to run, the
# .desktop entry and icon integrate via the user's AppImage launcher.

set -euo pipefail

VERSION="${1:-0.0.0}"
DIST="dist/verba"
APPDIR="dist/Verba.AppDir"

[[ -d "$DIST" ]] || { echo "dist/verba missing — run pyinstaller packaging/verba.spec first" >&2; exit 1; }

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r "$DIST"/. "$APPDIR/usr/bin/"
cp packaging/verba.desktop "$APPDIR/verba.desktop"
cp packaging/verba-256.png "$APPDIR/verba.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/Verba" "$@"
EOF
chmod +x "$APPDIR/AppRun"

if [[ ! -x appimagetool ]]; then
    curl -fsSL -o appimagetool \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x appimagetool
fi

ARCH=x86_64 ./appimagetool --no-appstream "$APPDIR" "dist/Verba-${VERSION}-x86_64.AppImage"
echo "AppImage: dist/Verba-${VERSION}-x86_64.AppImage"
