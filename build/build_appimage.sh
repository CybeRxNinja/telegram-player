#!/usr/bin/env bash
#
# Build the Telegram Player AppImage.
#
# Layout produced under build/:
#   dl/                      downloaded toolchain + mpv-build (kept between runs)
#   AppDir/                  AppDir tree fed to appimagetool
#   dist/                    PyInstaller onedir + final AppImage (../dist)
#
# Run from anywhere:  build/build_appimage.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$HERE"

PY="$ROOT/.venv/bin/python3"
PIP="$ROOT/.venv/bin/pip"
PYINSTALLER="$ROOT/.venv/bin/pyinstaller"
VENV_PY_DIR="$ROOT/.venv/bin"
export PATH="$VENV_PY_DIR:$PATH"

APP_NAME="Telegram-Player"
# Derive the version from pyproject.toml so the AppImage filename always
# matches the released version (no manual bump needed).
VERSION="$(grep -m1 '^version = ' "$ROOT/pyproject.toml" | sed -E 's/version = "(.*)"/\1/')"
APP_ID="telegram-player"
OUT_IMG="$ROOT/dist/${APP_NAME}-${VERSION}-x86_64.AppImage"

echo "==> [1/6] ensure CPython 3.12 venv + deps"
if [ ! -x "$PY" ]; then
  mkdir -p dl
  PBS="cpython-3.12.13+20260807-x86_64-unknown-linux-gnu-install_only.tar.gz"
  PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260807/$PBS"
  if [ ! -f "dl/$PBS" ]; then
    curl -fsSL --retry 3 -o "dl/$PBS" "$PBS_URL"
  fi
  rm -rf dl/py
  tar -xzf "dl/$PBS" -C dl
  "$ROOT/dl/python/bin/python3" -m venv "$ROOT/.venv"
fi
"$PIP" install -q -U pip wheel
"$PIP" install -q telethon==1.44.0 PyQt6==6.11.0 segno==1.6.6 pyinstaller==6.22.0 pytest meson
"$PIP" install -q -e "$ROOT"

echo "==> [2/6] mpv binary"
MPV_BIN=""
if [ -x "dl/mpv-build/mpv/build/mpv" ]; then
  MPV_BIN="$(realpath dl/mpv-build/mpv/build/mpv)"
elif [ -x "dl/mpv/mpv" ]; then
  MPV_BIN="$(realpath dl/mpv/mpv)"   # pkgforge fallback extraction
fi

if [ -z "$MPV_BIN" ]; then
  echo "    building mpv from source (mpv-build)"
  if [ ! -d dl/mpv-build/.git ]; then
    git clone https://github.com/mpv-player/mpv-build.git dl/mpv-build
  fi
  pushd dl/mpv-build >/dev/null
  bash ./use-mpv-release
  bash ./use-ffmpeg-release
  bash ./use-libplacebo-release
  # libass is bundled by default in mpv-build; the use-libass-* selectors are
  # optional, so we do not call them.
  bash ./rebuild -j"$(nproc)"
  popd >/dev/null
  MPV_BIN="$(realpath dl/mpv-build/mpv/build/mpv)"
fi

# Fallback: if the source build failed, extract the pinned pkgforge AppImage.
if [ ! -x "$MPV_BIN" ]; then
  echo "    source build missing; trying pkgforge AppImage fallback"
  PKG="mpv-v0.41.0-anylinux-x86_64.AppImage"
  PKG_URL="https://github.com/pkgforge-dev/mpv-AppImage/releases/download/v0.41.0@2026-07-01_1782914175/$PKG"
  [ -f "dl/$PKG" ] || curl -fsSL --retry 3 -o "dl/$PKG" "$PKG_URL"
  chmod +x "dl/$PKG"
  # Extract to a dedicated dir (NOT the CWD squashfs-root/AppDir, which the
  # pkgforge rootfs would otherwise contaminate) and clean it up afterwards.
  EXTRACT_DIR="dl/pkgforge-extract"
  rm -rf "$EXTRACT_DIR" && mkdir -p "$EXTRACT_DIR"
  (cd "$EXTRACT_DIR" && APPIMAGE_EXTRACT_AND_RUN=1 "../$PKG" --appimage-extract >/dev/null)
  # pkgforge uses the sharun layout: bin/mpv is a launcher; the REAL binary
  # (with bundled ffmpeg) is shared/bin/mpv and its libs live in lib/.
  mkdir -p dl/mpv dl/mpv-libs
  cp -a "$EXTRACT_DIR/squashfs-root/shared/bin/mpv" dl/mpv/mpv
  cp -a "$EXTRACT_DIR/squashfs-root/lib/." dl/mpv-libs 2>/dev/null || true
  rm -rf "$EXTRACT_DIR"
  MPV_BIN="$(realpath dl/mpv/mpv)"
