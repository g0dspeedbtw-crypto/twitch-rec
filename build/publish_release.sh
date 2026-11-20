#!/usr/bin/env bash
# Publish a new cloud update release to GitHub (triggers automatic Windows + macOS Release builds)
# Usage:
#   ./build/publish_release.sh 2.1.1 "Описание обновления"
#   ./build/publish_release.sh 2.1.1 "Локальная сборка macOS" --local-upload
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
NOTES="${2:-Обновление Twitch Recorder Pro}"
MODE="${3:-}"

if [ -z "$VERSION" ]; then
  echo "Usage: ./build/publish_release.sh <version> [release_notes] [--local-upload]"
  echo "Example: ./build/publish_release.sh 2.1.1 \"Новые фичи и исправления\""
  exit 1
fi

VERSION="${VERSION#v}"
if ! echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$'; then
  echo "ERROR: Invalid version '$VERSION'. Use SemVer like 2.1.1"
  exit 1
fi

TAG="v${VERSION}"

if [ ! -d ".git" ]; then
  echo "ERROR: Git repository is not initialized yet."
  echo "Run:"
  echo "  git init"
  echo "  git remote add origin https://github.com/<your-user>/<your-repo>.git"
  exit 1
fi

REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
if [ -z "$REMOTE_URL" ]; then
  echo "ERROR: Git remote 'origin' is not set."
  echo "Run: git remote add origin https://github.com/<your-user>/<your-repo>.git"
  exit 1
fi

# Extract owner/repo from git remote URL
GH_REPO="$(python3 - "$REMOTE_URL" <<'PY'
import re, sys
url = sys.argv[1].strip()
m = re.search(r"github\.com[:/]([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+?)(?:\.git)?/?$", url, re.IGNORECASE)
print(m.group(1) if m else "")
PY
)"

# Bump APP_VERSION and bake DEFAULT_GITHUB_REPO in app/config.py
python3 - "$VERSION" "$GH_REPO" <<'PY'
import re, sys
from pathlib import Path
ver, repo = sys.argv[1], sys.argv[2]
p = Path("app/config.py")
txt = p.read_text(encoding="utf-8")
txt = re.sub(r'^APP_VERSION\s*=\s*"[^"]*"', f'APP_VERSION = "{ver}"', txt, flags=re.MULTILINE)
if repo:
    txt = re.sub(r'^DEFAULT_GITHUB_REPO\s*=\s*"[^"]*"', f'DEFAULT_GITHUB_REPO = "{repo}"', txt, flags=re.MULTILINE)
p.write_text(txt, encoding="utf-8")
print(f"Updated app/config.py -> APP_VERSION={ver}, DEFAULT_GITHUB_REPO={repo or '(unchanged)'}")
PY

git add -A
if ! git diff --cached --quiet; then
  git commit -m "Release ${TAG}: ${NOTES}"
else
  echo "No file changes to commit; tagging current HEAD as ${TAG}."
fi

git tag -f -a "$TAG" -m "$NOTES"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"
git push origin "$TAG" --force

echo "=== Pushed ${TAG} to GitHub (${GH_REPO:-origin}) ==="
echo "GitHub Actions will now build Windows + macOS and publish Release ${TAG} automatically."

if [ "$MODE" = "--local-upload" ]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "WARNING: GitHub CLI (gh) not found. Skipping local upload."
    exit 0
  fi
  ASSETS=()
  [ -f "dist/TwitchRecorderPro-macOS.zip" ] && ASSETS+=("dist/TwitchRecorderPro-macOS.zip")
  [ -f "dist/TwitchRecorderPro-macOS.dmg" ] && ASSETS+=("dist/TwitchRecorderPro-macOS.dmg")
  [ -f "TwitchRecorderPro-Windows.zip" ] && ASSETS+=("TwitchRecorderPro-Windows.zip")
  if [ "${#ASSETS[@]}" -gt 0 ]; then
    echo "Uploading local builds to GitHub Release ${TAG}..."
    gh release create "$TAG" "${ASSETS[@]}" --title "Twitch Recorder Pro ${TAG}" --notes "$NOTES" 2>/dev/null \
      || gh release upload "$TAG" "${ASSETS[@]}" --clobber
    echo "Local assets uploaded to Release ${TAG}!"
  else
    echo "No local build archives found in dist/. Run ./build/build_macos.sh first if needed."
  fi
fi
