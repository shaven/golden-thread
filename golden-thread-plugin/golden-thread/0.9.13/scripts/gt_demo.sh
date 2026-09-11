#!/usr/bin/env bash
# gt_demo.sh — backing script for the /gt:gt-demo skill
# Commands: start | end | clean [--dry-run] | remove
#
# Paths are resolved at runtime — never hardcoded to a specific machine.
# PLUGIN_SCRIPTS_DIR is the directory containing this script (inside the plugin install).
#
# The demo runs in the user's REAL vault (vault_path from vault-config.json), which
# other sessions may be writing to at the same time. So nothing here touches a path
# the demo did not create, with one deliberate exception — `clean` resets the vault's
# git history to the snapshot — and that is guarded:
#   * `clean --dry-run` lists exactly what would be undone, for the skill to show first
#   * it refuses if any commit to be undone has already been pushed (a reset cannot
#     take back what the remote has; that would need a force push)
#   * it refuses if there is uncommitted work outside the demo's own paths, because
#     `git reset --hard` would destroy it
# `remove` commits only the demo's own paths; it never runs `git add -A`, which would
# sweep another session's half-written files into a commit.

set -euo pipefail

PLUGIN_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$PLUGIN_SCRIPTS_DIR")"
SNAPSHOT_FILE="$HOME/.claude/gt-demo-snapshot"

# Resolve vault path from config
VAULT_CONFIG="$HOME/.claude/vault-config.json"
VAULT=""
if [[ -f "$VAULT_CONFIG" ]]; then
  VAULT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('vault_path',''))" \
    "$VAULT_CONFIG" 2>/dev/null || echo "")
fi
if [[ -z "$VAULT" || ! -d "$VAULT" ]]; then
  echo "ERROR: could not read a vault_path that exists from $VAULT_CONFIG"
  echo "Run /gt:gt-init to set up your vault."
  exit 1
fi
if ! git -C "$VAULT" rev-parse --verify -q HEAD >/dev/null; then
  echo "ERROR: $VAULT is not a git repo with at least one commit."
  echo "The demo snapshots and restores through git; it cannot run without it."
  exit 1
fi

TEMPLATE_PROJECT="$PLUGIN_ROOT/templates/demo-pizzabot/project"
TEMPLATE_SOURCES="$PLUGIN_ROOT/templates/demo-pizzabot/sources"
DEMO_PROJECT_REL="Projects/demo-pizzabot"
DEMO_PROJECT_DIR="$VAULT/$DEMO_PROJECT_REL"
SOURCE_FILE=$(ls "$TEMPLATE_SOURCES/"*.md 2>/dev/null | head -1 || true)
SOURCE_REL=""
[[ -n "$SOURCE_FILE" ]] && SOURCE_REL="Sources/$(basename "$SOURCE_FILE")"

cd "$VAULT"

# Paths the demo owns. Anything else in the vault belongs to the user.
demo_paths() {
  echo "$DEMO_PROJECT_REL"
  [[ -n "$SOURCE_REL" ]] && echo "$SOURCE_REL"
  return 0
}

