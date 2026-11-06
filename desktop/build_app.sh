#!/bin/sh
# Build the native macOS app: dist/Homelab Console.app
#
#   desktop/build_app.sh
#
# The app is a Swift shell around the Python backend. PyInstaller is a
# build-time dependency only; the console itself stays stdlib-only:
#   ./.venv/bin/python -m pip install "pyinstaller>=6.16"
#
# Signing:
#   HOMELAB_SIGN_IDENTITY="-"                  ad-hoc (default, local only)
#   HOMELAB_SIGN_IDENTITY="Homelab Console Dev" a self-signed cert (stable
#                                              identity; permissions persist)
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY=${HLC_PYTHON:-"$REPO/.venv/bin/python"}
# Allow HLC_PYTHON=python3 as well as an absolute interpreter path.
if [ ! -x "$PY" ] && command -v "$PY" >/dev/null 2>&1; then
    PY=$(command -v "$PY")
fi
IDENTITY=${HOMELAB_SIGN_IDENTITY:--}
# Neutral by default. Set HLC_BUNDLE_ID to your own reverse-DNS id (or to
# dev.awychoi.homelab-console to keep an existing Keychain item) when building.
BUNDLE_ID=${HLC_BUNDLE_ID:-dev.homelab-console.app}
APP_NAME="Homelab Console"
DIST="$REPO/dist"
APP="$DIST/$APP_NAME.app"
BUILD="$REPO/build"

if [ ! -x "$PY" ]; then
    echo "error: python not found at $PY (set HLC_PYTHON)" >&2
    exit 2
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1; then
    echo "error: Python 3.10 or newer is required to build the app" >&2
    exit 2
fi

if ! command -v xcrun >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "error: swiftc not found; install Xcode or the command line tools" >&2
    exit 2
fi

if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "error: PyInstaller is not installed for $PY" >&2
    echo "hint:  $PY -m pip install 'pyinstaller>=6.16'" >&2
    exit 2
fi

VERSION=$("$PY" - "$REPO" <<'PY'
import re, sys
from pathlib import Path
text = (Path(sys.argv[1]) / "app/homelab_console/config.py").read_text(encoding="utf-8")
match = re.search(r'^VERSION = "([^"]+)"', text, re.MULTILINE)
print(match.group(1) if match else "0.0.0")
PY
)

# Keep PyInstaller's cache inside build/: it otherwise writes to
# ~/Library/Application Support, which can be locked down.
export PYINSTALLER_CONFIG_DIR="$BUILD/pyinstaller-cache"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "==> backend ($("$PY" -V 2>&1))"
"$PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$DIST" \
    --workpath "$BUILD" \
    "$REPO/desktop/HomelabConsoleBackend.spec" >/dev/null

echo "==> Swift shell"
SDK=$(xcrun --sdk macosx --show-sdk-path)
MODULE_CACHE="$BUILD/swift-module-cache"
rm -rf "$BUILD/swift"
mkdir -p "$BUILD/swift" "$MODULE_CACHE"
xcrun swiftc \
    -O \
    -swift-version 5 \
    -target "$(uname -m)-apple-macos13.0" \
    -sdk "$SDK" \
    -module-cache-path "$MODULE_CACHE" \
    -framework Cocoa \
    -framework WebKit \
    "$REPO"/desktop/macos/Sources/*.swift \
    -o "$BUILD/swift/$APP_NAME"

[ -f "$REPO/desktop/macos/Resources/AppIcon.icns" ] || {
    echo "error: AppIcon.icns is missing; run: node desktop/make_icon.cjs" >&2
    exit 2
}
[ -f "$REPO/THIRD_PARTY_NOTICES.md" ] || {
    echo "error: THIRD_PARTY_NOTICES.md is missing" >&2
    exit 2
}
[ -d "$REPO/licenses" ] || {
    echo "error: licenses/ is missing" >&2
    exit 2
}

echo "==> bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/desktop/macos/Resources/Info.plist" "$APP/Contents/Info.plist"
# MIT requires the notice in every distributed copy, including the .app.
cp "$REPO/LICENSE" "$APP/Contents/Resources/LICENSE"
# The bundle embeds Python and native libraries; ship their notices with it.
cp "$REPO/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/THIRD_PARTY_NOTICES.md"
cp -R "$REPO/licenses" "$APP/Contents/Resources/licenses"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLName $BUNDLE_ID" "$APP/Contents/Info.plist" >/dev/null
cp "$BUILD/swift/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"
cp "$REPO/desktop/macos/Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$DIST/backend" "$APP/Contents/Resources/backend"

echo "==> build info"
"$PY" - "$APP/Contents/Resources/BUILD_INFO.txt" "$REPO" "$BUNDLE_ID" "$VERSION" <<'PY'
import datetime
import platform
import sqlite3
import ssl
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1])
repo = Path(sys.argv[2])
bundle_id = sys.argv[3]
version = sys.argv[4]
try:
    import PyInstaller

    pyinstaller = PyInstaller.__version__
except Exception:
    pyinstaller = "unknown"
try:
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip() or "unknown"
except Exception:
    commit = "unknown"
lines = [
    "Homelab Console build information",
    "=================================",
    f"app version: {version}",
    f"bundle id: {bundle_id}",
    f"built at: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}",
    f"source commit: {commit}",
    f"macOS: {platform.mac_ver()[0]} ({platform.machine()})",
    f"Python: {platform.python_version()}",
    f"OpenSSL: {ssl.OPENSSL_VERSION}",
    f"SQLite: {sqlite3.sqlite_version}",
    f"PyInstaller: {pyinstaller}",
    "",
    "Third-party notices: THIRD_PARTY_NOTICES.md",
    "Full license texts: licenses/",
]
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

echo "==> sign ($IDENTITY)"
if [ "$IDENTITY" = "-" ]; then
    if ! codesign --force --deep --sign "$IDENTITY" "$APP"; then
        echo "error: signing failed for identity '$IDENTITY'" >&2
        echo "hint:  security find-identity -v -p codesigning" >&2
        exit 1
    fi
else
    # Hardened runtime for a real or self-signed identity; ad-hoc signatures
    # are for local runs only and do not need it.
    if ! codesign --force --deep --options runtime --sign "$IDENTITY" "$APP"; then
        echo "error: signing failed for identity '$IDENTITY'" >&2
        echo "hint:  security find-identity -v -p codesigning" >&2
        exit 1
    fi
fi

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
    "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
fi

echo
echo "built $APP"
codesign -dv "$APP" 2>&1 | sed -n '1,4p'
