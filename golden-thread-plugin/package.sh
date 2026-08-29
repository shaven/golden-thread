#!/usr/bin/env bash
set -euo pipefail

VERSION="0.1.0"
DIST="golden-thread-plugin"
ZIP="golden-thread-plugin.zip"

rm -rf "/tmp/$DIST" "$ZIP"
mkdir -p "/tmp/$DIST/golden-thread/$VERSION"
mkdir -p "/tmp/$DIST/golden-thread-wiki/$VERSION"

# Plugin files only
for dir in .claude-plugin skills scripts templates; do
  cp -r "golden-thread/$VERSION/$dir" "/tmp/$DIST/golden-thread/$VERSION/"
done
for dir in .claude-plugin skills scripts templates; do
  cp -r "golden-thread-wiki/$VERSION/$dir" "/tmp/$DIST/golden-thread-wiki/$VERSION/"
done

# Distribution files
cp install.sh README.md INSTALL.md MANUAL.md "/tmp/$DIST/"

(cd /tmp && zip -qr "$OLDPWD/$ZIP" "$DIST")
rm -rf "/tmp/$DIST"

echo "Created $ZIP"
