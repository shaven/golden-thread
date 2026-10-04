#!/usr/bin/env bash
# Run the Golden Thread test harness.
#
#   tests/run.sh                    # every test, in parallel
#   tests/run.sh test_safe_write    # one file (module name, no .py)
#   tests/run.sh -j 4               # cap the workers -- still the FULL suite, gates and all
#   GT_TEST_JOBS=4 tests/run.sh     # the same, as an environment variable
#   tests/run.sh --hosts a,b        # the full suite split across ssh runners; gates run here
#   GT_TEST_SERIAL=1 tests/run.sh   # plain unittest, one process
#   tests/run.sh --affected         # only the tests the branch's changes need (0.18.1): a pass
#                                   # records a SCOPED receipt, which the commit guard accepts
#                                   # on a feature branch only -- never at a release gate
#   tests/run.sh --gates test_x     # a subset, plus both gates (dev/remote-test.sh --affected)
#
# OPTIONS ARE NOT SELECTORS (0.20.1). A run is FULL when it names no test selector, whatever
# options it carries; only a full run runs the whole suite AND both gates (secrets, code) and
# may record a full receipt. Until 0.20.1 any argument at all -- `-j 8` included -- made the run
# a "subset": dev/remote-test.sh -j 8 skipped both gates and still recorded a receipt saying
# the suite passed. The last line of every run is a machine-readable verdict:
#   gt-gates: scope=full|scoped|subset tests=pass|fail count=N secrets=<v> code=<v>
# (<v> = pass | findings | cannot-run | partial | not-run), which dev/remote-test.sh reads back.
#
# The gates a passing receipt for this repo must carry (gt_test_receipt.py reads this line):
# gt-receipt-gates: tests secrets code
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
#
# TEMP-DIR LEAKS FAIL (0.20.1): prun.py gives every unit its own empty TMPDIR and fails any unit
# that leaves something in it, naming each entry and its size, then removes it. A leak used to be
# invisible -- a passing test that left 118 MB behind on every run looked exactly like a clean
# one. Not covered: GT_TEST_SERIAL=1 (plain unittest, one shared TMPDIR). GT_TEST_LEAK_CHECK=0
# turns it off, for diagnosis only.
set -uo pipefail
cd "$(dirname "$0")"

# Native Windows (0.20.1): in Git Bash `python3` is the Microsoft Store stub. Resolve a real
# interpreter the way the hooks do -- the newest release's hooks/gt_python.sh, which skips
# anything under ...\WindowsApps\ and is a no-op on macOS and Linux.
case "${OSTYPE:-}" in
  msys*|cygwin*)
    _gt_pysh=$(ls -d ../golden-thread/*/hooks/gt_python.sh 2>/dev/null | sort -V | tail -1)
    HERE=/nonexistent
    [ -n "$_gt_pysh" ] && . "$_gt_pysh"
    if [ -z "${GT_PYTHON:-}" ]; then
      echo "No usable Python found (the Microsoft Store Python does not count)." >&2
      exit 1
    fi
    # Unpiped, unlike the hooks' version: `tr` would hold the suite's live progress back until
    # it exited. Nothing here reads a value back from Python except digits, so \r is harmless.
    python3() { "$GT_PYTHON" "$@"; }
    export GT_PYTHON PYTHONUNBUFFERED=1
    export -f python3 ;;
esac

# A PASSING run leaves a receipt, which is what core_test_before_commit reads before
# letting a commit through. Only a FULL run counts: `tests/run.sh test_gt_lint` proves
# one module, not the tree, and a receipt from it would wave through a commit nothing
# had covered. Receipts are best-effort -- a clone with no ~/.claude still runs tests.
gate_args() {
  # The verdicts every receipt carries (0.20.1): a receipt names which gates ran.
  printf '%s\n' --gate "tests=$TESTS_V" --gate "secrets=$SEC_V" --gate "code=$CODE_V"
}

scoped_receipt() {
  # $1 test count, $2 the prun.py --affected mapping (JSON with the `files` it covers).
  # Newest release first: an older gt_test_receipt.py has no --gate and would refuse the call.
  local d
  for d in $(ls -d ../golden-thread/*/scripts 2>/dev/null | sort -V -r); do
    [ -f "$d/gt_test_receipt.py" ] || continue
    # shellcheck disable=SC2046
    python3 "$d/gt_test_receipt.py" record --repo .. --what "tests/run.sh --affected" \
      --tests "${1:-0}" --ok --scope scoped --files-from "$2" $(gate_args) >/dev/null 2>&1 || true
    return 0
  done
}

