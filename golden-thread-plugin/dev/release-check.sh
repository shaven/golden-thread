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

# Every plugin this repo ships, by the ONE discovery rule in dev/plugins.py (shared with
# package.sh, sync-gt-src.sh and the Python gates): "<dir> <version> <name>" per line.
# Until 0.13.0 this file named gt and gt-wiki by hand, so a third plugin would have
# passed the gate unchecked. gt ("golden-thread") stays named below only for the steps
# that run gt's own tooling.
PDIRS=(); PNAMES=(); PLABEL=""; GTV=""
while read -r d v n; do
  PDIRS+=("$d/$v"); PNAMES+=("$n"); PLABEL="${PLABEL:+$PLABEL, }$n $v"
  [ "$d" = golden-thread ] && GTV="$v"
done < <(python3 dev/plugins.py list)
[ -n "$GTV" ] || { echo "release-check: no installable golden-thread version directory"; exit 1; }
GT="golden-thread/$GTV"
echo "release-check: $PLABEL"

step "syntax"
n=0; while IFS= read -r f; do n=$((n+1)); bash -n "$f" 2>/dev/null || bad "bash -n $f"; done < <(
  { ls *.sh; find "${PDIRS[@]}" dev tests -name '*.sh'; find "$GT/templates/githooks" -type f; } 2>/dev/null | sort -u)
ok "bash -n on $n shell files"
PYERR=$(python3 - "${PDIRS[@]}" dev tests . <<'PY'
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
for d in "${PDIRS[@]}"; do
  v=${d##*/}
  pv=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$d/.claude-plugin/plugin.json")
  [ "$pv" = "$v" ] && ok "$d plugin.json says $pv" || bad "$d plugin.json says $pv, directory says $v"
done

step "modules"
# A module (a plugin dir whose newest release carries module.json, 0.14.0) must have a
# valid module.json -- no unknown keys, every listed skill/script/template present, no
# Core-rule enforcement hook -- and its requires_gt must admit the gt being released, or
# install.sh would skip it on every machine that installs this release.
NMOD=0
for d in "${PDIRS[@]}"; do
  [ -f "$d/module.json" ] || continue
  NMOD=$((NMOD+1))
  OUT=$(python3 dev/plugins.py module-check "$d" --gt "$GTV" 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "$OUT (admits gt $GTV)" || { echo "$OUT" | tail -15; bad "$d module.json invalid or does not admit gt $GTV"; }
done
[ $NMOD -gt 0 ] || ok "no modules ship in this release"

step "manifest"
# EVERY plugin ships a MANIFEST.json and it must match its tree. A plugin without one
# fails (dev/plugins.py manifest-check exits 2): until 0.13.0 only gt had hash trust, and
# gt-wiki shipped whatever sat on disk with nothing to compare it against.
for d in "${PDIRS[@]}"; do
  OUT=$(python3 dev/plugins.py manifest-check "$d" 2>&1); rc=$?
  case $rc in
    0) ok "$d MANIFEST.json files map matches the tree";;
    2) echo "  $OUT"; bad "$d ships no MANIFEST.json";;
    *) echo "  $OUT"; bad "$d MANIFEST.json is stale";;
  esac
done

step "skills"
for i in "${!PDIRS[@]}"; do
  OUT=$(python3 "$GT/scripts/skill_lint.py" "${PDIRS[$i]}" 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "skill_lint (${PNAMES[$i]})" || { echo "$OUT" | tail -15; bad "skill_lint (${PNAMES[$i]})"; }
done

step "packs"
# Every contributed pack that ships in this release must still pass the submission validator.
# A pack is merged once and then lives here forever; without this step a rule that was safe at
# review time can be edited afterwards -- by anyone with commit access, including a future
# refactor -- and nothing would notice. Re-validating in-tree is what keeps "it was reviewed"
# true rather than historical.
# PDIRS is the NEWEST version of each plugin, but install.sh <version> can install an older
# one, so an unvalidated pack in an older shipping dir would still reach a machine. Walk every
# version directory that ships (security review, 2026-09-16).
# -L, because plain `find` does not descend a SYMLINKED packs dir while install.sh's `cp -r`
# dereferences it -- the one path the gate could not see was a path the installer materialised.
# The walk is scoped to the plugin dirs and prunes .git/node_modules/tests, because an
# unscoped `find .` also validated a vendored submodule's .git/packs and any test fixture named
# *.pack.json, blocking the gate on a non-issue (security review, 2026-09-16).
NPACK=0; PACKBAD=0
while IFS= read -r PD; do
  [ -d "$PD" ] || continue
  case "$PD" in *"/.git/"*|*"/node_modules/"*|*"/tests/"*) continue;; esac
  # A symlink anywhere in the path means the content is not what the repo shows a reviewer.
  if [ "$(cd "$PD" 2>/dev/null && pwd -P)" != "$(cd "$(dirname "$PD")" 2>/dev/null && pwd -P)/$(basename "$PD")" ]; then
    PACKBAD=1; bad "packs dir is a symlink and does not ship what it appears to: $PD"
    continue
  fi
  while IFS= read -r p; do
    NPACK=$((NPACK+1))
    OUT=$(python3 dev/submissions.py validate "$p" 2>&1); RC=$?
    # The validator has THREE verdicts and only REJECT (2) is a gate failure. REVIEW (1) means
    # "a human must decide" -- and for a pack already in the tree, merging it WAS that decision.
    # Treating 1 as failure would wedge the gate permanently on the first shipped runbook or
    # vocabulary pack, whose legitimate content is imperative prose, and the obvious way out
    # would be to delete the content (review 2026-09-16, found before any such pack shipped).
    case "$RC" in
      0) ;;
      1) echo "$OUT" | tail -3
         echo "      note: REVIEW, not a failure — approved when merged, re-raised here" ;;
      *) echo "$OUT" | tail -5; PACKBAD=1
         bad "pack REJECTED by the submission validator: $(basename "$p")" ;;
    esac
  done < <(find -L "$PD" -name '*.pack.json' | sort)
