#!/usr/bin/env bash
# remote-test.sh — run the full test suite on a remote Linux runner instead of this machine.
#
#   dev/remote-test.sh [--host claudebox] [-j N] [--keep] [selector ...]
#
#   Selectors (test modules or units) run a subset; only a FULL run records a receipt.
#
# Why: a full local run turns every throwaway install into file churn that fseventsd, the sync
# agents, Spotlight and the virus scanner all react to. On 2026-10-01 that drove this Mac's load to
# 50–96 and dropped the owner's keystrokes. The runner has none of those watchers.
#
# In order, stopping at the first failure:
#   1. snapshot: every tracked + untracked-not-ignored file under the repo root, plus a fingerprint
#      (sha256 over path + content of each). The snapshot time is noted.
#   2. ship it over ssh into a fresh directory on the runner (one stream, no temp file here).
#   3. run tests/run.sh there with a throwaway HOME, streaming the output back.
#   4. on a pass, re-fingerprint the local tree. Only if it is UNCHANGED since the snapshot does
#      this machine record a receipt (gt_test_receipt.py), with what = "remote:tests/run.sh@<host>".
#      A receipt is local and file-time based (gt_test_receipt.py: "A receipt means nothing on
#      another machine"), so the Mac writes it, and only for the exact tree that passed.
#
# Prints the wall time of each step. Exit: 0 passed and receipt recorded · 1 tests failed ·
# 2 usage/ssh/ship failed · 3 passed, but the local tree changed during the run (no receipt).
set -uo pipefail
HOST=claudebox; JOBS=""; KEEP=no
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    -j|--jobs) JOBS="$2"; shift 2 ;;
    --keep) KEEP=yes; shift ;;
    --) shift; break ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1"; exit 2 ;;
    *) break ;;
  esac
done
PLUGIN="$(cd "$(dirname "$0")/.." && pwd -P)"
ROOT="$(git -C "$PLUGIN" rev-parse --show-toplevel)" || { echo "not in a git repo"; exit 2; }
REL="${PLUGIN#$ROOT/}"
now() { python3 -c 'import time; print(time.time())'; }
secs() { python3 -c "import sys; print(f'{float(sys.argv[2])-float(sys.argv[1]):.1f}s')" "$1" "$2"; }

fingerprint() {
  # path + content of every file the snapshot carries; any edit, add or delete changes it
  ( cd "$ROOT" && git ls-files -co --exclude-standard -z \
      | python3 -c '
import hashlib, os, sys
h = hashlib.sha256()
for p in sorted(x for x in sys.stdin.buffer.read().split(b"\0") if x):
    h.update(p + b"\0")
    try:
        with open(p, "rb") as f: h.update(hashlib.sha256(f.read()).digest())
    except (FileNotFoundError, IsADirectoryError): h.update(b"-")
print(h.hexdigest())' )
}

T0=$(now)
echo "== 1. snapshot $ROOT"
FP=$(fingerprint)
N=$(cd "$ROOT" && git ls-files -co --exclude-standard | wc -l | tr -d ' ')
echo "   $N files, fingerprint ${FP:0:16}"

echo "== 2. ship to $HOST"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true || { echo "FAILED: cannot reach $HOST"; exit 2; }
RUNID="gt-$(date +%Y%m%d-%H%M%S)-$$"
T1=$(now)
( cd "$ROOT" && git ls-files -co --exclude-standard -z \
    | COPYFILE_DISABLE=1 tar --null -T - -czf - 2>/dev/null ) \
  | ssh -o BatchMode=yes "$HOST" "mkdir -p ~/gt-remote/$RUNID && tar -xzf - -C ~/gt-remote/$RUNID" \
  || { echo "FAILED: ship"; exit 2; }
T2=$(now); echo "   shipped in $(secs "$T1" "$T2")"

echo "== 3. run tests/run.sh on $HOST"
REMOTE_CMD="cd ~/gt-remote/$RUNID/$REL && H=\$(mktemp -d) && umask 022 && HOME=\$H PYTHONDONTWRITEBYTECODE=1 bash tests/run.sh ${JOBS:+-j $JOBS} $* 2>&1; rc=\$?; rm -rf \$H; exit \$rc"
LOG="$(mktemp -t gt-remote-test).log"
ssh -o BatchMode=yes "$HOST" "$REMOTE_CMD" | tee "$LOG"
RC=${PIPESTATUS[0]}
T3=$(now)
[ "$KEEP" = yes ] || ssh -o BatchMode=yes "$HOST" "rm -rf ~/gt-remote/$RUNID" || true
COUNT=$(sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' "$LOG" | tail -1)
echo ""
echo "== timing: ship $(secs "$T1" "$T2") · run $(secs "$T2" "$T3") · total $(secs "$T0" "$T3")"
if [ "$RC" -ne 0 ]; then
  echo "TESTS FAILED on $HOST (exit $RC). Log: $LOG"; exit 1
fi

echo "== 4. receipt"
if [ $# -gt 0 ]; then echo "PASSED on $HOST (subset: $*). A subset never records a receipt."; exit 0; fi
if [ "$(fingerprint)" != "$FP" ]; then
  echo "PASSED on $HOST, but the local tree changed during the run — no receipt. Run again."; exit 3
fi
for d in "$PLUGIN"/golden-thread/*/scripts; do
  [ -f "$d/gt_test_receipt.py" ] || continue
  python3 "$d/gt_test_receipt.py" record --repo "$ROOT" --what "remote:tests/run.sh@$HOST" \
    --tests "${COUNT:-0}" --ok >/dev/null && { echo "receipt recorded: ${COUNT:-?} tests passed on $HOST"; exit 0; }
done
echo "PASSED on $HOST, but no gt_test_receipt.py was found to record the receipt"; exit 2
