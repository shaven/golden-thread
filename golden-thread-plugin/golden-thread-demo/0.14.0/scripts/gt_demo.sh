#!/usr/bin/env bash
# gt_demo.sh — backing script for the /gt-demo:gt-demo skill (the `demo` module, 0.14.0)
# Commands: start | end | clean | remove | status | upstream-release | core-scripts | tour-acts
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

# Since 0.14.0 the demo is its own plugin (gt-demo), so gt's CORE scripts (vault_init.py,
# gt_watch.py, ...) are no longer next to this file. They are resolved, first hit wins:
#   1. $GT_CORE_SCRIPTS, if it holds vault_init.py (explicit override);
#   2. the newest gt beside this plugin in the same cache:
#      <cache>/golden-thread-plugin/gt-demo/<v>  ->  <cache>/golden-thread-plugin/gt/<newest>/scripts
#   3. a source checkout: <repo>/golden-thread-demo/<v>  ->  <repo>/golden-thread/<newest>/scripts
#   4. ~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts (the installed gt)
# Siblings win over the home cache so a checkout never runs against a different installed gt.
# The stable hooks dir (~/.claude/golden-thread/hooks) is not used: it carries gt_watch.py
# but not vault_init.py, and one rule for every core script beats two.
# `gt_demo.sh core-scripts` prints the result; the skill uses it for the tour's <core>.
newest_scripts() {  # $1 = a directory of version dirs -> <newest with vault_init.py>/scripts
  [[ -d "$1" ]] || return 1
  python3 -c '
import os, re, sys
root = sys.argv[1]
vs = [d for d in os.listdir(root) if re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", d)
      and os.path.isfile(os.path.join(root, d, "scripts", "vault_init.py"))]
if not vs:
    sys.exit(1)
print(os.path.join(root, max(vs, key=lambda s: tuple(int(x) for x in s.split("."))), "scripts"))
' "$1"
}

core_scripts() {
  if [[ -n "${GT_CORE_SCRIPTS:-}" && -f "${GT_CORE_SCRIPTS}/vault_init.py" ]]; then
    echo "$GT_CORE_SCRIPTS"; return 0
  fi
  local parent; parent="$(dirname "$(dirname "$PLUGIN_ROOT")")"
  newest_scripts "$parent/gt" 2>/dev/null && return 0
  newest_scripts "$parent/golden-thread" 2>/dev/null && return 0
  newest_scripts "$HOME/.claude/plugins/cache/golden-thread-plugin/gt" 2>/dev/null && return 0
  return 1
}

