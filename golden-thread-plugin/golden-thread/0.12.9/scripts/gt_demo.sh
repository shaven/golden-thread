#!/usr/bin/env bash
# gt_demo.sh — backing script for the /gt:gt-demo skill
# Commands: start | end | clean | remove | status | upstream-release
#
# The demo runs in its OWN throwaway vault, never the user's. Until 0.9.14 it ran in
# the real vault and "clean" rewound that vault's git history to a snapshot — which,
# in a vault other sessions write to, can take their work with it; every guard added
# around that reset found another hole. A separate vault removes the reset entirely:
# clean deletes the demo vault and builds it again.
#
#   demo vault   $GT_DEMO_VAULT, else ~/.claude/golden-thread/demo-vault
#                (under ~/.claude so it is never synced, indexed or backed up as work)
#   marker       <demo vault>/.demo/DEMO_VAULT — clean and remove delete a directory
#                only if it carries this marker, so a mis-set path cannot aim them at
#                anything real
#
# The user's vault-config.json is never written: the demo session is opened with
# GT_VAULT pointing at the demo vault, which the hooks and every skill honor.

set -euo pipefail

PLUGIN_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$PLUGIN_SCRIPTS_DIR")"
TEMPLATE="$PLUGIN_ROOT/templates/demo-pizzabot"
DEMO="${GT_DEMO_VAULT:-$HOME/.claude/golden-thread/demo-vault}"
MARKER="$DEMO/.demo/DEMO_VAULT"
SLUG="demo-pizzabot"

is_demo_vault() { [[ -f "$MARKER" ]]; }

wipe() {
  if [[ -e "$DEMO" ]] && ! is_demo_vault; then
    echo "REFUSED: $DEMO exists but is not a demo vault (no .demo/DEMO_VAULT marker) — not deleting it."
    exit 2
  fi
  rm -rf "$DEMO"
}

