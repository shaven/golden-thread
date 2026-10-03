#!/bin/bash
# presence-release.sh -- build, Developer ID sign and (only on --submit) notarize the gt-presence
# Touch ID helper for a gt release (0.20.0).
#
#   dev/presence-release.sh --dry-run            print every command, run none
#   dev/presence-release.sh                      build + sign + zip + verify; NO submission
#   dev/presence-release.sh --submit             ... then notarize with notarytool (--wait)
#   options: --version V (default: newest golden-thread/<V>), --out DIR (default: $TMPDIR/gt-presence-release-<V>)
#
# What it produces: <out>/gt-presence (universal arm64 + x86_64, hardened runtime, secure
# timestamp, signed "Developer ID Application: Stacy Haven (CTM8ZW9QJD)") and
# <out>/gt-presence-<V>-macos.zip (what is submitted and what is shipped), plus SHA256SUMS.
#
# STAPLING DOES NOT APPLY. `xcrun stapler` staples tickets to .app bundles, .pkg/.mpkg installers
# and .dmg disk images only. A bare Mach-O executable has nowhere to hold a ticket, and a .zip
# cannot be stapled either. For a bare binary the ticket lives with Apple: Gatekeeper looks it up
# online the first time a QUARANTINED copy is run. So:
#   * the zip is notarized (Apple notarizes the binary inside it) and shipped as-is;
#   * a machine that is offline on first launch of a quarantined copy cannot confirm the
#     notarization (it would need a stapled .pkg -- which needs a separate "Developer ID
#     Installer" certificate and productsign; not set up);
#   * install.sh fetching with curl/git sets no quarantine flag, so Gatekeeper never assesses it
#     there; notarization matters for browser-downloaded copies and for enterprise policy
#     (MDM / `spctl` checks), which is who asked for it.
#
# The helper needs NO entitlements: CryptoKit Secure Enclave keys created from dataRepresentation
# blobs use no keychain item, and LocalAuthentication works under the hardened runtime.
#
# SAFETY: never submits without --submit; --dry-run runs nothing at all (not even swiftc). The
# notary credentials come from the keychain profile `gt-notary` (stored by
# `xcrun notarytool store-credentials`); this script never reads or prints them.
set -euo pipefail

IDENTITY="Developer ID Application: Stacy Haven (CTM8ZW9QJD)"
PROFILE="gt-notary"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DRY=0; SUBMIT=0; VERSION=""; OUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --submit) SUBMIT=1 ;;
    --version) VERSION="${2:?--version needs a value}"; shift ;;
    --out) OUT="${2:?--out needs a value}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "presence-release: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$VERSION" ]; then
  VERSION="$(ls "$ROOT/golden-thread" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
fi
SRC="$ROOT/golden-thread/$VERSION/scripts/gt-presence.swift"
OUT="${OUT:-${TMPDIR:-/tmp}/gt-presence-release-$VERSION}"     # outside the repo: never shipped by accident
BIN="$OUT/gt-presence"
ZIP="$OUT/gt-presence-$VERSION-macos.zip"

run() {
  # Print the command exactly; run it unless --dry-run.
  printf '+'; printf ' %q' "$@"; printf '\n'
  [ "$DRY" = 1 ] || "$@"
}

[ "$(uname -s)" = Darwin ] || { echo "presence-release: macOS only" >&2; exit 2; }
[ -f "$SRC" ] || { echo "presence-release: no helper source at $SRC" >&2; exit 2; }
if [ "$DRY" = 0 ]; then
  command -v swiftc >/dev/null || { echo "presence-release: swiftc missing (Xcode)" >&2; exit 2; }
  # The identity is checked BY NAME only; nothing is read from the keychain.
  security find-identity -v -p codesigning | grep -qF "\"$IDENTITY\"" \
    || { echo "presence-release: signing identity not found: $IDENTITY" >&2; exit 2; }
fi
[ "$DRY" = 1 ] && echo "# dry run: gt $VERSION -- nothing below is executed"

run mkdir -p "$OUT/arm64" "$OUT/x86_64"
run xcrun swiftc -O -target arm64-apple-macos11.0 -o "$OUT/arm64/gt-presence" "$SRC"
run xcrun swiftc -O -target x86_64-apple-macos11.0 -o "$OUT/x86_64/gt-presence" "$SRC"
run lipo -create -output "$BIN" "$OUT/arm64/gt-presence" "$OUT/x86_64/gt-presence"
run lipo "$BIN" -verify_arch arm64 x86_64
run codesign --force --options runtime --timestamp -s "$IDENTITY" "$BIN"
run codesign --verify --strict --verbose=2 "$BIN"
run codesign --display --verbose=2 "$BIN"
run rm -f "$ZIP"
run ditto -c -k --keepParent "$BIN" "$ZIP"
run sh -c "cd \"$OUT\" && shasum -a 256 gt-presence \"$(basename "$ZIP")\" > SHA256SUMS"

if [ "$SUBMIT" != 1 ]; then
  echo "# not submitted: re-run with --submit to notarize $ZIP (profile $PROFILE)"
  exit 0
fi
run xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
# Stapling is not possible for a bare binary or a zip (see the header). Confirm instead that
# Gatekeeper accepts the signed, notarized binary (online lookup of the ticket):
run spctl --assess --type execute --verbose=4 "$BIN" || true
echo "# notarized: ship $ZIP; the ticket is held by Apple (no staple for a bare Mach-O)"
