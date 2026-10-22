#!/bin/bash
# Builds Autopilot.app into ~/Applications.
set -e
cd "$(dirname "$0")"
APP="$HOME/Applications/Autopilot.app"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/Autopilot" main.swift 2>&1 | grep -v "^$" || true
[ -x "$APP/Contents/MacOS/Autopilot" ] || { echo "build failed"; exit 1; }
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Autopilot</string>
  <key>CFBundleIdentifier</key><string>local.handle.autopilot</string>
  <key>CFBundleExecutable</key><string>Autopilot</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
