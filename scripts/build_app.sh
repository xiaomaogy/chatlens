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
# Best-effort: macOS sometimes keeps the dist/ directory itself busy (Finder
# window open, a mounted ChatLens.dmg, Spotlight). rm clears the contents but
# may fail to remove the now-empty dir — that must not abort the build.
rm -rf build dist "${STAGE_DIR}" 2>/dev/null || true
rm -rf build/* dist/* 2>/dev/null || true

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

# Drop a Chinese-named README into the .dmg so Finder shows it at the top.
# Two-step install guidance: drag to /Applications, then right-click → Open
# on first launch (unsigned build means Gatekeeper otherwise blocks double-
# click with a confusing "cannot verify developer" dialog).
cat > "${STAGE_DIR}/先看这个 — 安装说明.txt" <<'TXT'
ChatLens — 第一次安装请按两步

【1】先拖到「应用程序 / Applications」
    把左边的 ChatLens 拖到右边的 Applications 文件夹再启动。
    千万别直接在这个 .dmg 窗口里双击 —— 等你弹出 .dmg，
    ChatLens 就消失了。

【2】第一次打开请「右键 → 打开」
    ChatLens 没有买 Apple 开发者签名，直接双击会被 macOS
    Gatekeeper 拦下来（弹一个「无法验证开发者」对话框）。
    解法：
      a. 进「应用程序」文件夹
      b. 在 ChatLens 上点右键 → 选「打开」
      c. 弹出确认框时点「打开」
    做一次就够，之后双击正常。

    懂命令行也可以跑：
      xattr -dr com.apple.quarantine /Applications/ChatLens.app

打开后会弹一个引导向导，带你完成 wechat-cli 初始化。
TXT

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