# A transcript for act 1, so the Stop hook's secret check can be shown without Claude
# ever typing a credential-shaped string. The key is assembled here at run time and
# exists only inside the demo vault's ignored .demo/ folder — never in the plugin
# source, where a key-shaped literal would also trip push protection.
write_secret_transcript() {
  local d="$DEMO/.demo" key="AKIA"
  key+="DEMOEXAMPLE"; key+="7Q2X9"
  python3 - "$d" "$key" "$(date '+%Y-%m-%d %H:%M %Z')" <<'PY'
import json, os, sys
d, key, stamp = sys.argv[1:]
t = os.path.join(d, "secret-transcript.jsonl")
lines = [
    {"type": "user", "message": {"role": "user", "content": "What's the deploy key for the PizzaBot bucket?"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": f"{stamp}\n\nSure, the key is {key} — paste it into the deploy step."}]}},
]
with open(t, "w") as fh:
    fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
with open(os.path.join(d, "secret-transcript.json"), "w") as fh:
    json.dump({"transcript_path": t, "stop_hook_active": False}, fh)
PY
}

# A local bare repo standing in for an upstream library, for the gt-watch act. It
# lives in the ignored .demo/ folder, is reached by file:// URL, and never touches
# the network. `upstream-release` later pushes a security release into it.
UPSTREAM="$DEMO/.demo/upstream/widget-lib.git"
GIT_DEMO=(-c user.name="widget-lib maintainer" -c user.email="maintainer@example.invalid" -c commit.gpgsign=false -c tag.gpgsign=false)

seed_upstream() {
  local work="$DEMO/.demo/upstream/work"
  mkdir -p "$(dirname "$UPSTREAM")"
  git init -q --bare "$UPSTREAM"
  git -C "$UPSTREAM" symbolic-ref HEAD refs/heads/main
  git init -q "$work"
  git -C "$work" symbolic-ref HEAD refs/heads/main
  git -C "$work" remote add origin "$UPSTREAM"
  printf '# widget-lib\n\nA small widget library PizzaBot depends on.\n' > "$work/README.md"
  git -C "$work" add -A
  git "${GIT_DEMO[@]}" -C "$work" commit -q -m "Initial widget library"
  printf 'def widget(): return "ok"\n' > "$work/widget.py"
  git -C "$work" add -A
  git "${GIT_DEMO[@]}" -C "$work" commit -q -m "Add the widget renderer"
  git "${GIT_DEMO[@]}" -C "$work" tag -a v1.0.0 -m "widget-lib 1.0.0"
  git -C "$work" push -q origin main --tags
}

upstream_release() {
  local work="$DEMO/.demo/upstream/work"
  printf 'def widget(): return "ok"  # input is escaped before rendering\n' > "$work/widget.py"
  git -C "$work" add -A
  git "${GIT_DEMO[@]}" -C "$work" commit -q -m "Escape widget input before rendering"
  git "${GIT_DEMO[@]}" -C "$work" tag -a v1.0.1 \
    -m "widget-lib 1.0.1 — security fix for CVE-2026-12345 (input injection in the renderer)"
  git -C "$work" push -q origin main --tags
}

build() {
  mkdir -p "$(dirname "$DEMO")"
  python3 "$PLUGIN_SCRIPTS_DIR/vault_init.py" fresh --vault "$DEMO" \
    --domain "PizzaBot 3000 demo" --no-config >/dev/null
  python3 "$PLUGIN_SCRIPTS_DIR/vault_init.py" create-project --vault "$DEMO" \
    --name "$SLUG" --title "PizzaBot 3000" --domain demo --tags demo,fictional >/dev/null
  cp -R "$TEMPLATE/project/." "$DEMO/Projects/$SLUG/"
  mkdir -p "$DEMO/Sources"
  cp "$TEMPLATE/sources/"*.md "$DEMO/Sources/"
  # Dates in the seeded tasks are relative to today, so "due today" is always true.
  python3 - "$DEMO/Projects/$SLUG" "$(date +%Y-%m-%d)" <<'PY'
import pathlib, sys
root, today = pathlib.Path(sys.argv[1]), sys.argv[2]
for p in root.rglob("*.md"):
    t = p.read_text(encoding="utf-8")
    if "{{TODAY}}" in t:
        p.write_text(t.replace("{{TODAY}}", today), encoding="utf-8")
PY
  printf -- '- [ ] PizzaBot should text the customer when the oven timer hits 90 seconds\n' >> "$DEMO/INBOX.md"
  mkdir -p "$DEMO/.demo"
  echo "Golden Thread demo vault — safe to delete; rebuilt by gt_demo.sh clean" > "$MARKER"
  write_secret_transcript
  seed_upstream
  printf '.demo/\n' >> "$DEMO/.gitignore"
  [[ -d "$DEMO/.git" ]] || git -C "$DEMO" init -q
  git -C "$DEMO" add -A
  git -C "$DEMO" -c user.name="gt-demo" -c user.email="gt-demo@example.invalid" \
    commit -q -m "Demo vault: PizzaBot 3000 seeded"
  git -C "$DEMO" rev-parse HEAD > "$DEMO/.demo/SEED"
}

case "${1:-}" in

start)
  if is_demo_vault; then
    echo "The demo vault already exists at $DEMO — run /gt:gt-demo clean for a fresh one."
    exit 1
  fi
  wipe
  build
  cat <<EOF
DEMO READY
vault: $DEMO

Open the demo session in a NEW terminal — it works on the demo vault only:

  cd "$DEMO" && GT_VAULT="$DEMO" GT_WATCH=report GT_WATCH_STATE="$DEMO/.demo/watch" claude

Then, in that session:  /gt:gt-demo tour
EOF
  ;;

end)
  is_demo_vault || { echo "No demo vault at $DEMO — run /gt:gt-demo start."; exit 1; }
  SEED=$(cat "$DEMO/.demo/SEED")
  echo "DEMO END — $DEMO"
  echo ""
  if [[ "$(git -C "$DEMO" rev-parse HEAD)" == "$SEED" ]]; then
    echo "No commits since the demo started."
  else
    echo "Commits during the demo:"
    git -C "$DEMO" log --oneline "$SEED..HEAD"
    echo ""
    echo "Files created or changed:"
    git -C "$DEMO" diff --name-status "$SEED..HEAD"
  fi
  UNCOMMITTED=$(git -C "$DEMO" status --porcelain)
  if [[ -n "$UNCOMMITTED" ]]; then
    echo ""
    echo "Not yet committed:"
    echo "$UNCOMMITTED"
  fi
  ;;

clean)
  wipe
  build
  echo "Demo vault rebuilt from the template at $DEMO — ready for the next run."
  echo "Reopen the demo session:  cd \"$DEMO\" && GT_VAULT=\"$DEMO\" GT_WATCH=report GT_WATCH_STATE=\"$DEMO/.demo/watch\" claude"
  ;;

remove)
  wipe
  echo "Removed the demo vault."
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
  echo "Restart Claude Code to clear the skill from this session."
  ;;

upstream-release)
  is_demo_vault || { echo "No demo vault at $DEMO — run /gt:gt-demo start."; exit 1; }
  if git -C "$UPSTREAM" rev-parse -q --verify refs/tags/v1.0.1 >/dev/null 2>&1; then
    echo "widget-lib v1.0.1 is already released — run /gt:gt-demo clean for a fresh demo."
    exit 1
  fi
  upstream_release
  echo "UPSTREAM RELEASE: widget-lib v1.0.1 pushed to file://$UPSTREAM"
  echo "  tag message: security fix for CVE-2026-12345"
  ;;

status)
  if is_demo_vault; then echo "demo vault: $DEMO (seeded $(git -C "$DEMO" log -1 --format=%cr "$(cat "$DEMO/.demo/SEED")"))"
  else echo "no demo vault (expected at $DEMO)"; fi
  ;;

*)
  echo "Usage: gt_demo.sh <start|end|clean|remove|status|upstream-release>"
  exit 1
  ;;

esac
