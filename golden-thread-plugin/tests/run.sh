#!/usr/bin/env bash
# Run the Golden Thread test harness.
#
#   tests/run.sh                 # every test
#   tests/run.sh test_safe_write # one file (module name, no .py)
#
# Stdlib unittest only. Every test uses a throwaway HOME; nothing on this machine
# is touched. Complements selftest.sh, which proves the new-user install path end to
# end; these tests pin each tool's behaviour one at a time.
set -uo pipefail
cd "$(dirname "$0")"
if [ $# -gt 0 ]; then
  exec python3 -m unittest -v "$@"
fi
exec python3 -m unittest discover -s . -p 'test_*.py' -v
