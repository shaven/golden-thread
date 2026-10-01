#!/usr/bin/env bash
# sync-gt-src.sh — publish the current release to the shared working copy (gt-src).
#
#   dev/sync-gt-src.sh --dry-run    # show what would change, touch nothing
#   dev/sync-gt-src.sh              # publish, then verify what landed
#
# gt-src is what the other machine copies into its own repo and commits, so it must
# hold exactly the files that should be checked in — nothing more:
#
#   * only files git TRACKS here (no zips, caches, scratch, untracked work)
#   * only from a COMMITTED tree, so gt-src always equals one commit (recorded in
#     gt-src/SOURCE.json)
#   * the NEWEST version directory of each plugin AND the one before it, so a machine
#     installing from gt-src can roll back with `install.sh <previous>`; older releases
#     stay here (owner decision, 2026-09-14 -- newest-only left nothing to roll back to)
#   * never a single employer- or machine-specific string: dev/scrub_check.py runs on
#     the staged set first, and any hit — or any file it could not scan — aborts
#
# gt-src mirrors the REPOSITORY's layout -- the repo root (README.md, CHANGELOG.md, LICENSE,
# docs/, .github/ ...) with golden-thread-plugin/ beneath it -- exactly as GitHub holds it,
# minus release directories older than the previous one (owner, 2026-09-28: "put the files into
# the gt-src just as they should go into github"). Until then gt-src was the plugin folder
# alone, so the other side never received CHANGELOG.md, the front-door README or LICENSE.
# install.sh, selftest.sh and tests/run.sh run from gt-src/golden-thread-plugin/ unchanged.
# Only this machine writes gt-src; other machines read it and write only to
# gt-feature-requests/new/.
#
# Destination: $GT_SRC, else ~/Library/CloudStorage/OneDrive-Personal/Projects2/gt-src
set -euo pipefail
cd "$(dirname "$0")/.."
PLUGIN="$PWD"
DEST="${GT_SRC:-$HOME/Library/CloudStorage/OneDrive-Personal/Projects2/gt-src}"
DRY=no; VERIFY=yes
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=yes ;;
    --no-verify) VERIFY=no ;;      # test fixtures only: a toy tree cannot pass selftest
    *) echo "usage: sync-gt-src.sh [--dry-run] [--no-verify]"; exit 2 ;;
  esac
done
PY="${GT_PYTHON:-python3}"

# Every plugin and its newest version, by the one rule in dev/plugins.py — the same
# answer release-check.sh and package.sh get. "<dir> <version> <name>" per line.
PDIRS=(); PVERS=(); PLABEL=""; PJSON=""; GTV=""
while read -r d v n; do
  PDIRS+=("$d"); PVERS+=("$v"); PLABEL="${PLABEL:+$PLABEL, }$n $v"
  PJSON="$PJSON\"$(printf '%s' "$n" | tr -- '-' '_')\": \"$v\", "
  [ "$d" = golden-thread ] && GTV="$v"
done < <("$PY" dev/plugins.py list)
[ "${#PDIRS[@]}" -gt 0 ] || { echo "REFUSED: no installable plugin version directory found"; exit 2; }

ROOT=$(git rev-parse --show-toplevel)
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "REFUSED: the repository has uncommitted changes — commit first, so gt-src equals a commit."
  git -C "$ROOT" status --short | head -20
  exit 2
fi
COMMIT=$(git rev-parse HEAD)
PREFIX=$(git rev-parse --show-prefix)       # e.g. golden-thread-plugin/ -- where the plugin sits in the repo

# Which release directories travel: newest + the one before, per plugin, space-joined.
KEEP=()
for i in "${!PDIRS[@]}"; do
  KEEP+=("$("$PY" dev/plugins.py releases "${PDIRS[$i]}" 2 | tr '\n' ' ')")
done

# THE TESTED TREE MUST BE THE COMMITTED TREE (2026-09-29). `git status` cannot see an empty
# directory, so it called the tree clean while packs/community -- empty -- existed only here:
# 0.16.0 through 0.17.3 were tested WITH it and shipped WITHOUT it. Compare directly. The newest
# release and everything outside release dirs must match; the previous release is already
# released, so a difference there is a warning; older releases never publish.
echo "== the tested tree is the committed tree"
if ! "$PY" dev/tree_is_commit.py "$ROOT" --published; then
  echo "REFUSED: the working tree the tests ran on is not the commit being published."
  exit 2
fi