fi

if [ ! -x "$MPV_BIN" ]; then
  echo "ERROR: could not obtain an mpv binary" >&2
  exit 1
fi
echo "    mpv: $MPV_BIN"

echo "==> [3/6] PyInstaller onedir"
"$PYINSTALLER" --noconfirm "$HERE/telegram_player.spec"
APP_BUNDLE="$HERE/dist/telegram_player"

echo "==> [4/6] assemble AppDir"
rm -rf AppDir
mkdir -p AppDir/usr/bin AppDir/usr/lib \
         AppDir/usr/share/applications \
         AppDir/usr/share/icons/hicolor/256x256/apps

cp -a "$APP_BUNDLE" "AppDir/usr/bin/telegram_player"
# mpv lives inside the PyInstaller onedir bundle (sys._MEIPASS), next to the exe.
cp "$MPV_BIN" "AppDir/usr/bin/telegram_player/mpv"
chmod +x "AppDir/usr/bin/telegram_player/mpv"

cat > AppDir/AppRun <<'EOF'
#!/bin/sh
export LC_ALL=C.UTF-8
# Bundled mpv resolves libs (incl. pulseaudio/spa/gbm subdirs) from usr/lib.
LP="$APPDIR/usr/lib"
for d in "$APPDIR/usr/lib"/*/; do
    [ -d "$d" ] && LP="$LP:${d%/}"
done
export LD_LIBRARY_PATH="$LP${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$APPDIR/usr/bin/telegram_player/telegram_player" "$@"
EOF
chmod +x AppDir/AppRun

cat > "AppDir/usr/share/applications/${APP_ID}.desktop" <<EOF
[Desktop Entry]
Name=Telegram Player
Exec=${APP_ID}
Icon=${APP_ID}
Type=Application
Categories=AudioVideo;Player;
Terminal=false
EOF
# Recent appimagetool builds only look for the desktop file at the AppDir root.
cp "AppDir/usr/share/applications/${APP_ID}.desktop" "AppDir/${APP_ID}.desktop"

cat > "AppDir/usr/share/icons/hicolor/256x256/apps/${APP_ID}.svg" <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect x="16" y="16" width="224" height="224" rx="48" fill="#17212B"/>
  <rect x="16" y="16" width="224" height="224" rx="48" fill="none" stroke="#2AABEE" stroke-width="6"/>
  <path d="M82 44 L198 128 L82 212 Z" fill="#2AABEE"/>
  <circle cx="186" cy="186" r="40" fill="#2AABEE" opacity="0.9"/>
  <path d="M170 174 L198 186 L170 198 Z" fill="#0E1621"/>
  <path d="M166 170 L186 158 L210 152 L196 176 L214 186 L188 190 L196 214 L178 196 L156 202 L168 180 Z" fill="#0E1621"/>
</svg>
EOF
# Same for the icon: root-level copy doubles as .DirIcon (shown for the AppImage).
cp "AppDir/usr/share/icons/hicolor/256x256/apps/${APP_ID}.svg" "AppDir/${APP_ID}.svg"
cp "AppDir/usr/share/icons/hicolor/256x256/apps/${APP_ID}.svg" "AppDir/.DirIcon"

echo "    copying non-core mpv libraries"
MISSING=0
for lib in $(ldd "$MPV_BIN" 2>/dev/null | awk '/=> \// {print $3}'); do
  base="$(basename "$lib")"
  case "$base" in
    libc.so.6|libm.so.6|libdl.so.2|libpthread.so.0|ld-linux-x86-64.so.2|libresolv.so.2)
      continue ;;
  esac
  cp -n "$lib" "AppDir/usr/lib/" 2>/dev/null || true
done
echo "    ldd not-found count: $(ldd "$MPV_BIN" 2>/dev/null | grep -c 'not found')"

echo "==> [5/6] appimagetool"
TOOL="dl/appimagetool-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
  curl -fsSL --retry 3 -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
mkdir -p "$ROOT/dist"
# continuous appimagetool ships as a plain static ELF now (no shell wrapper),
# so run it directly; APPIMAGE_EXTRACT_AND_RUN is ignored by the ELF build.
APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" AppDir "$OUT_IMG"

echo "==> [6/6] done"
ls -lh "$OUT_IMG"
echo "Built: $OUT_IMG"