#!/bin/bash
# Rebuild the standalone .app and install it to the Desktop.
set -e
cd "$(dirname "$0")"

[ -d .venv-slim ] || { echo "Run: python3 -m venv .venv-slim && .venv-slim/bin/pip install pyside6-essentials pyinstaller pytest"; exit 1; }

echo "Running tests…"
QT_QPA_PLATFORM=offscreen .venv-slim/bin/python -m pytest -q

echo "Building…"
rm -rf build dist
arch -arm64 .venv-slim/bin/pyinstaller --noconfirm --clean ClaudeMigrator.spec

DEST="$HOME/Desktop/Claude Migrator.app"
rm -rf "$DEST"
cp -Rc "dist/Claude Migrator.app" "$DEST"
xattr -cr "$DEST"
rm -rf build
echo "Installed to $DEST"