STAGE=$(mktemp -d); VCOPY=""; trap 'rm -rf "$STAGE" "$VCOPY"' EXIT
git -C "$ROOT" ls-files -z | while IFS= read -r -d '' f; do  # the WHOLE repo, root-relative
  for i in "${!PDIRS[@]}"; do                                 # older releases stay home
    case "$f" in
      "$PREFIX${PDIRS[$i]}"/*)
        rel=${f#"$PREFIX${PDIRS[$i]}"/}; ver=${rel%%/*}
        case " ${KEEP[$i]}" in *" $ver "*) ;; *) continue 2 ;; esac ;;
    esac
  done
  mkdir -p "$STAGE/$(dirname "$f")"
  cp -p "$ROOT/$f" "$STAGE/$f"
done
N=$(find "$STAGE" -type f | wc -l | tr -d ' ')
[ "$N" -gt 0 ] || { echo "REFUSED: staged 0 files — refusing to publish an empty tree"; exit 2; }

echo "== scrub"
if ! "$PY" dev/scrub_check.py "$STAGE"; then
  echo "REFUSED: scrub_check did not pass on the staged files — nothing was published."
  exit 1
fi

# CHECKSUMS (owner, 2026-09-28: "give a checksum for gt-src so the machine doing the install
# knows if it has the newest version of all the files"). SHA256SUMS lists every published file
# in the format BOTH `shasum -a 256 -c` (macOS) and `sha256sum -c` (Linux) read, and its own
# sha256 is the TREE digest in SOURCE.json -- one value to compare across machines, which
# changes if any file, name or count changes. The receiving side checks with:
#     shasum -a 256 -c SHA256SUMS          # or: sha256sum -c SHA256SUMS
#     shasum -a 256 SHA256SUMS             # must equal SOURCE.json "tree_sha256"
# Written from the STAGE, i.e. from the commit, never from what landed -- a checksum computed
# from the destination would vouch for whatever the copy happened to produce.
# The list is built OUTSIDE the stage and moved in: written inside it, the half-written list
# was itself found by `find` and listed (the test caught SHA256SUMS.tmp in its own output).
SUMS_TMP=$(mktemp)
(cd "$STAGE" && find . -type f ! -name SHA256SUMS ! -name SOURCE.json -print0 | LC_ALL=C sort -z \
   | xargs -0 shasum -a 256) > "$SUMS_TMP" && mv "$SUMS_TMP" "$STAGE/SHA256SUMS"
TREE_SHA=$(shasum -a 256 "$STAGE/SHA256SUMS" | cut -d' ' -f1)

cat > "$STAGE/SOURCE.json" <<EOF
{"commit": "$COMMIT", "layout": "repository", "plugin_path": "$PREFIX", $PJSON
 "synced_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')", "files": $N, "tree_sha256": "$TREE_SHA"}
EOF

# A file in the destination that this publisher did not write is the signal that
# something else writes here. rsync --delete removes it regardless; naming it first
# is what makes a second writer visible. See dev/foreign_files.py for the incident.
echo "== foreign files in $DEST"
FOREIGN=$("$PY" dev/foreign_files.py "$DEST" "$STAGE" 2>/dev/null || true)
if [ -n "$FOREIGN" ]; then
  COUNT=$(printf '%s\n' "$FOREIGN" | wc -l | tr -d ' ')
  echo "  $COUNT file(s) in gt-src that this publisher did not write:"
  printf '%s\n' "$FOREIGN" | head -40 | sed 's/^/    /'
  [ "$COUNT" -gt 40 ] && echo "    ... and $((COUNT - 40)) more"
  echo "  Backed up below, then removed. If any of it is work, rescue it FIRST."
else
  echo "  none - gt-src holds only what was published"
fi

echo "== changes to $DEST"
mkdir -p "$DEST"
rsync -a --delete --checksum --itemize-changes --dry-run --exclude '.DS_Store' "$STAGE/" "$DEST/" | grep -v '^\.d' || true
if [ "$DRY" = yes ]; then
  echo "(dry run — nothing written; $N files staged from $COMMIT)"
  exit 0
fi

# Back up whatever gt-src holds before replacing it, and prove the backup is real:
# a backup that silently captured nothing is worse than none, because it is trusted.
if [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  BK="$HOME/.claude/golden-thread/backups/gt-src-$(date +%Y%m%d-%H%M%S).tar.gz"
  mkdir -p "$(dirname "$BK")"
  tar -czf "$BK" -C "$DEST" .
  HAVE=$(find "$DEST" -type f ! -name .DS_Store | wc -l | tr -d ' ')
  GOT=$(tar -tzf "$BK" | grep -v '/$' | grep -vc '\.DS_Store$' || true)
  [ "$GOT" -ge "$HAVE" ] || { echo "REFUSED: backup holds $GOT of $HAVE files — not replacing gt-src"; exit 2; }
  echo "backed up $HAVE files → $BK"
fi
rsync -a --delete --checksum --exclude '.DS_Store' "$STAGE/" "$DEST/"
echo "published $N files ($PLABEL) from $COMMIT → $DEST"

# Prove what landed is complete and works — not merely that a copy ran. On
# 2026-09-11 the other machine received a gt-src with no hooks, no scripts and no
# plugin.json, and a skill that called a script that shipped nowhere; both would
# have failed here. The selftest runs on a throwaway COPY, because install.sh
# regenerates MANIFEST.json in whatever tree it installs from.
[ "$VERIFY" = no ] && { echo "(verification skipped — --no-verify)"; exit 0; }
echo "== verify $DEST"
VFAIL=0
vok()  { printf 'ok    %s\n' "$1"; }
vbad() { printf 'FAIL  %s\n' "$1"; VFAIL=$((VFAIL+1)); }
GOT=$(find "$DEST" -type f ! -name .DS_Store ! -name SOURCE.json ! -name SHA256SUMS | wc -l | tr -d ' ')
[ "$GOT" = "$N" ] && vok "$GOT files, matching the commit" || vbad "gt-src holds $GOT files, the commit $N"
if (cd "$DEST" && shasum -a 256 -c --quiet SHA256SUMS >/dev/null 2>&1); then
  vok "every file matches SHA256SUMS (tree $TREE_SHA)"
else
  vbad "a file in gt-src does not match SHA256SUMS"
fi
[ "$(shasum -a 256 "$DEST/SHA256SUMS" | cut -d' ' -f1)" = "$TREE_SHA" ] || vbad "SHA256SUMS changed in transit"
P="$DEST/$PREFIX"                     # the plugin root inside the published repo layout
for f in install.sh selftest.sh build-docs.py README.md MANUAL.md INSTALL.md tests/run.sh dev/release-check.sh dev/plugins.py; do
  [ -f "$P$f" ] || vbad "missing $PREFIX$f"
done
# The repo root arrives too -- the reason the layout changed (2026-09-28).
# Read line by line: a `for f in $(...)` loop split "Golden Thread.code-workspace" into two
# names and reported a file that was present as missing (first repo-layout publish, 0.17.3).
while IFS= read -r f; do
  [ -f "$DEST/$f" ] || vbad "missing repo-root file $f"
done < <(git -C "$ROOT" ls-files --full-name -- ':(top)*' | grep -v /)
# Every plugin arrives with its metadata AND its MANIFEST.json (hash trust, since 0.13.0).
for i in "${!PDIRS[@]}"; do
  for f in .claude-plugin/plugin.json MANIFEST.json; do
    [ -f "$P${PDIRS[$i]}/${PVERS[$i]}/$f" ] || vbad "missing $PREFIX${PDIRS[$i]}/${PVERS[$i]}/$f"
  done
done
if [ -n "$GTV" ]; then
  for d in hooks scripts skills templates; do
    [ -n "$(ls -A "${P}golden-thread/$GTV/$d" 2>/dev/null)" ] || vbad "${PREFIX}golden-thread/$GTV/$d is empty"
  done
else
  vbad "no golden-thread release in the published tree"
fi
while IFS= read -r f; do bash -n "$f" 2>/dev/null || vbad "bash -n $f"; done < <(find "$DEST" -name '*.sh')
# The published copy must LOAD its definitions, not merely hold its files (2026-09-29): gt-src
# lacked packs/community and every file-level check passed, because each compared against a
# commit that lacked it too. Ask the published release's own registry for its pack directories.
if [ -n "$GTV" ]; then
  REG_OUT=$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); import gt_registry as r; bad=[(t,d,f) for t,d,f in r.pack_dirs() if t!='local' and f]; [print('%s %s %s' % b) for b in bad]; sys.exit(1 if bad else 0)" "$DEST/${PREFIX}golden-thread/$GTV/scripts" 2>&1) \
    && vok "the published release finds every pack directory it ships (community, core)" \
    || { printf '%s\n' "$REG_OUT" | sed 's/^/    /'; vbad "the published release cannot load its pack directories"; }
fi
VCOPY=$(mktemp -d); cp -Rp "$DEST/." "$VCOPY/"
if OUT=$(cd "$VCOPY/$PREFIX" && ./selftest.sh 2>&1); then vok "$(echo "$OUT" | tail -1) — run from a copy of gt-src"; else echo "$OUT" | grep FAIL | head || true; vbad "selftest.sh from gt-src"; fi
rm -rf "$VCOPY"
[ $VFAIL -eq 0 ] && echo "gt-src VERIFIED — tree_sha256 $TREE_SHA (compare on the receiving machine: shasum -a 256 SHA256SUMS)" || { echo "gt-src FAILED verification ($VFAIL) — do not let the other machine copy it"; exit 3; }
