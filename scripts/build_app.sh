#!/usr/bin/env bash
# Build ChatLens.app and ChatLens.dmg.
#
# Usage:   ./scripts/build_app.sh
# Outputs: dist/ChatLens.app, dist/ChatLens.dmg
#
# Prereqs (in the active python env):
#   pip install -e ".[build]"
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
  echo "warning: no .venv found at $ROOT/.venv — using $(which python3)" >&2
fi

APP_NAME="ChatLens"
APP_PATH="dist/${APP_NAME}.app"
DMG_PATH="dist/${APP_NAME}.dmg"
STAGE_DIR="build/dmg-stage"

echo "==> Cleaning previous build"
rm -rf build dist "${STAGE_DIR}"

echo "==> Running py2app (using ${PY})"
"${PY}" setup.py py2app

if [[ ! -d "${APP_PATH}" ]]; then
  echo "build failed: ${APP_PATH} not found" >&2
  exit 1
fi

echo "==> Clearing quarantine flag on the build output"
xattr -dr com.apple.quarantine "${APP_PATH}" || true

echo "==> Staging .dmg contents"
mkdir -p "${STAGE_DIR}"
cp -R "${APP_PATH}" "${STAGE_DIR}/"
ln -s /Applications "${STAGE_DIR}/Applications"

echo "==> Building ${DMG_PATH}"
rm -f "${DMG_PATH}"
hdiutil create \
  -volname "${APP_NAME}" \
  -srcfolder "${STAGE_DIR}" \
  -ov -format UDZO \
  "${DMG_PATH}"

echo
echo "Done."
echo "  App: ${ROOT}/${APP_PATH}"
echo "  DMG: ${ROOT}/${DMG_PATH}"
echo
echo "First-launch on the recipient's Mac (unsigned build):"
echo "  Right-click ChatLens.app → Open → confirm in the dialog."
echo "  Or run:  xattr -dr com.apple.quarantine /Applications/ChatLens.app"
