#!/bin/bash
# Package the built .app into a distributable disk image.
set -e
cd "$(dirname "$0")"

APP="dist/Claude Migrator.app"
[ -d "$APP" ] || { echo "Build the app first: ./build-app.sh"; exit 1; }

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
DMG="dist/ClaudeMigrator-${VERSION}.dmg"
STAGE="$(mktemp -d)"

echo "Staging…"
ditto "$APP" "$STAGE/Claude Migrator.app"
ln -s /Applications "$STAGE/Applications"

# A short note lands next to the app so the Gatekeeper step is visible before
# the user hits it rather than after.
cat > "$STAGE/READ ME FIRST.txt" <<'NOTE'
Claude Migrator
===============

1. Drag "Claude Migrator" onto the Applications folder shown here.

2. The first launch will be blocked, because this app is not notarized
   by Apple. This is expected. To allow it:

     Open System Settings -> Privacy & Security
     Scroll to the bottom
     Next to "Claude Migrator was blocked", click "Open Anyway"

   Or, from Terminal:

     xattr -dr com.apple.quarantine "/Applications/Claude Migrator.app"

3. Quit the Claude desktop app before using the migrator. It refuses to
   write anything while Claude is running.

Full documentation:
https://github.com/pokharnajay/claude-migrator
NOTE

rm -f "$DMG"
hdiutil create \
  -volname "Claude Migrator" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG" >/dev/null

rm -rf "$STAGE"
echo "Built $DMG ($(du -h "$DMG" | cut -f1))"