done < <(find -L . -type d -name packs -not -path './.git/*' | sort)
# install.sh copies packs/ for EVERY plugin, but gt_registry reads only gt's own release and
# the module drift mapper ignores packs/ -- so a module's pack would install, never load, and
# never be integrity-checked. No module ships one; this makes that stay true out loud rather
# than by luck (review 2026-09-16).
while IFS= read -r MP; do
  case "$MP" in ./golden-thread/*) continue;; *"/.git/"*|*"/node_modules/"*|*"/tests/"*) continue;; esac
  PACKBAD=1
  bad "only the gt release may ship packs/ (nothing loads a module's): $MP"
done < <(find -L . -type d -name packs -not -path './.git/*' | sort)

if [ "$PACKBAD" -ne 0 ]; then :          # `bad` already reported it; do not also claim success
elif [ $NPACK -eq 0 ]; then ok "no packs shipped"
else ok "$NPACK shipped pack(s) still pass the submission validator"
fi

step "cli contract"
# core_explicit_vault_target requires the CALLER to name the vault. This step asserts
# the TOOLS still offer the flags that make that possible: a rule depending on a flag
# nobody implements is unfollowable. The incident is in dev/check_cli_contract.py.
OUT=$(python3 dev/check_cli_contract.py "$GT" 2>&1); rc=$?
[ $rc -eq 0 ] && ok "$OUT" || { echo "$OUT" | tail -20; bad "a vault tool can write without being told which vault"; }

step "retired"
# An upgrade from ANY older release must converge on a fresh install (owner requirement,
# 2026-09-14). install.sh can only remove what an older release left behind if the new
# release lists it in retired.json, because gt-src carries no history. Compared against the
# newest-but-one gt release on disk.
PREV=$(ls -d golden-thread/*/ 2>/dev/null | xargs -n1 basename | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | grep -vx "$GTV" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)
if [ -z "$PREV" ]; then ok "no earlier release on disk to compare against"; else
  OUT=$(python3 dev/check_retired.py "$GT" "golden-thread/$PREV" 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "$OUT" || { echo "$OUT" | tail -20; bad "a removal since $PREV is not recorded in retired.json"; }
fi

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
# Counts and version numbers, derived from the source. The rest of this step checks that a
# skill is NAMED; nothing in it can tell whether what a document SAYS is still true, and a
# stale count is the part of that which is mechanical enough to check (2026-09-16).
OUT=$(python3 dev/check_doc_counts.py 2>&1); rc=$?
[ $rc -eq 0 ] && ok "$(echo "$OUT" | tail -1)" \
  || { echo "$OUT" | sed 's/^/  /'; bad "a doc count disagrees with the code"; }
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

# The release MACHINERY needs documenting too, for the same reason the tools do: on
# 2026-09-12 three new gates and a parallel test runner shipped, and the only place any
# of them was mentioned was a changelog entry. dev/README.md is where they belong, and
# the test runner lives beside them.
dev = pathlib.Path("dev")
devdocs = {}
for n in ("dev/README.md", "CLAUDE.md"):
    f = pathlib.Path(n)
    if f.is_file():
        devdocs[n] = f.read_text(encoding="utf-8")
for f in sorted(list(dev.glob("*.py")) + list(dev.glob("*.sh"))):
    if not any(f.name in t for t in devdocs.values()):
        out.append(f"dev/{f.name} is documented nowhere (dev/README.md)")
for runner in ("tests/run.sh", "tests/prun.py"):
    name = pathlib.Path(runner).name
    if not any(name in t for t in devdocs.values()):
        out.append(f"{runner} is documented nowhere (dev/README.md)")
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
REFS=$(python3 - "${PDIRS[@]}" <<'PY2'
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
    # A MODULE requires gt (module.json requires_gt), so its skills may name gt's core
    # scripts -- the demo tour runs vault_init.py from gt. Since 0.15.0 they may also name
    # ANOTHER module's script: the tour runs gt_watch.py from the watch module, resolved at
    # run time (`gt_demo.sh module-scripts NAME`, an act skipped when that module is absent).
    # So a module's skills are checked against gt plus every plugin this release ships. A
    # name that ships in none of them still fails. (0.14.0: the demo moved out of gt.)
    if (root / "module.json").is_file():
        # Real N.N.N directories only: the glob also matched golden-thread/0.1.0-archive.zip,
        # int("0-archive") raised, and -- with the exit code unread -- the step printed ok.
        gt_core = sorted((d for d in pathlib.Path("golden-thread").iterdir()
                          if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name)),
                         key=lambda d: tuple(int(x) for x in d.name.split(".")))
        if gt_core:
            have |= {p.name for p in gt_core[-1].rglob("*") if p.is_file()}
        for other in map(pathlib.Path, sys.argv[1:]):
            have |= {p.name for p in other.rglob("*") if p.is_file()}
    for sk in root.glob("skills/*/SKILL.md"):
        for name in sorted(set(re.findall(r"\b([\w.-]+\.(?:py|sh))\b", sk.read_text(encoding="utf-8")))):
            if name not in have:
                out.append(f"{sk.parent.name} names {name}, which ships nowhere in {root}")
print("\n".join(out))
PY2
); REFS_RC=$?
# A crash is not a clean result: before 0.15.0 a traceback here left REFS empty and printed ok.
if [ "$REFS_RC" -ne 0 ]; then bad "skill reference check could not run (exit $REFS_RC)"
elif [ -z "$REFS" ]; then ok "every script/template a skill names exists in the release"
else echo "$REFS"; bad "skills reference files that do not ship"; fi

