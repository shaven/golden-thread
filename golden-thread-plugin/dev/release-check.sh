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

step "cli contract"
# core_explicit_vault_target requires the CALLER to name the vault. This step asserts
# the TOOLS still offer the flags that make that possible: a rule depending on a flag
# nobody implements is unfollowable. The incident is in dev/check_cli_contract.py.
OUT=$(python3 dev/check_cli_contract.py "$GT" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "$OUT" || { echo "$OUT" | tail -20; bad "a vault tool can write without being told which vault"; }

step "installer version"
# install.sh is what a user RUNS and it is NOT covered by MANIFEST.json, which hashes
# only what lives inside a version directory. On 2026-09-12 an installer fix was
# committed with no bump, so two published states both called themselves 0.12.2 — one
# whose installer wires the enforcement hooks and one whose installer does not.
OUT=$(python3 dev/check_installer_version.py . 2>&1); rc=$?
[ $rc -eq 0 ] && ok "$OUT" || { echo "$OUT"; bad "the installer changed without a version bump"; }

step "wiring coverage"
# Does every shipped item REACH its destination? Proven by installing into a throwaway
# HOME and asking each file where it ended up — including the upgrade path, where a
# vault already exists and a newly shipped hook has to be registered.
#
# Every other check here reported "clean" while guard_vault_writes.sh shipped inert
# through 0.12.0, 0.12.1 and 0.12.2: the manifest matched, the file was present, the
# registration list agreed with itself. Only doing the install catches it.
OUT=$(python3 dev/check_wiring_coverage.py "$GT" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "$OUT" || { echo "$OUT" | tail -20; bad "a shipped item does not reach its destination"; }

step "docs"
OUT=$(python3 build-docs.py 2>&1); rc=$?
[ $rc -eq 0 ] && ok "every .html matches its .md" || { echo "$OUT" | tail -12; bad "docs drifted — fix the .md, then ./build-docs.py --build"; }
MISSING=$(python3 - "$GT" <<'PY'
import sys, pathlib, re, importlib.util
gt = pathlib.Path(sys.argv[1])
# The REPO-ROOT README is the page GitHub shows, and it sat outside this check
# because release-check runs from the plugin directory -- so "README.md" meant the
# plugin's. It was three skills behind when that was noticed (2026-09-11). The page
# most people see was the one nothing verified.
names = ["README.md", "MANUAL.md", "golden-thread-docs.md"]
root_readme = pathlib.Path("..") / "README.md"
docs = {n: pathlib.Path(n).read_text(encoding="utf-8") for n in names}
if root_readme.is_file():
    docs["../README.md (repo root)"] = root_readme.read_text(encoding="utf-8")
out = []
for s in sorted(p.name for p in (gt / "skills").iterdir() if (p / "SKILL.md").is_file()):
    for n, t in docs.items():
        if s not in t:
            out.append(f"skill {s} not mentioned in {n}")

# Vault tools are COMMANDS a user runs, not skills, so nothing covered them. gt_log
# and gt_adr are the two you must use rather than writing a shared file by hand, so
# a release that ships them undocumented is a release nobody can adopt.
for tool in sorted(t.name for t in (gt / "templates" / "tools").glob("gt_*.py")):
    stem = tool[:-3]
    if not any(stem in t for t in docs.values()):
        out.append(f"vault tool {tool} documented nowhere (README/MANUAL/docs)")
spec = importlib.util.spec_from_file_location("s", str(gt / "scripts" / "gt_settings.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manual = docs["MANUAL.md"]
for k in m.SETTINGS:
    if f"`{k}`" not in manual:
        out.append(f"setting {k} not documented in MANUAL.md")
print("\n".join(out))
PY
)
[ -z "$MISSING" ] && ok "every skill in README, MANUAL, golden-thread-docs; every setting in MANUAL" || { echo "$MISSING"; bad "docs do not cover the release"; }
# ../CHANGELOG.md is included deliberately: a changelog nobody checks is the first
# document to go stale, and it is the one a stranger trusts most.
STALE=$(for f in README.md MANUAL.md golden-thread-docs.md ONBOARDING.md ../README.md ../CHANGELOG.md; do
  [ -f "$f" ] || continue
  grep -q "$GTV" "$f" || echo "$f never names $GTV"
done)
[ -z "$STALE" ] && ok "docs name the current version $GTV" || { echo "$STALE"; bad "docs a version behind"; }
STALEPDF=""
for pdf in *.pdf; do
  base=${pdf%.pdf}
  for src in "$base.md" "$base.html"; do
    [ -f "$src" ] && [ "$src" -nt "$pdf" ] && STALEPDF="$STALEPDF $pdf(<$src)"
  done
done
[ -z "$STALEPDF" ] && ok "every PDF is newer than its sources" || { echo "  stale:$STALEPDF"; bad "PDFs a render behind — run dev/render-pdfs.sh"; }
REFS=$(python3 - "$GT" "$WIKI" <<'PY2'
import sys, re, pathlib
out = []
for root in map(pathlib.Path, sys.argv[1:]):
    for sk in root.glob("skills/*/SKILL.md"):
        # Release-relative paths only; an installed path (~/.claude/golden-thread/hooks/x.py)
        # is checked by filename below, since install.sh copies scripts there.
        for ref in sorted(set(re.findall(r"(?<![\w/.-])((?:scripts|templates|hooks)/[\w./-]+\.(?:py|sh|md|json))\b", sk.read_text(encoding="utf-8")))):
            if not (root / ref).exists():
                out.append(f"{sk.parent.name} names {ref}, which is not in {root}")
    # Any script a skill names, by filename, wherever it says it lives: on 2026-09-11 a
    # skill ran <vault>/Projects/golden-thread/gt-demo.sh, which shipped nowhere.
    have = {p.name for p in root.rglob("*") if p.is_file()} | {p.name for p in pathlib.Path(".").iterdir() if p.is_file()}
    for sk in root.glob("skills/*/SKILL.md"):
        for name in sorted(set(re.findall(r"\b([\w.-]+\.(?:py|sh))\b", sk.read_text(encoding="utf-8")))):
            if name not in have:
                out.append(f"{sk.parent.name} names {name}, which ships nowhere in {root}")
print("\n".join(out))
PY2
)
[ -z "$REFS" ] && ok "every script/template a skill names exists in the release" || { echo "$REFS"; bad "skills reference files that do not ship"; }

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
