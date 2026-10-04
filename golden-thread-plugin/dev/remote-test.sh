#!/usr/bin/env bash
# remote-test.sh — run the full test suite on a remote Linux runner instead of this machine.
#
#   dev/remote-test.sh [--host H] [-j N] [--keep] [--affected] [selector ...]
#
#   Selectors (test modules or units) run a subset; only a FULL run records a receipt.
#   -j N is a worker count and nothing else: it goes to the runner as GT_TEST_JOBS=N, so a run
#   with -j and no selector is still FULL and runs both gates (0.20.0; see step 4).
#   --affected (0.18.1): the tests mapped to this branch's changes (tests/prun.py --affected,
#   computed HERE, where the git history is) run on the runner; a pass records a SCOPED receipt
#   here, which the commit guard accepts on a feature branch only.
#   The runner: --host, else $GT_REMOTE_TEST_HOST, else the first of the gt setting `runners`,
#   else claudebox.
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
#   4. on a pass, read the run's verdict line back -- `gt-gates: scope=... tests=... secrets=...
#      code=...`, the last line tests/run.sh prints -- and re-fingerprint the local tree. Only if
#      the verdict says the FULL suite passed AND both gates (secrets, code) passed, and the tree
#      is UNCHANGED since the snapshot, does this machine record a receipt (gt_test_receipt.py),
#      with what = "remote:tests/run.sh@<host>" and the three gate verdicts on it.
#      Until 0.20.0 this passed `-j N` to tests/run.sh, which took it for a test selector and ran
#      a "subset" with no gates -- and this script recorded a full receipt from the exit code
#      alone. Every remote receipt on 2026-10-03 had skipped the secrets scan. No verdict line,
#      or one that does not name all three as pass, means no receipt.
#      A receipt is local and file-time based (gt_test_receipt.py: "A receipt means nothing on
#      another machine"), so the Mac writes it, and only for the exact tree that passed.
#
# Prints the wall time of each step. Exit: 0 passed and receipt recorded · 1 tests or a gate failed ·
# 2 usage/ssh/ship failed · 3 passed, but the local tree changed during the run (no receipt).
set -uo pipefail
HOST=""; JOBS=""; KEEP=no; AFFECTED=no
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --affected) AFFECTED=yes; shift ;;
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
if [ -z "$HOST" ]; then HOST="${GT_REMOTE_TEST_HOST:-}"; fi
if [ -z "$HOST" ]; then
  HOST=$(python3 -c 'import json,os
try: print((json.load(open(os.path.expanduser("~/.claude/vault-config.json"))).get("runners") or "").split(",")[0].strip())
except Exception: pass' 2>/dev/null)
fi
[ -n "$HOST" ] || HOST=claudebox
case "$JOBS" in ''|*[!0-9]*) [ -z "$JOBS" ] || { echo "-j wants a number, got: $JOBS"; exit 2; } ;; esac
AFF_JSON=""; LOG=""; KEEP_LOG=no
# Its own temp files go when it exits (0.20.0: the leak check in tests/prun.py caught both left
# behind on every run). A FAILED run keeps its log, because the failure message names it.
cleanup() { [ -z "$AFF_JSON" ] || rm -f "$AFF_JSON"
            [ -z "$LOG" ] || [ "$KEEP_LOG" = yes ] || rm -f "$LOG"; }