need_core() {
  CORE_SCRIPTS="$(core_scripts)" || {
    echo "Cannot find Golden Thread's core scripts (vault_init.py): is gt installed? Run install.sh, or set GT_CORE_SCRIPTS." >&2
    exit 3
  }
}
# `tour-acts` prints the tour the skill runs: the core acts from templates/demo-pizzabot/
# tour.md, then one act per INSTALLED module that ships one, ordered by module name, all
# numbered in sequence. Installed means both:
#   - ~/.claude/settings.json enabledPlugins["<plugin>@golden-thread-plugin"] is true, and
#   - the plugin's newest cache dir ~/.claude/plugins/cache/golden-thread-plugin/<plugin>/<v>/
#     has a module.json whose `demo` is a relative path (no `..`, not absolute) naming a
#     file inside that dir with a `## Title` heading and narration:/do:/point: lines.
# A module act's heading names its module. An enabled module whose act is missing or
# invalid is skipped with a one-line `note:` on stderr; a module act is never fatal.
# Acts are first-party module files only: third-party extensions do not supply act text
# (it would be prompt text the model follows).
tour_acts() {
  python3 - "$TEMPLATE/tour.md" "$HOME" <<'PY'
import json, os, re, sys

tour_path, home = sys.argv[1], sys.argv[2]
MARKET = "golden-thread-plugin"
ACT_RE = re.compile(r"^## Act \d+ — (.+)$", re.M)


def note(msg):
    print("note: " + msg, file=sys.stderr)


text = open(tour_path, encoding="utf-8").read()
heads = list(ACT_RE.finditer(text))
preamble = text[:heads[0].start()] if heads else text
acts = []  # (title, body)
for i, m in enumerate(heads):
    end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
    acts.append((m.group(1).strip(), text[m.end():end].strip("\n")))

try:
    with open(os.path.join(home, ".claude", "settings.json"), encoding="utf-8") as fh:
        enabled = json.load(fh).get("enabledPlugins")
except (OSError, ValueError, AttributeError):
    enabled = None
if not isinstance(enabled, dict):
    enabled = {}

cache = os.path.join(home, ".claude", "plugins", "cache", MARKET)
modules = []  # (module name, title, body)
for plugin in (sorted(os.listdir(cache)) if os.path.isdir(cache) else []):
    if enabled.get("%s@%s" % (plugin, MARKET)) is not True:
        continue
    pdir = os.path.join(cache, plugin)
    vers = [v for v in os.listdir(pdir) if re.fullmatch(r"\d+\.\d+\.\d+", v)
            and os.path.isdir(os.path.join(pdir, v))] if os.path.isdir(pdir) else []
    if not vers:
        continue
    ver = max(vers, key=lambda s: tuple(int(x) for x in s.split(".")))
    vdir = os.path.join(pdir, ver)
    mpath = os.path.join(vdir, "module.json")
    if not os.path.isfile(mpath):
        continue  # not a module (gt core)
    try:
        with open(mpath, encoding="utf-8") as fh:
            mod = json.load(fh)
        if not isinstance(mod, dict):
            raise ValueError
    except (OSError, ValueError):
        note("%s %s: module.json is unreadable, its tour act is skipped" % (plugin, ver))
        continue
    if "demo" not in mod:
        continue
    name = mod["name"] if isinstance(mod.get("name"), str) else plugin
    rel = mod["demo"]
    if not (isinstance(rel, str) and rel and not rel.startswith("/") and "\\" not in rel
            and ".." not in rel.split("/")):
        note("module %s: demo %r is not a relative path inside the module, act skipped" % (name, rel))
        continue
    apath = os.path.join(vdir, rel)
    if not (os.path.realpath(apath).startswith(os.path.realpath(vdir) + os.sep) and os.path.isfile(apath)):
        note("module %s: demo %r does not exist in the module, act skipped" % (name, rel))
        continue
    try:
        with open(apath, encoding="utf-8") as fh:
            act = fh.read()
    except (OSError, UnicodeDecodeError):
        note("module %s: demo %r is unreadable, act skipped" % (name, rel))
        continue
    m = re.search(r"^## (?:Act \d+ — )?(.+)$", act, re.M)
    rest = act[m.end():] if m else ""
    if not m or any(not re.search(r"^%s " % k, rest, re.M) for k in ("narration:", "do:", "point:")):
        note("module %s: demo %r lacks a '## Title' heading with narration:, do: and point: lines, act skipped"
             % (name, rel))
        continue
    modules.append((name, m.group(1).strip(), rest.strip("\n")))

# Module acts run BEFORE the last core act: that act closes the tour and prints the receipt
# (`gt_demo.sh end`), so a module's work has to happen before it to appear in the receipt.
out = [preamble.rstrip("\n") + "\n"]
ordered = [(t, b, None) for t, b in acts[:-1]] + [(t, b, nm) for nm, t, b in sorted(modules)] \
          + [(t, b, None) for t, b in acts[-1:]]
for n, (title, body, name) in enumerate(ordered, 1):
    suffix = " (module: %s)" % name if name else ""
    out.append("## Act %d — %s%s\n\n%s\n" % (n, title, suffix, body))
sys.stdout.write("\n".join(out))
PY
}

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
  need_core
  mkdir -p "$(dirname "$DEMO")"
  python3 "$CORE_SCRIPTS/vault_init.py" fresh --vault "$DEMO" \
    --domain "PizzaBot 3000 demo" --no-config >/dev/null
  python3 "$CORE_SCRIPTS/vault_init.py" create-project --vault "$DEMO" \
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
    echo "The demo vault already exists at $DEMO — run /gt-demo:gt-demo clean for a fresh one."
    exit 1
  fi
  wipe
  build
  cat <<EOF
DEMO READY
vault: $DEMO

Open the demo session in a NEW terminal — it works on the demo vault only:

  cd "$DEMO" && GT_VAULT="$DEMO" GT_WATCH=report GT_WATCH_STATE="$DEMO/.demo/watch" claude

Then, in that session:  /gt-demo:gt-demo tour
EOF
  ;;

end)
  is_demo_vault || { echo "No demo vault at $DEMO — run /gt-demo:gt-demo start."; exit 1; }
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
  # Until 0.14.0 this also deleted its own files from the gt plugin cache and set
  # install_demo=no. The demo is now a module, and installing or removing a module is
  # install.sh's job alone (cache, marketplace, installed_plugins, settings.json and the
  # recorded choice), so this script deletes no plugin file.
  echo "The demo itself is a Golden Thread module (plugin gt-demo). To uninstall it, run from the plugin repo:"
  echo "  bash install.sh --without demo"
  echo "then restart Claude Code. That records the choice, so a later install does not bring it back."
  ;;

core-scripts)
  need_core
  echo "$CORE_SCRIPTS"
  ;;

tour-acts)
  tour_acts
  ;;

upstream-release)
  is_demo_vault || { echo "No demo vault at $DEMO — run /gt-demo:gt-demo start."; exit 1; }
  if git -C "$UPSTREAM" rev-parse -q --verify refs/tags/v1.0.1 >/dev/null 2>&1; then
    echo "widget-lib v1.0.1 is already released — run /gt-demo:gt-demo clean for a fresh demo."
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
  echo "Usage: gt_demo.sh <start|end|clean|remove|status|upstream-release|core-scripts|tour-acts>"
  exit 1
  ;;

esac
