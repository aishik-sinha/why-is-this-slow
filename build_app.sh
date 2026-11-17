#!/bin/bash
# Builds "build/Why is this slow?.app". `./build_app.sh install` also copies it to ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
NAME="Why is this slow?.app"
# Assembled and signed outside this folder: on a cloud-synced Desktop the sync service stamps Finder info
# onto the bundle within a second, and codesign refuses to sign or strictly verify a bundle carrying it.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/$NAME"
mkdir -p build "$APP/Contents/MacOS" "$APP/Contents/Resources/web"

swiftc -O -target arm64-apple-macos13.0 -sdk "$(xcrun --show-sdk-path)" \
  -module-cache-path build/ModuleCache \
  -framework AppKit -framework WebKit -framework ServiceManagement \
  mac/main.swift -o "$APP/Contents/MacOS/WhyIsThisSlow"

cp mac/Info.plist "$APP/Contents/"
cp slow.py server.py insights.py "$APP/Contents/Resources/"
cp web/index.html "$APP/Contents/Resources/web/"

rm -rf build/AppIcon.iconset
/usr/bin/python3 -B mac/make_icon.py build/AppIcon.iconset
iconutil -c icns build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

codesign --force --sign - "$APP"  # ad-hoc: runs on this Mac only
codesign --verify --strict "$APP"
rm -rf "build/$NAME"
ditto "$APP" "build/$NAME"
echo "Built build/$NAME"

if [ "${1:-}" = install ]; then
  if pgrep -x WhyIsThisSlow >/dev/null; then echo "Quit Why is this slow? first" >&2; exit 1; fi
  rm -rf "$HOME/Applications/$NAME"
  mkdir -p "$HOME/Applications"
  ditto "$APP" "$HOME/Applications/$NAME"
  codesign --verify --strict "$HOME/Applications/$NAME"
  echo "Installed. Open it from ~/Applications."
fi
