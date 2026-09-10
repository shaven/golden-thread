#!/usr/bin/env bash
set -euo pipefail

# Build the distributable zip -- the artifact INSTALL.md, ONBOARDING.md and
# golden-thread-docs.md all tell a new machine to unzip.
#
# Two defects this replaces, both found 2026-09-10:
#
#   1. VERSION was the hardcoded string "0.1.0", against a version directory that
#      had not existed for a dozen releases. `set -e` plus a missing directory
#      meant the script could only fail -- but the STALE ZIP IT HAD ALREADY
#      PRODUCED stayed in the repo, dated and plausible, and three documents
#      pointed a new user straight at it. The documented install path for a second
#      machine handed out a build from before nearly everything.
#
#   2. It never copied hooks/. install.sh reads "$SRC/hooks" to populate
#      ~/.claude/golden-thread/hooks, so a zip built by this script could not
#      install the Core-rule enforcement at all -- inject_core_rules.sh,
#      validate_response.sh and guard_session_claims.sh were simply absent. The
#      one tier whose whole purpose is that it never silently fails, silently
#      absent from the artifact strangers install from.
#
# The version is DERIVED, never typed, by the same rule install.sh uses: the
# newest version directory that carries plugin metadata. A constant here is a
# constant that goes stale the next time someone adds a directory and forgets
# this file -- which is exactly what happened.

cd "$(dirname "$0")"
DIST="golden-thread-plugin"
ZIP="golden-thread-plugin.zip"

# Sorted numerically per field, not lexically: a lexical sort puts 0.9.4 above
# 0.10.0 and would start shipping the older release at double digits.
latest_version() {
  local root="$1" d name
  for d in "$root"/*/; do
    name=$(basename "$d")
    [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    [ -f "$d/.claude-plugin/plugin.json" ] || continue
    echo "$name"
  done | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}

VERSION="$(latest_version golden-thread)"
WIKI_VERSION="$(latest_version golden-thread-wiki)"
[ -n "$VERSION" ]      || { echo "✗ no installable gt version directory found"; exit 1; }
[ -n "$WIKI_VERSION" ] || { echo "✗ no installable gt-wiki version directory found"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
rm -f "$ZIP"
mkdir -p "$STAGE/$DIST/golden-thread/$VERSION"
mkdir -p "$STAGE/$DIST/golden-thread-wiki/$WIKI_VERSION"

# hooks/ is NOT optional -- see note 2 above.
for dir in .claude-plugin skills scripts templates hooks; do
  [ -d "golden-thread/$VERSION/$dir" ] \
    && cp -R "golden-thread/$VERSION/$dir" "$STAGE/$DIST/golden-thread/$VERSION/"
done
for dir in .claude-plugin skills scripts templates commands hooks; do
  [ -d "golden-thread-wiki/$WIKI_VERSION/$dir" ] \
    && cp -R "golden-thread-wiki/$WIKI_VERSION/$dir" "$STAGE/$DIST/golden-thread-wiki/$WIKI_VERSION/"
done

# MANIFEST.json is deliberately NOT shipped: install.sh regenerates it against the
# files it actually lays down, so a stale manifest in the zip would make the very
# first component check report drift that does not exist.
find "$STAGE" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) \
     -exec rm -rf {} + 2>/dev/null || true

# The docs a fresh machine needs in hand before it has a vault to read them from.
for f in install.sh selftest.sh README.md INSTALL.md ONBOARDING.md MANUAL.md; do
  [ -f "$f" ] && cp "$f" "$STAGE/$DIST/"
done
chmod +x "$STAGE/$DIST/install.sh" "$STAGE/$DIST/selftest.sh" 2>/dev/null || true

(cd "$STAGE" && zip -qr "$OLDPWD/$ZIP" "$DIST")

echo "Created $ZIP  (gt $VERSION, gt-wiki $WIKI_VERSION)"
# State what a consumer will find, so a wrong build is visible here rather than on
# someone else's machine after they follow INSTALL.md.
unzip -l "$ZIP" | awk '/golden-thread(-wiki)?\/[0-9]/ {print $4}' \
  | sed -E 's#(golden-thread(-wiki)?/[0-9.]+)/.*#\1#' | sort -u | sed 's/^/  ships /'
