#!/usr/bin/env bash
# One-command native macOS (.app + .dmg) builder for Apple Silicon (M1-M4) & Intel Macs
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Building Twitch Recorder Pro for macOS ==="

# 1. Ensure ffmpeg is available (for bundling into .app/Contents/Resources/bin/)
if [ -f "bin/ffmpeg" ] && [ -f "bin/ffprobe" ]; then
  echo "Using static ffmpeg/ffprobe from ./bin/"
elif ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg via Homebrew..."
    brew install ffmpeg
  else
    echo "WARNING: ffmpeg not found. Install via: brew install ffmpeg"
  fi
else
  FFMPEG_BIN="$(command -v ffmpeg)"
  if otool -L "$FFMPEG_BIN" 2>/dev/null | grep -q "/opt/homebrew/\|/usr/local/"; then
    echo "WARNING: $FFMPEG_BIN is dynamically linked against Homebrew libraries."
    echo "         For a 100% portable .dmg across Macs without Homebrew, place static"
    echo "         macOS ffmpeg & ffprobe binaries into ./bin/ before building."
  fi
fi

# 2. Generate native macOS .icns icon from assets/app_icon.png
if [ -f "assets/app_icon.png" ] && [ ! -f "assets/app_icon.icns" ]; then
  echo "Generating assets/app_icon.icns..."
  ICONSET="assets/app_icon.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for sz in 16 32 64 128 256 512; do
    sips -z $sz $sz assets/app_icon.png --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
    sz2=$((sz * 2))
    sips -z $sz2 $sz2 assets/app_icon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o assets/app_icon.icns
  rm -rf "$ICONSET"
fi

# 3. Pick Python 3.11-3.13 (avoid macOS default 3.9 if newer Python is installed)
PY_BIN=""
for cand in python3.12 python3.11 python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    PY_BIN="$(command -v "$cand")"
    break
  fi
done
echo "Using Python: $PY_BIN ($("$PY_BIN" --version))"

# Recreate .venv cleanly if missing or copied from Windows (lacks bin/activate)
if [ ! -f ".venv/bin/activate" ]; then
  if [ -d ".venv" ]; then
    echo "Replacing incompatible .venv (e.g. from Windows) with native macOS venv..."
    rm -rf .venv
  fi
  "$PY_BIN" -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

# 4. Build native TwitchRecorderPro.app
pyinstaller --noconfirm --clean TwitchRecorderPro.spec

# 5. Package macOS .zip (for 1-click in-app auto-updater) & drag-to-Applications .dmg
ZIP_OUT="dist/TwitchRecorderPro-macOS.zip"
rm -f "$ZIP_OUT"
ditto -c -k --sequesterRsrc --keepParent "dist/TwitchRecorderPro.app" "$ZIP_OUT"

DMG_STAGING="dist/dmg_staging"
DMG_OUT="dist/TwitchRecorderPro-macOS.dmg"
rm -rf "$DMG_STAGING" "$DMG_OUT"
mkdir -p "$DMG_STAGING"
cp -R "dist/TwitchRecorderPro.app" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

hdiutil create -volname "Twitch Recorder Pro" \
  -srcfolder "$DMG_STAGING" \
  -ov -format UDZO "$DMG_OUT"
rm -rf "$DMG_STAGING"

echo "=== SUCCESS! ==="
echo "App bundle: dist/TwitchRecorderPro.app"
echo "Auto-update ZIP: $ZIP_OUT"
echo "DMG image:  $DMG_OUT"
