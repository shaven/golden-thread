#!/usr/bin/env bash
# release-check.sh — the gate every check-in passes, on this machine, before a commit.
#
#   dev/release-check.sh            # full gate
#   dev/release-check.sh --quick    # skip selftest.sh and the test harness
#
# The scrub step reads PDFs with pypdf; point GT_PYTHON at an interpreter that has it
# (GT_PYTHON=/path/to/venv/bin/python3). Without it the scrub reports UNSCANNED and fails.
#
# It exists because each check below once failed silently and something shipped:
# an install.sh that did not parse, a MANIFEST that did not match its files, docs a
# release behind their code, a PDF with a home path in it, a template naming the
# employer's platform. Every step prints ok/FAIL; the exit code is the number of
# failed steps (0 = ready to commit).
#
# Docs are part of the gate, not a follow-up: a skill or setting the docs do not
# mention fails here, and so does any .html that has drifted from its .md.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
QUICK=no; [ "${1:-}" = "--quick" ] && QUICK=yes
FAILS=0
ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS+1)); }
step() { printf '\n== %s\n' "$1"; }

newest() {  # newest installable version dir under $1, numerically
  for d in "$1"/*/; do
    n=$(basename "$d"); [[ "$n" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    [ -f "$d/.claude-plugin/plugin.json" ] && echo "$n"
  done | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}
GTV=$(newest golden-thread); WV=$(newest golden-thread-wiki)
GT="golden-thread/$GTV"; WIKI="golden-thread-wiki/$WV"
echo "release-check: gt $GTV, gt-wiki $WV"

step "syntax"
n=0; while IFS= read -r f; do n=$((n+1)); bash -n "$f" 2>/dev/null || bad "bash -n $f"; done < <(
  { ls *.sh; find "$GT" "$WIKI" dev tests -name '*.sh'; find "$GT/templates/githooks" -type f; } 2>/dev/null | sort -u)
ok "bash -n on $n shell files"
PYERR=$(python3 - "$GT" "$WIKI" dev tests . <<'PY'
import sys, pathlib
bad = []
for root in sys.argv[1:]:
    p = pathlib.Path(root)
    files = [p] if p.is_file() else (p.glob("*.py") if root == "." else p.rglob("*.py"))
    for f in files:
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        except SyntaxError as e:
            bad.append(f"{f}:{e.lineno}: {e.msg}")
print("\n".join(bad))
PY
)
[ -z "$PYERR" ] && ok "python compiles" || { echo "$PYERR"; bad "python syntax"; }

step "versions"
for pair in "$GT:$GTV" "$WIKI:$WV"; do
  d=${pair%%:*}; v=${pair##*:}
  pv=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$d/.claude-plugin/plugin.json")
  [ "$pv" = "$v" ] && ok "$d plugin.json says $pv" || bad "$d plugin.json says $pv, directory says $v"
done

step "manifest"
python3 - "$GT" <<'PY' && ok "MANIFEST.json files map matches the tree" || bad "MANIFEST.json is stale — run: python3 $GT/scripts/gt_components.py manifest $GT"
import json, sys, importlib.util, pathlib
d = sys.argv[1]
spec = importlib.util.spec_from_file_location("c", f"{d}/scripts/gt_components.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
have = json.load(open(f"{d}/MANIFEST.json"))
want = m.build_manifest(d)
sys.exit(0 if have["files"] == want["files"] and have.get("hooks") == want["hooks"] else 1)
PY

step "skills"
OUT=$(python3 "$GT/scripts/skill_lint.py" "$GT" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "skill_lint (gt)" || { echo "$OUT" | tail -15; bad "skill_lint (gt)"; }
OUT=$(python3 "$GT/scripts/skill_lint.py" "$WIKI" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "skill_lint (gt-wiki)" || { echo "$OUT" | tail -15; bad "skill_lint (gt-wiki)"; }

step "docs"
OUT=$(python3 build-docs.py 2>&1); rc=$?
[ $rc -eq 0 ] && ok "every .html matches its .md" || { echo "$OUT" | tail -12; bad "docs drifted — fix the .md, then ./build-docs.py --build"; }
MISSING=$(python3 - "$GT" <<'PY'
import sys, pathlib, re, importlib.util
gt = pathlib.Path(sys.argv[1])
docs = {n: pathlib.Path(n).read_text(encoding="utf-8") for n in ("README.md", "MANUAL.md", "golden-thread-docs.md")}
out = []
for s in sorted(p.name for p in (gt / "skills").iterdir() if (p / "SKILL.md").is_file()):
    for n, t in docs.items():
        if s not in t:
            out.append(f"skill {s} not mentioned in {n}")
spec = importlib.util.spec_from_file_location("s", str(gt / "scripts" / "gt_settings.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manual = docs["MANUAL.md"]
for k in m.SETTINGS:
    if f"`{k}`" not in manual:
        out.append(f"setting {k} not documented in MANUAL.md")
print("\n".join(out))
PY
)
[ -z "$MISSING" ] && ok "every skill in README, MANUAL, golden-thread-docs; every setting in MANUAL" || { echo "$MISSING"; bad "docs do not cover the release"; }
STALE=$(for f in README.md MANUAL.md golden-thread-docs.md ONBOARDING.md; do grep -q "$GTV" "$f" || echo "$f never names $GTV"; done)
[ -z "$STALE" ] && ok "docs name the current version $GTV" || { echo "$STALE"; bad "docs a version behind"; }
for pdf in *.pdf; do
  base=${pdf%.pdf}
  src="$base.md"; [ -f "$src" ] || src="$base.html"
  [ "$pdf" -nt "$src" ] || echo "  note: $pdf is older than $src — re-render (./build-docs.py --pdf)"
done

step "scrub (employer and machine names)"
OUT=$("${GT_PYTHON:-python3}" dev/scrub_check.py "$GT" "$WIKI" dev tests *.md *.html *.pdf *.sh *.py 2>&1); rc=$?
case $rc in
  0) ok "$(echo "$OUT" | tail -1)";;
  1) echo "$OUT" | grep '^HIT' | head -20; bad "employer/machine strings present";;
  *) echo "$OUT" | tail -5; bad "scrub could not check (terms file missing, or pypdf absent for PDFs)";;
esac

if [ "$QUICK" = no ]; then
  step "test harness"
  OUT=$(tests/run.sh 2>&1); rc=$?
  echo "$OUT" | tail -3
  [ $rc -eq 0 ] && ok "tests/run.sh" || { echo "$OUT" | grep -E '^(FAIL|ERROR):' | head -20; bad "tests/run.sh"; }
  step "selftest"
  OUT=$(./selftest.sh 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "$(echo "$OUT" | tail -1)" || { echo "$OUT" | grep FAIL | head; bad "selftest.sh"; }
fi

printf '\n'
if [ $FAILS -eq 0 ]; then echo "RELEASE CHECK PASSED — gt $GTV, gt-wiki $WV"; else echo "RELEASE CHECK FAILED — $FAILS step(s)"; fi
exit $FAILS
