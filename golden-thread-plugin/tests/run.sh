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

if [ "${GT_TEST_SERIAL:-}" = "1" ]; then
  if [ $# -gt 0 ]; then exec python3 -m unittest -v "$@"; fi
  exec python3 -m unittest discover -s . -p 'test_*.py' -v
fi

exec python3 prun.py "$@"
