#!/usr/bin/env bash
# render-pdfs.sh — render every shipped PDF from its HTML, locally, with one renderer.
#
#   dev/render-pdfs.sh              # render all five
#   dev/render-pdfs.sh MANUAL       # render one
#
# MANUAL.pdf used to need WeasyPrint on a single remote host, because its stylesheet
# numbers pages with an @page margin box. On 2026-09-11 that host's disk was full and
# the MANUAL shipped a release behind. Chrome renders @page margin boxes since
# version 131, so every PDF now comes from local headless Chrome; this script checks
# the version rather than assuming it.
#
# --no-pdf-header-footer is not optional: Chrome's default footer embeds the source
# file:// URL, which once put a home directory path inside three shipped PDFs.
# After rendering, run dev/scrub_check.py over the PDFs (release-check.sh does).
set -euo pipefail
cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || CHROME=$(command -v google-chrome || command -v chromium || true)
[ -n "$CHROME" ] && [ -x "$CHROME" ] || { echo "Chrome not found — set CHROME=/path/to/chrome"; exit 2; }
MAJOR=$("$CHROME" --version 2>/dev/null | grep -oE '[0-9]+' | head -1)
[ "${MAJOR:-0}" -ge 131 ] || { echo "Chrome $MAJOR is too old: @page margin boxes (MANUAL page numbers) need 131+"; exit 2; }

DOCS=(MANUAL ONBOARDING OBSIDIAN-WORKFLOW golden-thread-docs golden-thread-developer-guide)
[ $# -gt 0 ] && DOCS=("$@")
for d in "${DOCS[@]}"; do
  [ -f "$d.html" ] || { echo "no $d.html"; exit 2; }
  "$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --print-to-pdf="$PWD/$d.pdf" "file://$PWD/$d.html" >/dev/null 2>&1
  [ -s "$d.pdf" ] && [ "$d.pdf" -nt "$d.html" ] || { echo "FAILED $d.pdf"; exit 1; }
  echo "rendered $d.pdf ($(wc -c < "$d.pdf" | tr -d ' ') bytes)"
done
