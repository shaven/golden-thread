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
#   * only the NEWEST version directory of each plugin; older releases stay here
#   * never a single employer- or machine-specific string: dev/scrub_check.py runs on
#     the staged set first, and any hit — or any file it could not scan — aborts
#
# gt-src mirrors this plugin root's layout, so install.sh, selftest.sh and
# tests/run.sh run from it unchanged. Only this machine writes gt-src; other machines
# read it and write only to gt-feature-requests/new/.
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

newest() {
  for d in "$1"/*/; do
    n=$(basename "$d"); [[ "$n" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    [ -f "$d/.claude-plugin/plugin.json" ] && echo "$n"
  done | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}
GTV=$(newest golden-thread); WV=$(newest golden-thread-wiki)

if [ -n "$(git status --porcelain -- .)" ]; then
  echo "REFUSED: the plugin tree has uncommitted changes — commit first, so gt-src equals a commit."
  git status --short -- . | head -20
  exit 2
fi
COMMIT=$(git rev-parse HEAD)
PREFIX=$(git rev-parse --show-prefix)       # e.g. golden-thread-plugin/

STAGE=$(mktemp -d); VCOPY=""; trap 'rm -rf "$STAGE" "$VCOPY"' EXIT
git ls-files -z -- . | while IFS= read -r -d '' f; do
  case "$f" in
    golden-thread/"$GTV"/*|golden-thread-wiki/"$WV"/*) ;;
    golden-thread/*|golden-thread-wiki/*) continue ;;          # older releases stay home
  esac
  mkdir -p "$STAGE/$(dirname "$f")"
  cp -p "$f" "$STAGE/$f"
done
N=$(find "$STAGE" -type f | wc -l | tr -d ' ')
[ "$N" -gt 0 ] || { echo "REFUSED: staged 0 files — refusing to publish an empty tree"; exit 2; }

echo "== scrub"
if ! "$PY" dev/scrub_check.py "$STAGE"; then
  echo "REFUSED: scrub_check did not pass on the staged files — nothing was published."
  exit 1
fi

cat > "$STAGE/SOURCE.json" <<EOF
{"commit": "$COMMIT", "path": "$PREFIX", "gt": "$GTV", "gt_wiki": "$WV",
 "synced_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')", "files": $N}
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
echo "published $N files (gt $GTV, gt-wiki $WV) from $COMMIT → $DEST"

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
GOT=$(find "$DEST" -type f ! -name .DS_Store ! -name SOURCE.json | wc -l | tr -d ' ')
[ "$GOT" = "$N" ] && vok "$GOT files, matching the commit" || vbad "gt-src holds $GOT files, the commit $N"
for f in install.sh selftest.sh build-docs.py README.md MANUAL.md INSTALL.md tests/run.sh dev/release-check.sh \
         "golden-thread/$GTV/.claude-plugin/plugin.json" "golden-thread/$GTV/MANIFEST.json" \
         "golden-thread-wiki/$WV/.claude-plugin/plugin.json"; do
  [ -f "$DEST/$f" ] || vbad "missing $f"
done
for d in hooks scripts skills templates; do
  [ -n "$(ls -A "$DEST/golden-thread/$GTV/$d" 2>/dev/null)" ] || vbad "golden-thread/$GTV/$d is empty"
done
while IFS= read -r f; do bash -n "$f" 2>/dev/null || vbad "bash -n $f"; done < <(find "$DEST" -name '*.sh')
VCOPY=$(mktemp -d); cp -Rp "$DEST/." "$VCOPY/"
if OUT=$(cd "$VCOPY" && ./selftest.sh 2>&1); then vok "$(echo "$OUT" | tail -1) — run from a copy of gt-src"; else echo "$OUT" | grep FAIL | head || true; vbad "selftest.sh from gt-src"; fi
rm -rf "$VCOPY"
[ $VFAIL -eq 0 ] && echo "gt-src VERIFIED" || { echo "gt-src FAILED verification ($VFAIL) — do not let the other machine copy it"; exit 3; }
