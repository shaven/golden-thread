#!/usr/bin/env bash
# Run the Golden Thread test harness.
#
#   tests/run.sh                    # every test, in parallel
#   tests/run.sh test_safe_write    # one file (module name, no .py)
#   tests/run.sh -j 4               # cap the workers
#   GT_TEST_SERIAL=1 tests/run.sh   # plain unittest, one process
#
# Stdlib only. Every test uses a throwaway HOME; nothing on this machine is touched.
# Complements selftest.sh, which proves the new-user install path end to end; these
# tests pin each tool's behaviour one at a time.
#
# PARALLEL BY DEFAULT (2026-09-12): one process per TestCase class. The suite is I/O-bound
# almost end to end — each test shells out to install.sh, vault_init or a hook and then
# waits — so sixteen cores sat idle while the wall clock ran. Measured here: 509s
# serial, 109s parallel, the same 672 tests passing.
#
# GT_TEST_SERIAL=1 falls back to plain `unittest`, and is worth keeping: if a test fails
# in parallel and passes serially, that difference is itself the finding.
set -uo pipefail
cd "$(dirname "$0")"

# A PASSING run leaves a receipt, which is what core_test_before_commit reads before
# letting a commit through. Only a FULL run counts: `tests/run.sh test_gt_lint` proves
# one module, not the tree, and a receipt from it would wave through a commit nothing
# had covered. Receipts are best-effort -- a clone with no ~/.claude still runs tests.
receipt() {
  # $1 is the test count parsed from the run, so the receipt says WHAT passed rather
  # than merely that something did. A receipt reading "0 tests" is exactly the kind of
  # evidence that looks like evidence.
  local count="${1:-0}"
  for d in ../golden-thread/*/scripts; do
    [ -f "$d/gt_test_receipt.py" ] || continue
    python3 "$d/gt_test_receipt.py" record --repo .. \
      --what "tests/run.sh" --tests "$count" --ok >/dev/null 2>&1 || true
    return 0
  done
}

# ── The secrets gate ───────────────────────────────────────────────────────────
#
# A FULL passing run writes a receipt, and that receipt is what core_test_before_commit
# reads to license a commit. So this runs BEFORE the receipt: a credential in the tree must
# not be licensed by a green suite.
#
# It BLOCKS rather than warns, and it has earned the right to. Measured 2026-09-27 on this
# repo: 231 files scanned, ONE finding, and that one is a fixture in the file that tests the
# secrets rule. A deterministic check at that rate is exactly the kind that should stop a
# commit; a judgement-based one would not be.
#
# The excludes are the ARCHIVED release directories, and they are not cosmetic: without
# them the scan reported 48 findings, 38 of which were four files counted once per shipped
# release, because this repo keeps eighteen version directories. An immutable published
# artefact is a git-history question, not an "am I about to commit this" question.
#
# A finding you have decided to accept goes in the baseline, not in a wider exclude:
#   python3 <release>/scripts/gt_secrets.py . <excludes> --write-baseline tests/secrets-baseline.json
SECRETS_EXCLUDES=(--exclude 'golden-thread/0.1[0-5].*'
                  --exclude 'golden-thread/0.16.[0-4]'
                  --exclude 'golden-thread-*/0.*')

secrets_gate() {
  local sec base rc=0
  # The NEWEST release's scanner, the same way install.sh picks a version: a gate that
  # silently used an old copy of itself would be the very failure this repo keeps finding.
  sec=$(ls -d ../golden-thread/*/scripts/gt_secrets.py 2>/dev/null | sort -V | tail -1)
  if [ -z "$sec" ]; then
    echo "secrets: NOT RUN — no gt_secrets.py in any release directory" >&2
    return 1                      # absent means unknown, and unknown is not clean
  fi
  base=(); [ -f secrets-baseline.json ] && base=(--baseline secrets-baseline.json)
  python3 "$sec" .. "${SECRETS_EXCLUDES[@]}" ${base[@]+"${base[@]}"} || rc=$?
  [ "$rc" -eq 0 ] || echo "secrets: the tree is not clean — no test receipt will be written" >&2
  return $rc
}

# Only a FULL run is evidence: `tests/run.sh test_gt_lint` proves one module, not the
# tree, and a receipt from it would wave through a commit nothing had covered.
FULL_RUN=no; [ $# -eq 0 ] && FULL_RUN=yes

if [ "${GT_TEST_SERIAL:-}" = "1" ]; then
  if [ $# -gt 0 ]; then exec python3 -m unittest -v "$@"; fi
  out=$(python3 -m unittest discover -s . -p 'test_*.py' -v 2>&1); rc=$?
  printf '%s\n' "$out"
  if [ $rc -eq 0 ] && [ "$FULL_RUN" = yes ]; then
    secrets_gate || rc=$?
  fi
  if [ $rc -eq 0 ] && [ "$FULL_RUN" = yes ]; then
    receipt "$(printf '%s\n' "$out" | sed -n 's/^Ran \([0-9]*\) test.*/\1/p' | tail -1)"
  fi
  exit $rc
fi

# tee, not capture-then-print: the suite takes minutes and its progress must stay live.
# PIPESTATUS[0] is the RUNNER's exit code. [1] is tee's, which is 0 whether the suite
# passed or failed -- reading it would call every run a pass and write a receipt for a
# red suite, which is worse than having no receipt at all.
python3 prun.py "$@" 2>&1 | tee /tmp/gt-tests-$$.log
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ] && [ "$FULL_RUN" = yes ]; then
  secrets_gate || rc=$?
fi
if [ $rc -eq 0 ] && [ "$FULL_RUN" = yes ]; then
  receipt "$(sed -n 's/^Ran \([0-9]*\) test.*/\1/p' /tmp/gt-tests-$$.log | tail -1)"
fi
rm -f /tmp/gt-tests-$$.log
exit $rc