receipt() {
  # $1 is the test count parsed from the run, so the receipt says WHAT passed rather
  # than merely that something did. A receipt reading "0 tests" is exactly the kind of
  # evidence that looks like evidence.
  local count="${1:-0}" d
  for d in $(ls -d ../golden-thread/*/scripts 2>/dev/null | sort -V -r); do
    [ -f "$d/gt_test_receipt.py" ] || continue
    # shellcheck disable=SC2046
    python3 "$d/gt_test_receipt.py" record --repo .. \
      --what "tests/run.sh" --tests "$count" --ok $(gate_args) >/dev/null 2>&1 || true
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
    return 127                    # absent means unknown, and unknown is not clean
  fi
  base=(); [ -f secrets-baseline.json ] && base=(--baseline secrets-baseline.json)
  local out
  out=$(python3 "$sec" .. "${SECRETS_EXCLUDES[@]}" ${base[@]+"${base[@]}"} 2>&1) || rc=$?
  printf '%s\n' "$out"
  [ "$rc" -eq 0 ] || echo "secrets: the tree is not clean — no test receipt will be written" >&2
  # File the verdict in the vault, so "is this gate running, and is it getting noisier?"
  # is answerable from history rather than from whoever remembers this run. Bookkeeping:
  # it never changes rc, because a missing record must not fail a green suite.
  local verdict count
  case "$rc" in
    0) verdict=clean ;;
    1) verdict=findings ;;
    *) verdict=cannot-run ;;
  esac
  count=$(printf '%s\n' "$out" | sed -n 's/.*[^0-9]\([0-9]\{1,\}\) new finding(s).*/\1/p' | tail -1)
  check_report secrets "$verdict" "${count:-}" "the plugin tree"
  return $rc
}

# ── The source-validation gate ─────────────────────────────────────────────────
#
# Same posture as the secrets gate above and for the same reason: a check that did not run is
# not a check that passed. Exit 3 from the leaf means a rule was SKIPPED -- it needed an
# evaluator tier this machine has not got -- and that is reported, not swallowed.
#
# It uses the same archived-release excludes, because this repo keeps 17 version directories
# and without them every finding is reported once per release.
code_gate() {
  local sc base rc=0 out
  sc=$(ls -d ../golden-thread/*/scripts/gt_scan_code.py 2>/dev/null | sort -V | tail -1)
  if [ -z "$sc" ]; then
    echo "code: NOT RUN — no gt_scan_code.py in any release directory" >&2
    return 127                    # absent means unknown, and unknown is not clean
  fi
  base=(); [ -f ../.gt/code-baseline.json ] && base=(--baseline ../.gt/code-baseline.json)
  out=$(python3 "$sc" .. "${SECRETS_EXCLUDES[@]}" ${base[@]+"${base[@]}"} 2>&1) || rc=$?
  printf '%s\n' "$out"
  case "$rc" in
    0) check_report code clean 0 "the plugin tree" ;;
    1) echo "code: findings — no test receipt will be written" >&2
       check_report code findings "$(printf '%s\n' "$out" | sed -n 's/.*[^0-9]\([0-9]\{1,\}\) finding(s).*/\1/p' | tail -1)" "the plugin tree" ;;
    *) echo "code: the scan was PARTIAL (exit $rc) — a rule was skipped or a pack failed" >&2
       check_report code cannot-run "" "the plugin tree" ;;
  esac
  return $rc
}

# Record a check's verdict in the vault. Resolves the vault the same way every other tool
# does -- from vault-config.json -- and stays silent when there is none, because a clone
# with no vault must still be able to run the tests.
check_report() {
  local rep vault
  rep=$(ls -d ../golden-thread/*/scripts/gt_check_report.py 2>/dev/null | sort -V | tail -1)
  [ -n "$rep" ] || return 0
  vault=$(python3 - <<'EOF' 2>/dev/null
import json, os
try:
    print(json.load(open(os.path.expanduser("~/.claude/vault-config.json")))["vault_path"])
except Exception:
    pass
EOF
)
  [ -n "$vault" ] || return 0
  python3 "$rep" record --vault "$vault" --check "$1" --verdict "$2" \
    ${3:+--count "$3"} ${4:+--scope "$4"} \
    ${GT_TEST_REF:+--ref "$GT_TEST_REF"} >/dev/null 2>&1 || true
}

# ── Arguments: OPTIONS are not SELECTORS (0.20.1) ─────────────────────────────────
#
# Only a FULL run is evidence: `tests/run.sh test_gt_lint` proves one module, not the tree, and
# a receipt from it would wave through a commit nothing had covered. But "full" means "names no
# selector", not "has no arguments": `-j 8` caps the workers and changes nothing about WHAT ran.
# Until 0.20.1 this read `[ $# -eq 0 ]`, so `-j 8` (which dev/remote-test.sh always passed when
# asked for a worker count) silently turned a full run into a subset that skipped both gates.
OPTS=(); SELECTORS=(); SCOPED=no; FORCE_GATES=no; PRINT_ONLY=no
while [ $# -gt 0 ]; do
  case "$1" in
    -j|--jobs|--hosts|--base)
      [ $# -ge 2 ] || { echo "tests/run.sh: $1 needs a value" >&2; exit 2; }
      OPTS+=("$1" "$2"); shift 2 ;;
    -j?*|--jobs=*|--hosts=*|--base=*|--no-load-aware) OPTS+=("$1"); shift ;;
    --affected) SCOPED=yes; OPTS+=("$1"); shift ;;
    --gates) FORCE_GATES=yes; shift ;;
    --print-affected|-h|--help) PRINT_ONLY=yes; OPTS+=("$1"); shift ;;
    --) shift; SELECTORS+=("$@"); break ;;
    -*) echo "tests/run.sh: unknown option $1 (a selector is a test module, module.Class or module.Class.test)" >&2
        exit 2 ;;
    *) SELECTORS+=("$1"); shift ;;
  esac
done
# Runs nothing, so it proves nothing and records nothing.
[ "$PRINT_ONLY" = yes ] && exec python3 prun.py ${OPTS[@]+"${OPTS[@]}"}

FULL_RUN=no; [ ${#SELECTORS[@]} -eq 0 ] && [ "$SCOPED" = no ] && FULL_RUN=yes
RUN_GATES=no; { [ "$FULL_RUN" = yes ] || [ "$FORCE_GATES" = yes ]; } && RUN_GATES=yes
if [ "$FULL_RUN" = yes ]; then SCOPE=full; elif [ "$SCOPED" = yes ]; then SCOPE=scoped; else SCOPE=subset; fi
TESTS_V=fail; SEC_V=not-run; CODE_V=not-run; COUNT=""

# Both gates, each on its own: a secrets finding no longer hides what the code gate would say,
# and both verdicts reach the summary line. $1 is the test run's exit code; the combined code
# keeps the FIRST failure (tests, then secrets, then code), as before.
run_gates() {
  local trc=$1 src=0 crc=0
  secrets_gate || src=$?
  case $src in 0) SEC_V=pass ;; 1) SEC_V=findings ;; *) SEC_V=cannot-run ;; esac
  code_gate || crc=$?
  case $crc in 0) CODE_V=pass ;; 1) CODE_V=findings ;; 127) CODE_V=cannot-run ;; *) CODE_V=partial ;; esac
  rc=$trc
  [ "$rc" -eq 0 ] && rc=$src
  [ "$rc" -eq 0 ] && rc=$crc
  return 0
}

# The LAST line of every run. dev/remote-test.sh reads it back from the runner, and records a
# receipt only from what it says -- never from the exit code alone.
gate_line() {
  echo "gt-gates: scope=$SCOPE tests=$TESTS_V count=${COUNT:-0} secrets=$SEC_V code=$CODE_V"
}
# The suite's own count: prun's summary line ("Ran N tests in ... across M unit(s)") comes BEFORE
# the verbatim output of any failing unit, which carries "Ran k tests" lines of its own; the
# last "Ran" line is the fallback, for plain unittest (GT_TEST_SERIAL).
run_count() {
  local n
  n=$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .* across [0-9][0-9]* unit.*/\1/p' "$1" | head -1)
  [ -n "$n" ] || n=$(sed -n 's/^Ran \([0-9][0-9]*\) test.*/\1/p' "$1" | tail -1)
  printf '%s\n' "$n"
}
all_pass() { [ "$TESTS_V" = pass ] && [ "$SEC_V" = pass ] && [ "$CODE_V" = pass ]; }

LOG=$(mktemp "${TMPDIR:-/tmp}/gt-tests.XXXXXX")
if [ "$SCOPED" = yes ]; then
  AFF=$(mktemp "${TMPDIR:-/tmp}/gt-affected.XXXXXX")
  GT_AFFECTED_OUT="$AFF" python3 prun.py ${OPTS[@]+"${OPTS[@]}"} ${SELECTORS[@]+"${SELECTORS[@]}"} 2>&1 | tee "$LOG"
  rc=${PIPESTATUS[0]}
  [ $rc -eq 0 ] && TESTS_V=pass
  COUNT=$(run_count "$LOG")
  # The credential and code gates run here too: a scoped receipt licenses a commit as well.
  # An empty mapping means nothing ran ("no code changed"), and nothing is recorded.
  if [ $rc -eq 0 ] && [ -s "$AFF" ]; then run_gates "$rc"; fi
  if [ $rc -eq 0 ] && [ -s "$AFF" ] && all_pass; then
    scoped_receipt "$COUNT" "$AFF"
    echo "scoped receipt recorded: it covers the changed files on this feature branch only"
  fi
  rm -f "$LOG" "$AFF"
  gate_line
  exit $rc
fi

if [ "${GT_TEST_SERIAL:-}" = "1" ]; then
  if [ ${#SELECTORS[@]} -gt 0 ] && [ "$RUN_GATES" = no ]; then
    exec python3 -m unittest -v "${SELECTORS[@]}"
  fi
  if [ ${#SELECTORS[@]} -gt 0 ]; then
    out=$(python3 -m unittest -v "${SELECTORS[@]}" 2>&1); rc=$?
  else
    out=$(python3 -m unittest discover -s . -p 'test_*.py' -v 2>&1); rc=$?
  fi
  printf '%s\n' "$out" > "$LOG"
  printf '%s\n' "$out"
else
  # tee, not capture-then-print: the suite takes minutes and its progress must stay live.
  # PIPESTATUS[0] is the RUNNER's exit code. [1] is tee's, which is 0 whether the suite
  # passed or failed -- reading it would call every run a pass and write a receipt for a
  # red suite, which is worse than having no receipt at all.
  python3 prun.py ${OPTS[@]+"${OPTS[@]}"} ${SELECTORS[@]+"${SELECTORS[@]}"} 2>&1 | tee "$LOG"
  rc=${PIPESTATUS[0]}
fi
[ $rc -eq 0 ] && TESTS_V=pass
COUNT=$(run_count "$LOG")
rm -f "$LOG"
# Gates run on every FULL run whether or not the tests passed, so a red suite still says what
# the scans found -- and a green one can never be recorded without them.
[ "$RUN_GATES" = yes ] && run_gates "$rc"
if [ "$FULL_RUN" = yes ] && [ $rc -eq 0 ] && all_pass; then
  receipt "$COUNT"
fi
gate_line
exit $rc
