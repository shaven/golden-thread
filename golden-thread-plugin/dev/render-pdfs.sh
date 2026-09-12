#!/usr/bin/env bash
# render-pdfs.sh — render every shipped PDF from its HTML, locally, with one renderer.
#
#   dev/render-pdfs.sh              # render all five, in parallel
#   dev/render-pdfs.sh MANUAL       # render one
#   GT_RENDER_JOBS=1 dev/render-pdfs.sh    # serial, for debugging one render
#
# PARALLEL (2026-09-12): five independent Chrome renders sat in a serial for-loop, the
# exact shape core_parallel_when_beneficial exists to catch. Each render now gets its
# own --user-data-dir, because concurrent Chrome instances sharing the default profile
# fight over its lock and one of them silently produces nothing. The worker count comes
# from the parallel_work / parallel_max settings, via `gt_settings.py jobs`.
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
for d in "${DOCS[@]}"; do [ -f "$d.html" ] || { echo "no $d.html"; exit 2; }; done

GTV=$(ls -d golden-thread/*/ 2>/dev/null | sed 's#.*/\([^/]*\)/#\1#' \
      | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)
JOBS="${GT_RENDER_JOBS:-$(python3 "golden-thread/$GTV/scripts/gt_settings.py" \
        jobs "${#DOCS[@]}" --io-bound 2>/dev/null || echo 1)}"

# WAIT FOR THE OUTPUT, NOT THE PROCESS.
#
# Chrome given a throwaway --user-data-dir writes the PDF and then does not exit --
# verified 2026-09-12 with both --headless and --headless=new: the file is complete in
# seconds and the process sits there indefinitely. A serial loop sharing the default
# profile happened to exit, which is why this only appeared once the renders were
# parallelised and each got its own profile.
#
# So each render polls for its own output file, waits for the size to stop changing, and
# then stops Chrome itself. Output goes to $d.pdf.new and is moved into place only on
# success, so a failed render leaves the previous PDF untouched. Every render is also
# fully detached from this script's stdin/stdout/stderr: Chrome's helper processes
# inherit whatever fds they are given, and while they hold a pipeline's write end open
# the reader never sees EOF -- that is what left ten idle shells sitting for nine
# minutes after the PDFs were already on disk.
render_one() {
  # This function is deliberately NOT under errexit. The script sets -e, and every poll
  # here is a test that is legitimately false most of the time (`[ -f "$out" ]` before
  # Chrome has written anything, `kill -0` once Chrome has gone). Under -e the background
  # subshell dies at the first false test, silently: no output, a leftover .pdf.new, and a
  # non-zero status that reads as "the render failed" when nothing had gone wrong.
  set +e
  d="$1"
  prof=$(mktemp -d)
  out="$PWD/$d.pdf.new"
  rm -f "$out"
  "$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --no-first-run --no-default-browser-check --disable-crash-reporter \
    --user-data-dir="$prof" \
    --print-to-pdf="$out" "file://$PWD/$d.html" \
    >/dev/null 2>&1 </dev/null &
  cpid=$!

  ticks=0                                   # half-seconds
  deadline=$(( ${GT_RENDER_TIMEOUT:-120} * 2 ))
  last=-1
  stable=0
  while :; do
    sz=0
    [ -f "$out" ] && sz=$(wc -c < "$out" | tr -d ' ')
    if [ "$sz" -gt 0 ] && [ "$sz" = "$last" ]; then
      stable=$((stable + 1))
      [ "$stable" -ge 2 ] && break          # unchanged across two polls: done
    else
      stable=0
    fi
    last=$sz
    if [ "$ticks" -ge "$deadline" ]; then
      echo "TIMEOUT $d.pdf after $((ticks / 2))s"
      break
    fi
    kill -0 "$cpid" 2>/dev/null || break     # Chrome died: fall through to the checks
    sleep 0.5
    ticks=$((ticks + 1))
  done

  kill "$cpid" 2>/dev/null
  sleep 0.2
  kill -9 "$cpid" 2>/dev/null
  wait "$cpid" 2>/dev/null
  # kill -9 returns before Chrome's helpers have finished writing into the profile, so a
  # single rm -rf races them and prints "Directory not empty". Retry briefly instead.
  for _ in 1 2 3 4; do rm -rf "$prof" 2>/dev/null && break; sleep 0.5; done

  if [ -s "$out" ]; then
    mv "$out" "$d.pdf"
    echo "rendered $d.pdf in $((ticks / 2))s ($(wc -c < "$d.pdf" | tr -d ' ') bytes)"
    return 0
  fi
  rm -f "$out"
  echo "FAILED $d.pdf"
  return 1
}


echo "rendering ${#DOCS[@]} document(s), $JOBS worker(s)"
started=$SECONDS
pids=""
for d in "${DOCS[@]}"; do
  while [ "$(jobs -pr | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 0.2; done
  render_one "$d" &
  pids="$pids $!"
done
fail=0
for p in $pids; do wait "$p" || fail=1; done
echo "done in $((SECONDS - started))s"
[ "$fail" -eq 0 ] || { echo "at least one render FAILED"; exit 1; }