step "scrub (employer and machine names)"
# The WHOLE repository, not a list of plugin directories. Until 0.12.9 this scrubbed
# "$GT" "$WIKI" dev tests and the plugin root, so every file above golden-thread-plugin/
# -- CHANGELOG.md, README.md, docs/, and a sync handoff naming internal systems -- reached
# the public remote unscanned. Found 2026-09-13 with that file already live. --repo scans
# what a push publishes: tracked files plus untracked-but-not-ignored ones.
OUT=$("${GT_PYTHON:-python3}" dev/scrub_check.py --repo "$(git rev-parse --show-toplevel 2>/dev/null || echo "$ROOT")" 2>&1); rc=$?
case $rc in
  0) ok "$(echo "$OUT" | tail -1)";;
  1) echo "$OUT" | grep '^HIT' | head -20; bad "employer/machine strings present";;
  *) echo "$OUT" | tail -5; bad "scrub could not check (terms file missing, or pypdf absent for PDFs)";;
esac

if [ "$QUICK" = no ]; then
  step "test harness"
  # KEEP THE OUTPUT. This used to grep out the `FAIL:` lines and discard everything
  # else, so a failing gate named the test and threw away the traceback -- and the only
  # way to see why was to run it again. For a flake that is fatal: 2026-09-12 produced
  # two parallel-only failures (test_gt_demo, test_gt_edits) whose evidence was gone
  # before it could be read, which is exactly what dev/README.md warns against when it
  # says a test failing in parallel IS the finding.
  TESTLOG="${TMPDIR:-/tmp}/gt-release-check-tests.log"
  OUT=$(tests/run.sh 2>&1); rc=$?
  printf '%s\n' "$OUT" > "$TESTLOG"
  echo "$OUT" | tail -3
  if [ $rc -eq 0 ]; then
    ok "tests/run.sh"
  else
    echo "$OUT" | grep -E '^(FAIL|ERROR):' | head -20
    echo "  full output, tracebacks included: $TESTLOG"
    echo "  a unit that passes on its own is a PARALLEL-ONLY failure, which is a finding:"
    echo "    GT_TEST_SERIAL=1 tests/run.sh <module>   # compare"
    bad "tests/run.sh"
  fi
  step "selftest"
  OUT=$(./selftest.sh 2>&1); rc=$?
  [ $rc -eq 0 ] && ok "$(echo "$OUT" | tail -1)" || { echo "$OUT" | grep FAIL | head; bad "selftest.sh"; }
fi

printf '\n'
if [ $FAILS -eq 0 ]; then
  echo "RELEASE CHECK PASSED — $PLABEL"
  # The receipt core_test_before_commit reads. A --quick pass skipped the tests and the
  # selftest, so it is deliberately NOT evidence: recording one would let a commit
  # through on the strength of a check that never ran the suite.
  if [ "$QUICK" = no ]; then
    python3 "$GT/scripts/gt_test_receipt.py" record --repo . \
      --what "dev/release-check.sh" --ok >/dev/null 2>&1 || true
  fi
else
  echo "RELEASE CHECK FAILED — $FAILS step(s)"
fi
exit $FAILS