# Uncommitted changes to tracked files outside the demo's own paths — exactly what
# `git reset --hard` would destroy. (Untracked files survive a reset, so they are
# not listed.)
foreign_changes() {
  git status --porcelain --untracked-files=no | while IFS= read -r line; do
    path="${line:3}"
    case "$path" in
      "$DEMO_PROJECT_REL"/*|"$DEMO_PROJECT_REL") continue ;;
    esac
    [[ -n "$SOURCE_REL" && "$path" == "$SOURCE_REL" ]] && continue
    echo "$line"
  done
}

install_demo_project() {
  rm -rf "$DEMO_PROJECT_DIR"
  mkdir -p "$(dirname "$DEMO_PROJECT_DIR")"
  cp -r "$TEMPLATE_PROJECT" "$DEMO_PROJECT_DIR"
  echo "Copied demo project template to $DEMO_PROJECT_REL/"
  if [[ -n "$SOURCE_FILE" ]]; then
    mkdir -p "$VAULT/Sources"
    cp "$SOURCE_FILE" "$VAULT/Sources/"
    echo "Copied demo source: $SOURCE_REL"
  fi
}

case "${1:-}" in

# ── START ────────────────────────────────────────────────────────────────────
start)
  if [[ -f "$SNAPSHOT_FILE" ]]; then
    echo "ERROR: demo already started (snapshot exists at $SNAPSHOT_FILE)"
    echo "Run /gt:gt-demo clean first."
    exit 1
  fi

  SHA=$(git rev-parse HEAD)
  echo "$SHA" > "$SNAPSHOT_FILE"
  install_demo_project

  echo "DEMO START"
  echo "snapshot_sha=$SHA"
  echo "vault=$VAULT"
  echo "snapshot_file=$SNAPSHOT_FILE"
  echo "status=ready"
  ;;

# ── END ──────────────────────────────────────────────────────────────────────
end)
  if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    echo "ERROR: no demo in progress (no snapshot file found)"
    exit 1
  fi

  SNAPSHOT_SHA=$(cat "$SNAPSHOT_FILE")
  CURRENT_SHA=$(git rev-parse HEAD)

  echo "DEMO END"
  echo "snapshot_sha=$SNAPSHOT_SHA"
  echo "current_sha=$CURRENT_SHA"
  echo ""

  if [[ "$SNAPSHOT_SHA" == "$CURRENT_SHA" ]]; then
    echo "No commits made during demo."
  else
    echo "Commits since snapshot:"
    git log --oneline "${SNAPSHOT_SHA}..HEAD"
    echo ""
    echo "Files created/modified since snapshot:"
    git diff --name-only "${SNAPSHOT_SHA}..HEAD"
  fi
  UNCOMMITTED=$(git status --porcelain)
  if [[ -n "$UNCOMMITTED" ]]; then
    echo ""
    echo "Uncommitted changes:"
    echo "$UNCOMMITTED"
  fi
  ;;

# ── CLEAN ────────────────────────────────────────────────────────────────────
clean)
  if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    echo "ERROR: no snapshot found — nothing to clean"
    echo "Run /gt:gt-demo start before the demo."
    exit 1
  fi

  SNAPSHOT_SHA=$(cat "$SNAPSHOT_FILE")
  DRY_RUN="no"
  [[ "${2:-}" == "--dry-run" ]] && DRY_RUN="yes"

  if [[ "$DRY_RUN" == "yes" ]]; then echo "DEMO CLEAN (dry run — nothing changed)"; else echo "DEMO CLEAN"; fi
  echo "snapshot_sha=$SNAPSHOT_SHA"
  echo ""

  if ! git merge-base --is-ancestor "$SNAPSHOT_SHA" HEAD 2>/dev/null; then
    echo "REFUSED: HEAD is not a descendant of the snapshot — history was rewritten or"
    echo "switched since the demo started. Resolve by hand; nothing was changed."
    exit 2
  fi

  UNDO=$(git log --oneline "${SNAPSHOT_SHA}..HEAD")
  if [[ -n "$UNDO" ]]; then
    echo "Commits that would be undone (ALL of them, from any session):"
    echo "$UNDO"
    echo ""
    if git rev-parse --verify -q '@{upstream}' >/dev/null; then
      PUSHED=$(git log --oneline "${SNAPSHOT_SHA}..@{upstream}" 2>/dev/null || true)
      if [[ -n "$PUSHED" ]]; then
        echo "REFUSED: these commits are already on the remote, so a reset cannot take them back:"
        echo "$PUSHED"
        echo "Nothing was changed."
        exit 2
      fi
    fi
  else
    echo "No commits to undo — vault is already at the snapshot."
  fi

  FOREIGN=$(foreign_changes)
  if [[ -n "$FOREIGN" ]]; then
    echo "REFUSED: uncommitted changes outside the demo would be destroyed by the reset:"
    echo "$FOREIGN"
    echo "Commit or stash them first. Nothing was changed."
    exit 2
  fi

  if [[ "$DRY_RUN" == "yes" ]]; then
    echo "status=clean-possible"
    exit 0
  fi

  if [[ -n "$UNDO" ]]; then
    git reset -q --hard "$SNAPSHOT_SHA"
    echo "Git reset complete."
  fi

  install_demo_project
  rm "$SNAPSHOT_FILE"

  echo ""
  echo "status=clean"
  echo "vault is at sha=$(git rev-parse HEAD)"
  echo "Re-armed: run /gt:gt-demo start to begin the next demo."
  ;;

# ── REMOVE ───────────────────────────────────────────────────────────────────
remove)
  if [[ -f "$SNAPSHOT_FILE" ]]; then
    rm "$SNAPSHOT_FILE"
    echo "Removed snapshot file."
  fi

  if [[ -d "$DEMO_PROJECT_DIR" ]]; then
    rm -rf "$DEMO_PROJECT_DIR"
    echo "Removed $DEMO_PROJECT_REL/ from vault."
  fi
  if [[ -n "$SOURCE_REL" && -f "$VAULT/$SOURCE_REL" ]]; then
    rm "$VAULT/$SOURCE_REL"
    echo "Removed demo source from vault."
  fi

  # Commit the removal — of the demo's own tracked paths ONLY.
  PATHS=()
  while IFS= read -r p; do
    if [[ -n "$(git ls-files -- "$p")" ]]; then PATHS+=("$p"); fi
  done < <(demo_paths)
  if (( ${#PATHS[@]} )) && [[ -n "$(git status --porcelain -- "${PATHS[@]}")" ]]; then
    git add -A -- "${PATHS[@]}"
    git commit -q -m "Remove gt-demo artifacts (demo complete)" -- "${PATHS[@]}"
    echo "Committed removal of the demo's own files."
  fi

  # Record the choice so a later install.sh does not bring the demo back.
  if python3 "$PLUGIN_SCRIPTS_DIR/gt_settings.py" set install_demo no >/dev/null 2>&1; then
    echo "Set install_demo=no — install.sh will not reinstall the demo."
  fi

  # Strip the demo from INSTALLED copies only — never from a source checkout, which
  # is what PLUGIN_ROOT is when this script is run straight from the repo.
  for base in "$HOME/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt" "$PLUGIN_ROOT"; do
    case "$base" in "$HOME/.claude/"*) ;; *) continue ;; esac
    rm -rf "$base/skills/gt-demo" "$base/templates/demo-pizzabot" "$base/scripts/gt_demo.sh"
  done
  echo "Removed the /gt:gt-demo skill, script and templates from the installed plugin."

  echo ""
  echo "status=removed"
  echo "gt-demo is fully removed. Restart Claude Code to clear the skill from this session."
  ;;

*)
  echo "Usage: gt_demo.sh <start|end|clean [--dry-run]|remove>"
  exit 1
  ;;

esac