trap cleanup EXIT
if [ "$AFFECTED" = yes ]; then
  # A full template, not `mktemp -t NAME`: GNU mktemp refuses a template with no X's, and the
  # empty result then named a file in the CURRENT directory -- inside the tree being fingerprinted.
  AFF_JSON=$(mktemp "${TMPDIR:-/tmp}/gt-affected.XXXXXX") || { echo "FAILED: mktemp"; exit 2; }
  ( cd "$PLUGIN/tests" && python3 prun.py --print-affected ) > "$AFF_JSON" || { echo "FAILED: --affected mapping"; exit 2; }
  SEL=$(python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
print("" if d["full"] else " ".join(d["modules"]) or "-")' "$AFF_JSON")
  if [ "$SEL" = "-" ]; then echo "--affected: no code changed; nothing to run"; exit 0; fi
  echo "--affected: ${SEL:-the FULL suite (an unmapped change)}"
  # shellcheck disable=SC2086
  set -- $SEL
fi
# The runner has no git history to map changes with, so an --affected subset is sent as plain
# selectors -- plus --gates, so the secrets and code gates still run on it: a scoped receipt
# licenses a commit too. (Until 0.20.0 they did not, and the scoped receipt was recorded anyway.)
GATES_OPT=""; [ "$AFFECTED" = yes ] && [ $# -gt 0 ] && GATES_OPT="--gates"
SELQ=""; [ $# -gt 0 ] && SELQ=$(printf '%q ' "$@")
# From git, not by stripping $ROOT off $PLUGIN: in Git Bash `pwd -P` says /tmp/... while git says
# C:/Users/.../Temp/..., the strip did nothing, and the runner was told to cd into an absolute path.
REL=$(git -C "$PLUGIN" rev-parse --show-prefix); REL="${REL%/}"; [ -n "$REL" ] || REL=.
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
# Only paths that exist: `git ls-files -co` still lists a tracked file deleted from the working
# tree, and tar failing to stat it aborted the ship ("FAILED: ship") for an ordinary deletion.
( cd "$ROOT" && git ls-files -co --exclude-standard -z \
    | python3 -c 'import os, sys
sys.stdout.buffer.write(b"".join(p + b"\0" for p in sys.stdin.buffer.read().split(b"\0") if p and os.path.lexists(p)))' \
    | COPYFILE_DISABLE=1 tar --no-xattrs --null -T - -czf - 2>/dev/null ) \
  | ssh -o BatchMode=yes "$HOST" "mkdir -p ~/gt-remote/$RUNID && tar -xzf - -C ~/gt-remote/$RUNID" \
  || { echo "FAILED: ship"; exit 2; }
T2=$(now); echo "   shipped in $(secs "$T1" "$T2")"

echo "== 3. run tests/run.sh on $HOST"
# Every temp file of the run -- the throwaway HOME and TMPDIR for every test -- lives in ONE
# per-run directory on the runner's disk, <base>/gt-test-<uid>/<run id> (base: $GT_REMOTE_TMP, default
# /var/tmp), which the run removes itself.
# Not /tmp: on claudebox2 /tmp is a 3.9 GB tmpfs, and the temp dirs some tests never clean up
# (~230 per full run, gt-dem-* ~118 MB each) filled it until runs failed with ENOSPC (2026-10-04).
# Not under the runner's HOME either: gt_schedule.py treats any HOME inside the real one as the
# real user and tries to reload launchd jobs from a sandbox install (test_gt_doctor_postinstall
# caught it). The per-uid parent is 700, because /var/tmp is shared.
RTMP="${GT_REMOTE_TMP:-/var/tmp}"
case "$RTMP" in *[!A-Za-z0-9_./:-]*|"") echo "GT_REMOTE_TMP must be a plain path"; exit 2 ;; esac
REMOTE_CMD="cd ~/gt-remote/$RUNID/$REL && P=$RTMP/gt-test-\$(id -u) && mkdir -p \$P && chmod 700 \$P && T=\$P/$RUNID && mkdir -p \$T/home && umask 022 && HOME=\$T/home TMPDIR=\$T GT_TEST_TMPDIR=\$T PYTHONDONTWRITEBYTECODE=1 ${JOBS:+GT_TEST_JOBS=$JOBS }bash tests/run.sh $GATES_OPT $SELQ 2>&1; rc=\$?; rm -rf \$T; exit \$rc"
LOG=$(mktemp "${TMPDIR:-/tmp}/gt-remote-test.XXXXXX") || { echo "FAILED: mktemp"; exit 2; }
ssh -o BatchMode=yes "$HOST" "$REMOTE_CMD" | tee "$LOG"
RC=${PIPESTATUS[0]}
T3=$(now)
# The run's temp dir goes even with --keep, and even if the run's own cleanup never ran (a dropped
# connection kills the remote shell before its `rm`).
CLEAN="$RTMP/gt-test-\$(id -u)/$RUNID"; [ "$KEEP" = yes ] || CLEAN="$CLEAN ~/gt-remote/$RUNID"
ssh -o BatchMode=yes "$HOST" "rm -rf $CLEAN" || true
echo ""
echo "== timing: ship $(secs "$T1" "$T2") · run $(secs "$T2" "$T3") · total $(secs "$T0" "$T3")"
# One execution row HERE (gt_metrics.py): the runner's own HOME is thrown away with the run.
GM=$(ls -d "$PLUGIN"/golden-thread/*/scripts/gt_metrics.py 2>/dev/null | sort -V | tail -1)
if [ -n "$GM" ]; then
  python3 "$GM" record --repo "$ROOT" --project "${GT_METRICS_PROJECT:-golden-thread}" \
    --process "tests:remote" --duration "$(python3 -c "print(float('$T3')-float('$T2'))")" \
    --exit "$RC" --trait remote $( [ "$AFFECTED" = yes ] && echo "--scope scoped" ) \
    $( [ $# -gt 0 ] && [ "$AFFECTED" = no ] && echo "--trait subset" ) >/dev/null 2>&1 || true
fi
# The run's own verdict: the LAST non-empty line, and only if it is a gt-gates line (tests/run.sh
# prints it last). Not "the last gt-gates line anywhere": a failing unit's output is printed
# verbatim and may quote a nested run's verdict line.
GATELINE=$(grep -v '^[[:space:]]*$' "$LOG" | tail -1 | tr -d '\r' | grep '^gt-gates: ')
gate() { printf '%s\n' "$GATELINE" | tr ' ' '\n' | sed -n "s/^$1=//p" | tail -1; }
G_SCOPE=$(gate scope); G_TESTS=$(gate tests); G_SEC=$(gate secrets); G_CODE=$(gate code)
# The count the run itself reports: the last "Ran N tests" line can be a failing unit's own output.
COUNT=$(gate count); [ -n "$COUNT" ] || COUNT=$(sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' "$LOG" | tail -1)
echo "== gates on $HOST: ${GATELINE:-NONE -- the run printed no gt-gates verdict line}"
if [ "$RC" -ne 0 ]; then
  KEEP_LOG=yes
  echo "FAILED on $HOST (exit $RC): tests=${G_TESTS:-?} secrets=${G_SEC:-?} code=${G_CODE:-?}. Log: $LOG"; exit 1
fi

echo "== 4. receipt"
if [ "$AFFECTED" = yes ] || [ $# -eq 0 ]; then
  if [ "$G_TESTS" != pass ] || [ "$G_SEC" != pass ] || [ "$G_CODE" != pass ]; then
    echo "PASSED on $HOST by exit code, but the verdict line does not name tests, secrets and code"
    echo "all as pass (${GATELINE:-no verdict line}) -- no receipt."; exit 1
  fi
fi
GATE_ARGS=(--gate "tests=$G_TESTS" --gate "secrets=$G_SEC" --gate "code=$G_CODE")
if [ "$AFFECTED" = yes ]; then
  if [ "$(fingerprint)" != "$FP" ]; then
    echo "PASSED on $HOST, but the local tree changed during the run — no receipt. Run again."; exit 3
  fi
  for d in $(ls -d "$PLUGIN"/golden-thread/*/scripts | sort -V -r); do
    [ -f "$d/gt_test_receipt.py" ] || continue
    python3 "$d/gt_test_receipt.py" record --repo "$ROOT" --what "remote:tests/run.sh --affected@$HOST" \
      --tests "${COUNT:-0}" --ok --scope scoped --files-from "$AFF_JSON" "${GATE_ARGS[@]}" >/dev/null \
      && { echo "scoped receipt recorded: ${COUNT:-?} tests passed on $HOST (feature branches only)"; exit 0; }
  done
  echo "PASSED on $HOST, but no gt_test_receipt.py was found to record the receipt"; exit 2
fi
if [ $# -gt 0 ]; then echo "PASSED on $HOST (subset: $*). A subset never records a receipt."; exit 0; fi
if [ "$G_SCOPE" != full ]; then
  echo "PASSED on $HOST, but the run reports scope=${G_SCOPE:-?}, not full -- no receipt."; exit 1
fi
if [ "$(fingerprint)" != "$FP" ]; then
  echo "PASSED on $HOST, but the local tree changed during the run — no receipt. Run again."; exit 3
fi
for d in $(ls -d "$PLUGIN"/golden-thread/*/scripts | sort -V -r); do
  [ -f "$d/gt_test_receipt.py" ] || continue
  python3 "$d/gt_test_receipt.py" record --repo "$ROOT" --what "remote:tests/run.sh@$HOST" \
    --tests "${COUNT:-0}" --ok "${GATE_ARGS[@]}" >/dev/null \
    && { echo "receipt recorded: ${COUNT:-?} tests, secrets and code gates passed on $HOST"; exit 0; }
done
echo "PASSED on $HOST, but no gt_test_receipt.py was found to record the receipt"; exit 2
