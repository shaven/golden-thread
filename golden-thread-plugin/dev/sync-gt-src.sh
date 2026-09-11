#!/usr/bin/env bash
# sync-gt-src.sh — publish the current release to the shared working copy (gt-src).
#
#   dev/sync-gt-src.sh --dry-run    # show what would change, touch nothing
#   dev/sync-gt-src.sh              # publish
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
DRY=no; [ "${1:-}" = "--dry-run" ] && DRY=yes
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

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
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
