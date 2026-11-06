#!/usr/bin/env bash
# gt_demo.sh — backing script for the /gt-demo:gt-demo skill (the `demo` module, 0.14.0)
# Commands: start | end | clean | remove | status | upstream-release | core-scripts |
#           module-scripts NAME | tour-acts | show TOPIC (0.21.4)
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
# gt_lint.py, ...) are no longer next to this file. They are resolved, first hit wins:
#   1. $GT_CORE_SCRIPTS, if it holds vault_init.py (explicit override);
#   2. the newest gt beside this plugin in the same cache:
#      <cache>/golden-thread-plugin/gt-demo/<v>  ->  <cache>/golden-thread-plugin/gt/<newest>/scripts
#   3. a source checkout: <repo>/golden-thread-demo/<v>  ->  <repo>/golden-thread/<newest>/scripts
#   4. ~/.claude/plugins/cache/golden-thread-plugin/gt/<newest>/scripts (the installed gt)
# Siblings win over the home cache so a checkout never runs against a different installed gt.
# The stable hooks dir (~/.claude/golden-thread/hooks) is not used: it carries some core
# scripts but not vault_init.py, and one rule for every core script beats two.
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

# A MODULE's scripts (since 0.15.0 gt_watch.py is the watch module's, plugin gt-watch) are
# resolved the same way, first hit wins; a candidate is a version dir whose module.json
# names the module and which has a scripts/ dir:
#   1. the newest gt-<NAME> beside this plugin in the same cache;
#   2. the newest golden-thread-<NAME> beside it in a source checkout;
#   3. ~/.claude/plugins/cache/golden-thread-plugin/gt-<NAME>/<newest>/scripts.
# `gt_demo.sh module-scripts NAME` prints the result; the skill uses it for the tour's
# <module:NAME>. A module that is not found exits 3 with the reason, so a caller skips cleanly.
module_scripts() {  # $1 = module name
  local parent; parent="$(dirname "$(dirname "$PLUGIN_ROOT")")"
  python3 - "$1" "$parent/gt-$1" "$parent/golden-thread-$1" \
    "$HOME/.claude/plugins/cache/golden-thread-plugin/gt-$1" <<'PY'
import json, os, re, sys
name = sys.argv[1]
for root in sys.argv[2:]:
    try:
        vs = [d for d in os.listdir(root) if re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", d)
              and os.path.isdir(os.path.join(root, d, "scripts"))]
    except OSError:
        continue
    for v in sorted(vs, key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True):
        try:
            with open(os.path.join(root, v, "module.json"), encoding="utf-8") as fh:
                if json.load(fh).get("name") != name:
                    continue
        except (OSError, ValueError, AttributeError):
            continue
        print(os.path.join(root, v, "scripts"))
        sys.exit(0)
sys.exit(1)
PY
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
# Windows (0.20.1): Git Bash's $HOME is "/c/Users/x". Printed into the launch command, that
# reached Claude Code -- a native program -- as GT_VAULT=/c/Users/x/..., which no Windows
# process can open. The "C:/Users/x/..." form works in bash, git and Windows alike.
case "${OSTYPE:-}" in
  msys*|cygwin*) command -v cygpath >/dev/null 2>&1 && DEMO=$(cygpath -m "$DEMO") ;;
esac
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

# A local bare repo standing in for an upstream library, for the watch module's act. It
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
  # The seeded decisions.md (ADR-001, ADR-002) replaced the one create-project generated, so
  # the ADR spool's baseline no longer matches it: `gt_adr.py allocate` would hand out ADR-1
  # and `merge` would drop the seeded ADRs. Re-baseline from the seeded file, so act 4 can
  # allocate ADR-3 and merge it like any real project (0.17.11: decisions go through gt_adr).
  rm -rf "$DEMO/Projects/golden-thread/spool/decisions/$SLUG"
  python3 "$DEMO/Projects/golden-thread/tools/gt_adr.py" --vault "$DEMO" migrate "$SLUG" >/dev/null
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

# ---------------------------------------------------------------------------------------------
# `show TOPIC` (0.21.4): the feature acts added after 0.21.0. Each topic runs REAL gt commands
# against the demo vault and prints each command before its output, so the audience sees what
# ran. Nothing here reaches the network, a credential, a running model or the presenter's own
# settings:
#   - every command runs with HOME set to <demo vault>/.demo/home, a sandbox home whose
#     ~/.claude/vault-config.json holds the demo's settings (gt keeps its settings in that file,
#     so `gt_settings.py set` in a demo act would otherwise change the presenter's real install);
#   - the only "engine" is a closed loopback port, and the only start command is a script in the
#     demo vault that prints one line and starts nothing;
#   - LOTR runs against a fixture ~/.claude.json in that sandbox home, never a real server.
# A step that cannot honestly run offline prints a DESCRIPTION banner and says so on screen.
# ---------------------------------------------------------------------------------------------
DEMO_HOME="$DEMO/.demo/home"
# Python on native Windows finds ~ from USERPROFILE and ignores HOME, so every command pointed at
# the sandbox home sets both; without it the demo's Python read and wrote the presenter's ~/.claude
# there (found 2026-10-16). cygpath gives the Windows form; elsewhere it is the same path.
DEMO_HOME_W="$(cygpath -w "$DEMO_HOME" 2>/dev/null || printf '%s' "$DEMO_HOME")"

# stdin -> stdout with every spelling of the demo vault's path shown as $GT_VAULT: as given, its
# real path, and on Windows the C:\... and C:/... forms; a path under it keeps forward slashes, so
# Windows output reads like the commands shown (tools there join with a backslash).
vmask() {
  python3 -c '
import re, sys
text = sys.stdin.read()
for v in sorted({a for a in sys.argv[1:] if a}, key=len, reverse=True):
    text = text.replace(v, "$GT_VAULT").replace(v.replace("/", "\\"), "$GT_VAULT")
text = re.sub(r"\$GT_VAULT[^\s\"\x27<>|]*", lambda m: m.group(0).replace("\\", "/"), text)
sys.stdout.write(text)' "$DEMO" "$(cd "$DEMO" 2>/dev/null && pwd -P)" \
    "$(cygpath -w "$DEMO" 2>/dev/null)" "$(cygpath -m "$DEMO" 2>/dev/null)"
}
SHOW_TOPICS="tokens-startup tokens-output tokens-compact local-answers farm-first paths effort secrets owner-command guards roles workflow lotr farm usage report-card pane"

banner() {  # $1 = LIVE | DESCRIPTION, $2 = what
  if [[ "$1" == LIVE ]]; then
    printf '\n== LIVE: %s\n' "$2"
  else
    printf '\n== DESCRIPTION, not a live run: %s\n' "$2"
  fi
}

shown_cmd() {  # print a command the way the audience should read it: <core>, <module:x>, $GT_VAULT
  local s="" a
  for a in "$@"; do
    # A one-string pipeline is shown as written; an argument with a space is quoted.
    [[ $# -gt 1 && "$a" == *[[:space:]]* && "$a" != *"'"* ]] && a="'$a'"
    s="${s:+$s }$a"
  done
  s="${s//$CORE_SCRIPTS/<core>}"
  [[ -n "${MOD_DIR:-}" ]] && s="${s//$MOD_DIR/<module:$MOD_NAME>}"
  s="${s//$DEMO/\$GT_VAULT}"
  printf '$ %s\n' "$s"
}

# x CMD... : show the command, run it in the sandbox home, show its exit code. Never fails the
# topic: an exit code is part of what the audience is shown (lint exits 1 with findings).
x() {
  shown_cmd "$@"
  local rc=0 out
  # The demo vault's path is printed as $GT_VAULT, the way the commands above show it.
  out="$(HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_VAULT="$DEMO" "$@" 2>&1)" || rc=$?
  # A tool that resolves symlinks prints the vault's real path (/private/var/... on macOS), and a
  # fix line names gt's scripts in full: both are shown the way the commands above show them.
  local real
  real="$(cd "$DEMO" 2>/dev/null && pwd -P)" || real="$DEMO"
  [[ "$real" != "$DEMO" ]] && out="${out//$real/\$GT_VAULT}"
  out="${out//$CORE_SCRIPTS/<core>}"
  [[ -n "$out" ]] && printf '%s\n' "${out//$DEMO/\$GT_VAULT}" | vmask
  printf '(exit %s)\n' "$rc"
}

demo_settings() {  # KEY=VALUE ... -> merged into the sandbox home's vault-config.json
  python3 - "$DEMO_HOME/.claude/vault-config.json" "$DEMO" "$@" <<'PY'
import json, os, sys
path, vault, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
try:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
except (OSError, ValueError):
    cfg = {}
cfg["vault_path"] = vault
for p in pairs:
    k, _, v = p.partition("=")
    if v == "":
        cfg.pop(k, None)
    else:
        cfg[k] = v
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
}

ensure_demo_home() {
  mkdir -p "$DEMO_HOME/.claude/golden-thread" "$DEMO/.demo/bin"
  [[ -f "$DEMO_HOME/.claude/vault-config.json" ]] || demo_settings
}

need_demo() {
  is_demo_vault || { echo "No demo vault at $DEMO — run /gt-demo:gt-demo start."; exit 1; }
  need_core
  ensure_demo_home
}

need_module() {  # $1 = module name -> MOD_DIR, MOD_NAME, or exit 3 with the reason
  MOD_NAME="$1"
  MOD_DIR="$(module_scripts "$1")" || {
    echo "Module $1 is not installed — this act is skipped."
    exit 3
  }
}

# -- Notes answered locally (gt 0.21.4: vault_answers, vault_answers_start) --------------------
# The "engine" is a row in a service table that points at 127.0.0.1:9 (nothing listens there),
# so `ask` takes the real fallback path. Its start command is a demo script that only prints.
services_table() {  # $1 = start command ("" for none)
  python3 - "$DEMO_HOME/farm-services.md" "$1" <<'PY'
import sys
path, start = sys.argv[1], sys.argv[2]
rows = ["| name | transport | base_url | model | pool | local | key_file | start | notes |",
        "|---|---|---|---|---|---|---|---|---|",
        "| pizza-engine | chat-completions | http://127.0.0.1:9/v1 | demo-model | | yes | | %s | demo "
        "engine: nothing listens on port 9 |" % start]
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(rows) + "\n")
PY
}

show_local_answers() {
  local starter="$DEMO/.demo/bin/start-pizza-engine.sh"
  cat > "$starter" <<'EOF'
#!/bin/sh
# gt demo: stands in for an engine's start command. It starts nothing.
echo "demo start script: a real engine would start here; this one starts nothing"
EOF
  chmod +x "$starter"
  demo_settings vault_answers=local-first "farm_services=$DEMO_HOME/farm-services.md" vault_answers_start=ask
  local q="what did we decide about the negotiation rounds"

  services_table ""
  banner LIVE "notes answered on your own machine first; no engine is running"
  x python3 "$CORE_SCRIPTS/gt_vault_answer.py" mode
  x python3 "$CORE_SCRIPTS/gt_vault_answer.py" ask "$q" --vault "$DEMO"
  echo "-> exit 3 is the silent fallback: gt-query reads the notes itself, and you see one answer either way."

  if [[ "$starter" == *[[:space:]]* ]]; then
    banner DESCRIPTION "the demo vault path has a space, so gt will not offer to start an engine from it"
  else
    services_table "$starter"
    banner LIVE "the same engine, now set up with a start command"
    x python3 "$CORE_SCRIPTS/gt_vault_answer.py" ask "$q" --vault "$DEMO"
  fi
  banner DESCRIPTION "the question gt-query asks on engine_stopped (asked in a live session; never answered here)"
  grep -o 'Your local engine <engine> is set up but not running[^"]*' "$CORE_SCRIPTS/../skills/gt-query/SKILL.md" | head -1
  grep -E '^  - \*\*(Start it now|Not now|Don.t ask again)\*\*' "$CORE_SCRIPTS/../skills/gt-query/SKILL.md" \
    | sed -E 's/^  - \*\*([^*]+)\*\*.*/    [ \1 ]/'
  echo "-> Start it now runs the START line above, which in this demo only prints; nothing is started."
  banner LIVE "Don't ask again sets one setting (in the demo's own home)"
  x python3 "$CORE_SCRIPTS/gt_settings.py" set vault_answers_start never
  x python3 "$CORE_SCRIPTS/gt_vault_answer.py" ask "$q" --vault "$DEMO"
  demo_settings vault_answers_start=ask
}

# -- Farm first (gt 0.21.4: farm_first) ---------------------------------------------------------
show_farm_first() {
  services_table ""
  demo_settings "farm_services=$DEMO_HOME/farm-services.md" farm_first=suggest
  banner LIVE "what routine work a local engine could take (farm_first: suggest)"
  x python3 "$CORE_SCRIPTS/gt_settings.py" get farm_first
  x python3 "$CORE_SCRIPTS/gt_persona.py" farm-tasks --vault "$DEMO" --personas developer --engines
  if MOD_NAME=farm MOD_DIR="$(module_scripts farm)" && [[ -f "$MOD_DIR/gt_farm_calibrate.py" ]]; then
    banner LIVE "is the engine up? (a status check; calibrating would call the model, so it is not run)"
    x python3 "$MOD_DIR/gt_farm_calibrate.py" --status
  else
    MOD_DIR=""
    banner DESCRIPTION "the farm module is not installed, so there is no engine status to show"
  fi
  MOD_DIR=""
  banner DESCRIPTION "what the three values mean"
  x python3 "$CORE_SCRIPTS/gt_settings.py" explain farm_first
}

# -- Path check (core rule 10, gt_path_check.py) and the path-anchoring lint -------------------
show_paths() {
  banner LIVE "check a path before saying it exists (core rule 10: label it)"
  x python3 "$CORE_SCRIPTS/gt_path_check.py" --vault "$DEMO" Projects/demo-pizzabot/research.md \
    Projects/demo-pizzabot/design.md Projects/demo-pizzabot/decision.md
  echo "-> rule 10 labels what was checked: unverified, self-verified or independently verified."
  sed -n '/^- `unverified`/,/^  the reasoning/p' "$DEMO/core-rules/core_verification_state.md" 2>/dev/null || true
  # One note whose links are written three wrong ways, linted in the demo vault itself and then
  # removed, so the vault (and act 7's lint) is exactly as it was. Lint runs with the session's
  # own HOME: the sandbox home lives inside the vault, and lint would rightly call every `~/`
  # path in the vault's docs a path into the vault.
  local note="$DEMO/Projects/demo-pizzabot/oven-notes.md" user="${USER:-presenter}"
  cat > "$note" <<EOF
# Oven notes

See [the design](../demo-pizzabot/design.md) and [the old plan](../demo-pizzabot/plan.md).
The config lives at \`/Users/$user/pizzabot/oven.toml\`.
EOF
  banner LIVE "lint flags paths that break when a note or the vault moves"
  shown_cmd python3 "$CORE_SCRIPTS/gt_lint.py" "$DEMO"
  # Only this note's path findings are shown; lint's other checks are act 7's.
  python3 "$CORE_SCRIPTS/gt_lint.py" "$DEMO" 2>&1 | awk '
    /^\[path-.*oven-notes/ { show = 1; n++ }
    /^\[/ && !/^\[path-.*oven-notes/ { show = 0 }
    /^$/ { show = 0 }
    show { print }
    END { printf "(%d path finding(s) in the note; lint'"'"'s other checks are act 7'"'"'s)\n", n }' || true
  rm -f "$note"
}

# -- Effort and model calibration (gt 0.21.4: gt_effort_report.py) -----------------------------
effort_fixture() {  # $1 = projects dir: fixture transcripts dated today, aggregates only
  python3 - "$1" "$(date -u +%Y-%m-%d)" "$CORE_SCRIPTS/gt_model_prices.json" <<'PY'
import json, os, sys
root, day = sys.argv[1], sys.argv[2]
# model ids come from gt's price table, never written here (model ids live in one place)
with open(sys.argv[3], encoding="utf-8") as fh:
    _ids = [p[0] for p in json.load(fh)["prices"]]
def model(family):
    return [i for i in _ids if family in i][-1]


def row(i, model, effort, out, think, hour):
    return {"type": "assistant", "timestamp": "%sT%02d:%02d:00Z" % (day, hour, i),
            "requestId": "req-%s-%d-%d" % (effort, hour, i), "effort": effort,
            "message": {"id": "msg-%s-%d-%d" % (effort, hour, i), "model": model,
                        "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 800, "output_tokens": out,
                                  "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 0,
                                  "output_tokens_details": {"thinking_tokens": think}}}}


def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")


def skill(name, hour):
    return {"type": "user", "timestamp": "%sT%02d:00:00Z" % (day, hour),
            "message": {"role": "user", "content": "<command-name>/gt:%s</command-name>" % name}}


proj = os.path.join(root, "-demo-pizzabot")
write(os.path.join(proj, "sess-list.jsonl"),
      [skill("gt-list", 9)] + [row(i, model("opus"), "xhigh", 2000, 1200, 9) for i in range(6)])
sub = os.path.join(proj, "sess-list", "subagents")
write(os.path.join(sub, "agent-merge.jsonl"),
      [row(i, model("sonnet"), "high", 3000, 1800, 10) for i in range(6)])
with open(os.path.join(sub, "agent-merge.meta.json"), "w", encoding="utf-8", newline="\n") as fh:
    json.dump({"agentType": "general-purpose", "description": "merge the feature branch",
               "effort": "high"}, fh)
write(os.path.join(proj, "sess-lint.jsonl"),
      [skill("gt-lint", 11)] + [row(i, model("haiku"), "low", 2500, 1500, 11) for i in range(6)])
PY
}

show_effort() {
  local pd="$DEMO/.demo/transcripts"
  rm -rf "$pd"
  effort_fixture "$pd"
  banner LIVE "where thinking goes, read from transcripts (the demo's own fixture transcripts; no API call)"
  x python3 "$CORE_SCRIPTS/gt_effort_report.py" effort --projects-dir "$pd" --days 2
  echo "-> the effort report only ever suggests LESS effort, and only where a lower level exists."
  banner DESCRIPTION "calibration in both directions re-runs real work one tier and one effort up and down, which spends allowance, so it is described here, not run"
  x python3 "$CORE_SCRIPTS/gt_settings.py" explain model_calibration
  banner LIVE "the calibration report with calibration off: it says so and spends nothing"
  x python3 "$CORE_SCRIPTS/gt_model_calibrate.py" report --dry-run --vault "$DEMO"
}

# -- gt-workflow module act ----------------------------------------------------------------------
show_workflow() {
  need_module workflow
  local wh="$DEMO/.demo/workflow" draft="$DEMO/.demo/workflow-draft.json"
  rm -rf "$wh"
  mkdir -p "$wh"
  # The draft Claude writes when the presenter picks "Build a workflow": two gt steps that run on
  # their own, then one that waits for Claude.
  cat > "$draft" <<'EOF'
{
  "title": "PizzaBot morning check",
  "description": "What is due, what is waiting, then one recommendation.",
  "steps": [
    {"id": "tasks", "kind": "run", "title": "The open tasks", "script": "gt_task.py",
     "args": ["list", "--vault", "{{vault}}"], "ok_exit": [0, 1]},
    {"id": "waiting", "kind": "run", "title": "The handoffs that are waiting",
     "script": "gt_handoff_status.py", "args": ["list", "--vault", "{{vault}}"], "ok_exit": [0, 1]},
    {"id": "pick", "kind": "claude", "title": "Pick the one thing to do first",
     "prompt": "From these tasks and waiting handoffs, name the one thing to do first and why. Tasks: {{steps.tasks.output.text}} Handoffs: {{steps.waiting.output.text}}"}
  ]
}
EOF
  export GT_WORKFLOW_HOME="$wh" GT_WORKFLOW_CORE_SCRIPTS="$CORE_SCRIPTS" GT_EXECUTION_METRICS=off
  local W="$MOD_DIR/gt_workflow.py"
  banner LIVE "the starters a workflow can begin from"
  x python3 "$W" list
  banner LIVE "Build a workflow: Claude drafts it, you see it before anything is saved"
  x python3 "$W" new pizzabot-morning --from-description "$draft"
  x python3 "$W" new pizzabot-morning --from-description "$draft" --save
  x python3 "$W" check pizzabot-morning
  banner LIVE "run it by name: the gt steps run, then it waits for Claude"
  x python3 "$W" run pizzabot-morning
  x python3 "$W" runs
  banner LIVE "the gates that are yours: schedule (and approve, and a --confirm of a write or a shell step) asks a person at a real terminal; this act has none"
  cat > "$DEMO/.demo/workflow-tasks.json" <<'EOF'
{
  "title": "PizzaBot open tasks",
  "description": "The open tasks, every morning: gt steps only, so it could run on its own.",
  "steps": [
    {"id": "tasks", "kind": "run", "title": "The open tasks", "script": "gt_task.py",
     "args": ["list", "--vault", "{{vault}}"], "ok_exit": [0, 1]}
  ]
}
EOF
  x python3 "$W" new pizzabot-tasks --from-description "$DEMO/.demo/workflow-tasks.json" --save
  x python3 "$W" schedule pizzabot-tasks --daily 08:00 < /dev/null
  echo "-> Claude's Bash tool, a pipe and a scheduler have no terminal, so Claude cannot schedule it or approve a write for you."
  unset GT_WORKFLOW_HOME GT_WORKFLOW_CORE_SCRIPTS GT_EXECUTION_METRICS
}

# -- gt-lotr module act: pull existing MCP servers, the capability index --------------------------
show_lotr() {
  need_module lotr
  local L="$MOD_DIR/lotr.py" D="$MOD_DIR/lotrd.py"
  # The fixture: the MCP servers a Claude Code install might already have, in the sandbox home's
  # ~/.claude.json. Nothing is connected to; --no-probe makes no network call.
  python3 - "$DEMO_HOME/.claude.json" <<'PY'
import json, sys
cfg = {"mcpServers": {
    "atlassian": {"type": "http", "url": "https://mcp.example.invalid/atlassian",
                  "headers": {"Authorization": "Bearer ${ATLASSIAN_TOKEN}"}},
    "filesystem": {"type": "stdio", "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
    "projects": {"/Users/example/pizzabot": {"mcpServers": {
        "github": {"type": "http", "url": "https://mcp.example.invalid/github"}}}}}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
  # A client's own token file, so `pull --import` can register by reference. Its value is not a
  # token; the dry run below never reads it, and nothing ever sends it anywhere.
  printf '{"access_token": "demo-placeholder-not-a-token"}\n' > "$DEMO_HOME/.claude/atlassian-mcp-tokens.json"
  chmod 600 "$DEMO_HOME/.claude/atlassian-mcp-tokens.json"
  # The gateway home is relative to .demo/ so its socket path stays short (unix sockets cap at
  # about 104 bytes, and the demo vault can live deep in a home directory).
  cd "$DEMO/.demo"
  rm -rf lotr
  banner LIVE "the MCP servers Claude Code already has (a fixture config; nothing is contacted)"
  x python3 "$L" --home lotr init --zone demo --mode local >/dev/null
  x python3 "$L" --home lotr pull --no-probe --table
  x python3 "$L" --home lotr pull --no-probe --import atlassian --dry-run
  echo "-> --import without --dry-run registers it by reference to its token file; it would contact the server, so the demo stops here."
  banner LIVE "the capability index: find an operation by what it does"
  x python3 "$L" --home lotr add-http jira@demo --profile jira-v3 --base-url https://jira.example.invalid \
    --identity "jira @ example.invalid" --auth none --description "PizzaBot tracker (fixture, never contacted)"
  # Native Windows: lotrd serves a named pipe (gt core's gt_ipc), not lotr/lotrd.sock, and finds
  # gt_ipc in the hooks dir of the home it runs in -- the sandbox home has none, so it is named.
  local win=""
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) win=1; export GT_HOOKS_DIR="$CORE_SCRIPTS" ;; esac
  lotrd_up() {
    if [[ -n "$win" ]]; then
      HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" python3 -c 'import sys; sys.path.insert(0, sys.argv[1])
from lotrlib import server
sys.exit(0 if server._gt_ipc().alive_at(server.pipe_address("lotr"), timeout=0.2) else 1)' "$(dirname "$L")" 2>/dev/null
    else
      [[ -S lotr/lotrd.sock ]]
    fi
  }
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" python3 "$D" --home lotr >/dev/null 2>&1 &
  local pid=$! i
  # Native Windows: $pid is Git Bash's process for the python3 shim, and `kill` ends only that one:
  # the real python.exe lotrd kept running after every demo, holding its sandbox folder (found
  # 2026-10-16, four of them left on a test box). There the whole Windows process tree is stopped.
  stop_lotrd() {
    if [[ -n "$win" && -r "/proc/$pid/winpid" ]]; then
      taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1 || true
    fi
    kill "$pid" 2>/dev/null || true
  }
  trap 'stop_lotrd' EXIT                          # never outlives this act, even on an error
  for i in $(seq 1 50); do lotrd_up && break; sleep 0.1; done
  if lotrd_up; then
    x python3 "$L" --home lotr find "open issues assigned to me" --limit 3
    x python3 "$L" --home lotr describe jira@demo get_issue
  else
    banner DESCRIPTION "the gateway daemon did not come up, so find could not run"
  fi
  stop_lotrd
  wait "$pid" 2>/dev/null || true
  trap - EXIT
  [[ -n "$win" ]] && unset GT_HOOKS_DIR
  cd - >/dev/null
}

# -- module acts with no act until 0.21.4: gt-farm, gt-usage, gt-report-card, gt-pane ---------------
# Each runs the module's own scripts against the demo's sandbox home, so the presenter's ledgers,
# button list and notices are never read or written; nothing is sent and no model is called.

# A module's setting, in its own words (module.json), the way `gt_settings.py explain` prints a
# gt one: gt_settings registers module settings from the installed caches of the HOME it runs in,
# and the demo's sandbox home has none, so the module's text is read where it is declared.
module_explain() {  # $1 = setting key (MOD_DIR set by need_module)
  shown_cmd "explain $1   # from <module:$MOD_NAME>/module.json"
  python3 - "$MOD_DIR/../module.json" "$1" <<'PY2'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
s = next((x for x in m.get("settings", []) if x.get("key") == sys.argv[2]), None)
if s is None:
    print("(no such setting in this module)")
    sys.exit()
print("%s  (default: %s; options: %s)" % (s["key"], s.get("default"), " | ".join(s.get("values") or [])))
print()
print(s.get("summary", ""))
if s.get("detail"):
    print()
    print(s["detail"])
PY2
}

# gt-farm: a packet holding a credential is refused before anything is sent; a clean one is checked
# against the engine with --dry-run (the engine is 127.0.0.1:9, where nothing listens).
show_farm() {
  need_module farm
  local F="$MOD_DIR/gt_farm_send.py" pk="$DEMO/.demo/farm"
  rm -rf "$pk"
  mkdir -p "$pk"
  python3 - "$pk" <<'PY'
import os, sys
d = sys.argv[1]
head = ("### TASK\nSummarise the oven log below in three bullet points.\n\n### INPUTS\n"
        "oven 2 ran 12 minutes long at 14:05; oven 3 sensor timeout at 14:20\n")
tail = "\n### RETURN CONTRACT\nThree bullet points, nothing else.\n"
with open(os.path.join(d, "oven-summary.md"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write(head + tail)
# A credential-shaped line, assembled here so no file in the plugin holds one. It is not a key.
fake = "AKIA" + "ABCDEFGHIJKLMNOP"
with open(os.path.join(d, "oven-summary-with-key.md"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write(head + "oven API key: %s\n" % fake + tail)
PY
  export GT_CORE_SCRIPTS="$CORE_SCRIPTS"
  banner LIVE "a packet holding a credential-shaped line is refused before anything is sent"
  x python3 "$F" send "$pk/oven-summary-with-key.md" --base-url http://127.0.0.1:9/v1 --vault "$DEMO" --dry-run --no-ledger
  echo "-> the refusal names the line and the rule, never the value, and exits 2."
  banner LIVE "the same packet without it: checked and ready (--dry-run, so nothing is sent)"
  x python3 "$F" send "$pk/oven-summary.md" --base-url http://127.0.0.1:9/v1 --vault "$DEMO" --dry-run --no-ledger
  banner LIVE "what farmed work saved, from the savings ledger (empty in this fresh home)"
  x python3 "$F" ledger --days 7
  banner DESCRIPTION "the ladder: local engine first, a bigger one next, Claude last, only when a check fails"
  module_explain farm_ladder
  unset GT_CORE_SCRIPTS
}

# gt-usage: one status-line render (what Claude Code hands the meter), then where the allowance stands.
show_usage() {
  need_module usage
  local U="$MOD_DIR/gt_usage.py" now blob
  rm -rf "$DEMO_HOME/.claude/golden-thread/usage"
  now="$(date +%s)"
  blob='{"rate_limits": {"five_hour": {"used_percentage": 82, "resets_at": '"$((now + 5400))"'}, "seven_day": {"used_percentage": 41, "resets_at": '"$((now + 259200))"'}}, "context_window": {"used_percentage": 31}, "session_id": "demo-usage"}'
  banner LIVE "the meter: one status-line render at 82% of the 5-hour window (a fixture reading, not your plan)"
  shown_cmd "echo '<status-line JSON: 5-hour 82%, weekly 41%>' | python3 $U"
  local rc=0
  printf '%s' "$blob" | HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" python3 "$U" || rc=$?
  printf '\n(exit %s)\n' "$rc"
  echo "-> 82% is past the 5-hour alert line (80%), so the meter speaks; below it, it prints nothing and records the reading anyway."
  banner LIVE "where the allowance stands, from the reading just recorded"
  x python3 "$U" --now
  x python3 "$U" --history 1
  banner DESCRIPTION "when the meter speaks: the alert lines"
  module_explain usage_alert
}

# gt-report-card: the card for the demo vault, as a person runs it (no session ended, so nothing is
# parked for a next session, and closeout_check off keeps the demo vault unwritten).
show_report_card() {
  need_module report-card
  local R="$MOD_DIR/gt_report_card.py"
  demo_settings report_card=full closeout_check=off
  banner LIVE "the session report card for the demo vault (report_card: full)"
  x python3 "$R"
  banner DESCRIPTION "it runs by itself at /compact and at session end, and the next session in the same folder shows it"
  module_explain report_card
  demo_settings report_card= closeout_check=
}

# gt-pane: the buttons beside the prompt. The pane itself is drawn by Claude Code; the list is a
# file, edited here in the demo's own copy (GT_PANE_FILE), never the presenter's.
show_pane() {
  need_module pane
  local P="$MOD_DIR/gt_pane.py"
  export GT_PANE_FILE="$DEMO/.demo/pane/buttons.json" GT_PANE_PACKS="$CORE_SCRIPTS/../packs"
  rm -rf "$DEMO/.demo/pane"
  banner LIVE "the button list: seeded from the defaults, then moved in plain words"
  x python3 "$P" seed
  x python3 "$P" list
  x python3 "$P" move 3 1
  banner DESCRIPTION "the pane is drawn beside the prompt by Claude Code; a demo prints its list instead"
  module_explain pane_at_start
  unset GT_PANE_FILE GT_PANE_PACKS
}

show_topic() {
  need_demo
  case "${1:-}" in
    local-answers) show_local_answers ;;
    farm-first) show_farm_first ;;
    paths) show_paths ;;
    effort) show_effort ;;
    secrets) show_secrets ;;
    owner-command) show_owner_command ;;
    guards) show_guards ;;
    roles) show_roles ;;
    workflow) show_workflow ;;
    lotr) show_lotr ;;
    farm) show_farm ;;
    usage) show_usage ;;
    report-card) show_report_card ;;
    pane) show_pane ;;
    tokens-startup) show_tokens_startup ;;
    tokens-output) show_tokens_output ;;
    tokens-compact) show_tokens_compact ;;
    *) echo "Usage: gt_demo.sh show <$(echo "$SHOW_TOPICS" | tr ' ' '|')>" >&2; exit 1 ;;
  esac
}

# -- Spend fewer tokens (gt 0.21.4: the context settings and the Spending Fewer Tokens guide) -------
# The hooks run from a copy laid out the way install.sh lays out ~/.claude/golden-thread/hooks
# (gt's hook scripts beside its scripts), inside the demo, so the presenter's own hooks and
# their per-session markers are never touched.
demo_hooks() {
  local hd="$DEMO/.demo/hooks"
  rm -rf "$hd"
  mkdir -p "$hd"
  cp "$CORE_SCRIPTS"/*.py "$hd/"
  cp "$CORE_SCRIPTS"/../hooks/*.sh "$hd/"
  # the secret scan's detectors sit beside hooks/ in an install; intake and spill keep nothing without them
  rm -rf "$DEMO/.demo/packs"
  cp -R "$CORE_SCRIPTS"/../packs "$DEMO/.demo/packs"
  cp "$CORE_SCRIPTS"/../MANIFEST.json "$DEMO/.demo/MANIFEST.json"
  echo "$hd"
}

show_tokens_startup() {
  local hd sid t="$DEMO/.demo/rules-transcript.jsonl" first
  sid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  hd="$(demo_hooks)"
  banner DESCRIPTION "startup_output: a healthy session start is one line (this session's own start printed it above the prompt; a hand run always prints the long form)"
  x python3 "$CORE_SCRIPTS/gt_settings.py" explain startup_output
  demo_settings rules_injection=compact
  rm -f "$t"
  : > "$t"
  banner LIVE "rules_injection: compact — prompt 1 of a session carries every Core rule"
  local payload='{"hook_event_name":"UserPromptSubmit","session_id":"'"$sid"'","transcript_path":"'"$t"'","prompt":"hello"}'
  shown_cmd "echo '<prompt 1>' | bash $hd/inject_core_rules.sh"
  first="$(printf '%s' "$payload" | HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_VAULT="$DEMO" bash "$hd/inject_core_rules.sh")"
  printf '%s' "$first" | python3 -c '
import json, sys
c = json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]
print("  %d characters, %d lines, opening: %s" % (len(c), c.count("\n") + 1,
      next((l for l in c.splitlines() if l.startswith("CORE RULES")), "?")))' || true
  # The transcript now holds that full text, exactly as Claude Code records hook context.
  printf '%s' "$first" | python3 -c '
import datetime, json, sys
c = json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
print(json.dumps({"type": "attachment", "timestamp": ts,
                  "attachment": {"type": "hook_additional_context", "content": [c]}}))' >> "$t"
  banner LIVE "prompt 2 of the same session: one line, and the rule-5 reminder"
  payload='{"hook_event_name":"UserPromptSubmit","session_id":"'"$sid"'","transcript_path":"'"$t"'","prompt":"and now?"}'
  shown_cmd "echo '<prompt 2>' | bash $hd/inject_core_rules.sh"
  printf '%s' "$payload" | HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_VAULT="$DEMO" bash "$hd/inject_core_rules.sh" | python3 -c '
import json, sys
c = json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]
line = next((l for l in c.splitlines() if "rules_injection: compact" in l), None)
print("  %d characters in all; the rules line:" % len(c))
print("  " + (line or "(no compact line: the full text was sent again)"))' || true
  x python3 "$hd/gt_rules_once.py" status --session "$sid"
  demo_settings rules_injection=
}

show_tokens_output() {
  local hd sd="$DEMO/.demo/spill" id="$DEMO/.demo/intake" big="$DEMO/.demo/big-paste.txt"
  hd="$(demo_hooks)"
  rm -rf "$sd" "$id"
  demo_settings spill_threshold_lines=200 paste_intake=ask
  banner LIVE "spill_threshold_lines: a 300-line command output, as the PostToolUse hook rewrites it"
  python3 - "$DEMO/.demo/spill-payload.json" <<'PY'
import json, sys
out = "\n".join("oven log line %d%s" % (i, "  ERROR: crust sensor timeout" if i == 150 else "")
                for i in range(1, 301))
doc = {"session_id": "demo-spill", "transcript_path": "", "cwd": "", "hook_event_name": "PostToolUse",
       "tool_name": "Bash", "tool_input": {"command": "cat oven.log"},
       "tool_response": {"stdout": out, "stderr": "", "interrupted": False, "isImage": False}}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(doc, fh)
PY
  shown_cmd "python3 $hd/gt_spill.py hook < \$GT_VAULT/.demo/spill-payload.json"
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_SPILL_DIR="$sd" python3 "$hd/gt_spill.py" hook < "$DEMO/.demo/spill-payload.json" \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
o = d["hookSpecificOutput"]["updatedToolOutput"]
text = o.get("stdout", "") if isinstance(o, dict) else str(o)
lines = text.replace(sys.argv[1], "$GT_VAULT").splitlines()
print("\n".join(lines[:5] + ["  ... %d lines in all reach Claude instead of 300 ..." % len(lines)]))' "$DEMO" | vmask || true
  echo "The whole output is in the file the marker names:"
  shown_cmd ls "$sd"
  ls "$sd" && wc -l "$sd"/*.txt | vmask || true

  banner LIVE "paste_intake: a 100 KB paste is held back and saved; a digest is offered"
  python3 - "$DEMO/.demo/paste-payload.json" "$big" <<'PY'
import json, sys
text = "".join("2026-10-15 12:%02d:%02d order %05d margherita large OK\n" % (i // 60 % 60, i % 60, i)
               for i in range(2400))
with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(text)
doc = {"prompt": "Why did Friday's orders fail?\n" + text, "session_id": "demo-paste",
       "transcript_path": "", "cwd": "", "hook_event_name": "UserPromptSubmit"}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(doc, fh)
PY
  shown_cmd "python3 $hd/gt_intake.py hook < \$GT_VAULT/.demo/paste-payload.json"
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_INTAKE_DIR="$id" python3 "$hd/gt_intake.py" hook < "$DEMO/.demo/paste-payload.json" \
    | python3 -c '
import json, sys, textwrap
raw = sys.stdin.read().strip()
if not raw:
    print("  (the paste went through: it is under the limit for a machine with no local engine)")
    sys.exit()
d = json.loads(raw)
print("  decision: %s" % d.get("decision"))
for l in textwrap.wrap(d.get("reason", "").replace(sys.argv[1], "$GT_VAULT"), 90, break_long_words=False, break_on_hyphens=False):
    print("  " + l)' "$DEMO" | vmask || true
  banner LIVE "/gt:gt-digest picks the cheapest reader for a file before reading it"
  x python3 "$hd/gt_intake.py" plan "$big"
  x python3 "$hd/gt_break_even.py" advise 3000
  banner DESCRIPTION "the digest itself is read by a subagent or a local engine, which this demo does not run"
  demo_settings spill_threshold_lines= paste_intake=
}

show_tokens_compact() {
  # A fresh session id each run: the ledger and the nudge's once-per-band marker key on its first
  # 8 characters, so a fixed id would be silent after the first run (as a real session would be).
  local hd sid ledger="$DEMO_HOME/.claude/golden-thread/usage/readings.jsonl"
  sid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  hd="$(demo_hooks)"
  demo_settings context_nudge=40 auto_compact=50
  mkdir -p "$(dirname "$ledger")"
  printf '{"session": "%s", "ctx_pct": 47, "ts": "%s"}\n' "${sid:0:8}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ledger"
  banner LIVE "context_nudge: a session reading 47% of its window is told so, once per band"
  local payload='{"hook_event_name":"UserPromptSubmit","session_id":"'"$sid"'","prompt":"next"}'
  shown_cmd "echo '<a prompt at 47%>' | python3 $hd/gt_state.py check"
  printf '%s' "$payload" | HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_VAULT="$DEMO" python3 "$hd/gt_state.py" check | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    print("  " + json.loads(raw).get("systemMessage", raw))
except ValueError:
    print(raw or "  (no nudge)")' || true
  printf '{"session": "%s", "ctx_pct": 49, "ts": "%s"}\n' "${sid:0:8}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ledger"
  shown_cmd "echo '<the next prompt, at 49%>' | python3 $hd/gt_state.py check"
  printf '%s' "$payload" | HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_VAULT="$DEMO" python3 "$hd/gt_state.py" check \
    | grep -q . && echo "  (nudged again)" || echo "  (silent: same band, already told)"
  banner DESCRIPTION "auto_compact: past the line it saves findings, writes a handoff, then compacts (cut_handoff picks none, handoff or facts; cut_kind compact or clear); the cut is never run on this session"
  x python3 "$CORE_SCRIPTS/gt_settings.py" explain auto_compact
  banner LIVE "the handoff it writes before compacting (--dry-run: printed, not written; the first lines)"
  shown_cmd python3 "$CORE_SCRIPTS/gt_handoff.py" --vault "$DEMO" --project demo-pizzabot --dry-run
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" python3 "$CORE_SCRIPTS/gt_handoff.py" --vault "$DEMO" --project demo-pizzabot --dry-run 2>&1 \
    | sed -n '9,20p' || true
  x python3 "$hd/gt_compact.py" status
  banner LIVE "the guide: /gt:gt-help tokens"
  x python3 "$CORE_SCRIPTS/gt_help.py" tokens --print --no-open
  demo_settings context_nudge= auto_compact=
}

# -- Credentials never land in a file (gt 0.21.4: spill, intake and the broker scan before they keep) --
# The fake key is assembled at run time, so this script never holds a credential-shaped literal.
show_secrets() {
  local hd sd="$DEMO/.demo/spill" id="$DEMO/.demo/intake" key
  hd="$(demo_hooks)"
  rm -rf "$sd" "$id"
  key="AKIA""PIZZADEMO0000000"
  demo_settings spill_threshold_lines=200 paste_intake=ask
  banner LIVE "a 300-line output with a key in it: the spill file and the preview keep a marker, not the key"
  python3 - "$DEMO/.demo/spill-payload.json" "$key" <<'PY'
import json, sys
out = "\n".join(("export OVEN_API_KEY=%s" % sys.argv[2]) if i == 7 else "oven env line %d" % i
                for i in range(1, 301))
doc = {"session_id": "demo-secrets", "transcript_path": "", "cwd": "", "hook_event_name": "PostToolUse",
       "tool_name": "Bash", "tool_input": {"command": "env"},
       "tool_response": {"stdout": out, "stderr": "", "interrupted": False, "isImage": False}}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(doc, fh)
PY
  shown_cmd "python3 $hd/gt_spill.py hook < \$GT_VAULT/.demo/spill-payload.json"
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_SPILL_DIR="$sd" python3 "$hd/gt_spill.py" hook < "$DEMO/.demo/spill-payload.json" \
    | python3 -c '
import json, sys
o = json.load(sys.stdin)["hookSpecificOutput"]["updatedToolOutput"]
text = o.get("stdout", "") if isinstance(o, dict) else str(o)
for l in text.splitlines()[:12]:
    if "withheld" in l or "env line 6" in l or "env line 8" in l:
        print("  " + l.replace(sys.argv[1], "$GT_VAULT"))' "$DEMO" | vmask || true
  echo "In the saved file:"
  grep -h -n 'withheld\|env line 8$' "$sd"/*.txt 2>/dev/null | head -2 | sed 's/^/  /' || true
  if grep -q -r -F "$key" "$sd" 2>/dev/null; then echo "  !! the key is in the spill file"; else echo "  (the key is in neither the preview nor the file)"; fi
  banner LIVE "a long paste with the same key: held back, saved with the line withheld, and the reason says how many"
  python3 - "$DEMO/.demo/paste-payload.json" "$key" <<'PY'
import json, sys
text = "".join("2026-10-15 12:%02d order %05d OK\n" % (i % 60, i) for i in range(2400))
doc = {"prompt": "Why did the oven API fail?\nOVEN_API_KEY=%s\n" % sys.argv[2] + text, "session_id": "demo-secrets",
       "transcript_path": "", "cwd": "", "hook_event_name": "UserPromptSubmit"}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(doc, fh)
PY
  shown_cmd "python3 $hd/gt_intake.py hook < \$GT_VAULT/.demo/paste-payload.json"
  HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" GT_INTAKE_DIR="$id" python3 "$hd/gt_intake.py" hook < "$DEMO/.demo/paste-payload.json" \
    | python3 -c '
import json, sys, textwrap
raw = sys.stdin.read().strip()
if not raw:
    print("  (the paste went through)")
    sys.exit()
for l in textwrap.wrap(json.loads(raw).get("reason", "").replace(sys.argv[1], "$GT_VAULT"), 90,
                       break_long_words=False, break_on_hyphens=False):
    print("  " + l)' "$DEMO" | vmask || true
  if grep -q -r -F "$key" "$id" 2>/dev/null; then echo "  !! the key is in the saved paste"; else echo "  (the saved paste holds a marker where the key was)"; fi
  banner DESCRIPTION "the same scan runs before the write broker puts a line in the vault, before gt-farm sends a packet, and before a workflow keeps a step's output"
  demo_settings spill_threshold_lines= paste_intake=
  rm -rf "$sd" "$id" "$DEMO/.demo/spill-payload.json" "$DEMO/.demo/paste-payload.json"
}

# -- A command only you can run (gt 0.21.4: gt_owner_cmd) -----------------------------------------
# The presenter's clipboard is never touched (GT_CLIPBOARD_CMD=none); on their own machine the
# same line is on the clipboard. The command handed over is a harmless read of the demo vault.
show_owner_command() {
  local oc="$DEMO/.demo/owner-cmd" out line
  rm -rf "$oc"
  banner DESCRIPTION "applying a reviewed write (design.md, global-memory/) needs a person at a terminal, so an agent's accept is refused with needs_owner"
  echo "  gt_broker.py accept: REFUSED (needs_owner): accept applies a reviewed write and needs a person at a terminal"
  echo "  -> since 0.21.4 the refusal hands over the WHOLE command (absolute paths, --version, --vault),"
  echo "     on your clipboard, so you paste it once."
  banner LIVE "the hand-off (here with no clipboard, so it is printed): the command itself"
  export GT_CLIPBOARD_CMD=none GT_OWNER_CMD_DIR="$oc"
  x python3 "$CORE_SCRIPTS/gt_owner_cmd.py" hand -- git -C "$DEMO" log -1 --format=%s
  banner LIVE "owner_command_script on: one short line runs a script that deletes itself only if the command succeeds"
  out="$(HOME="$DEMO_HOME" USERPROFILE="$DEMO_HOME_W" python3 "$CORE_SCRIPTS/gt_owner_cmd.py" hand --script --json -- git -C "$DEMO" log -1 --format=%s 2>&1)"
  line="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["paste"])' 2>/dev/null)" || line=""
  if [[ -n "$line" ]]; then
    printf '  paste: %s\n' "${line//$DEMO/\$GT_VAULT}"
    shown_cmd "$line"
    (cd "$DEMO" && eval "$line" 2>&1 | sed 's/^/  /') || true
    if ls "$oc"/*.sh >/dev/null 2>&1; then echo "  (the script is still there: the command failed)"; else echo "  (the script ran and deleted itself)"; fi
  else
    printf '%s\n' "$out"
  fi
  unset GT_CLIPBOARD_CMD GT_OWNER_CMD_DIR
  rm -rf "$oc"
  echo "-> gt never reads your clipboard and never runs the command: the gate is unchanged."
}

# -- When a guard stops you, it says why (gt 0.21.4: gt_doctor testing, scope_gate) ----------------
show_guards() {
  local t="$DEMO/.demo/blocked-session.jsonl"
  python3 - "$t" <<'PY'
import datetime, json, sys, time
def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
now = time.time() - 60
rows = []
for i in range(3):     # the same test command asked three times, and the person said yes each time
    tid, t = "t%d" % i, now + i * 10
    rows.append({"type": "assistant", "uuid": "a-" + tid, "timestamp": iso(t), "sessionId": "demo",
                 "message": {"role": "assistant", "model": "claude-x", "content": [
                     {"type": "tool_use", "id": tid, "name": "Bash",
                      "input": {"command": "python3 -m unittest discover tests"}}]}})
    rows.append({"type": "user", "uuid": "u-" + tid, "timestamp": iso(t + 1), "sessionId": "demo",
                 "sourceToolAssistantUUID": "a-" + tid, "permissionDecision":
                     {"decision": "accept", "source": "user_temporary"},
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write("".join(json.dumps(r) + "\n" for r in rows))
PY
  banner LIVE "why does my test run keep stopping? the doctor reads the session and names the gate (a fixture transcript)"
  (cd "$DEMO" && x python3 "$CORE_SCRIPTS/gt_doctor.py" testing --transcript "$t")
  rm -f "$t"
  banner DESCRIPTION "scope_gate: with an approved gt-plan, a commit that touches files outside the plan's scope is flagged (warn) before it lands"
  x python3 "$CORE_SCRIPTS/gt_settings.py" explain scope_gate
}

# -- Sessions shaped by your role, and day one (gt 0.21.4: personas, /gt:gt-help start) --------------
show_roles() {
  banner LIVE "the roles gt ships: each one shapes what is offered first (order only, nothing is hidden)"
  x python3 "$CORE_SCRIPTS/gt_persona.py" list
  banner LIVE "what a developer is offered first, and what a project manager is"
  x python3 "$CORE_SCRIPTS/gt_persona.py" featured --personas developer
  x python3 "$CORE_SCRIPTS/gt_persona.py" featured --personas project-manager
  banner LIVE "day one: /gt:gt-help start builds an offline walkthrough page for that role (built, not opened)"
  x python3 "$CORE_SCRIPTS/gt_help.py" start --persona developer --print --no-open
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

module-scripts)
  [[ "${2:-}" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "Usage: gt_demo.sh module-scripts <module name>" >&2; exit 1; }
  module_scripts "$2" || {
    echo "Module $2 is not installed (no gt-$2 plugin with module.json beside the demo or in the plugin cache) — its tour act is skipped." >&2
    exit 3
  }
  ;;

tour-acts)
  tour_acts
  ;;

show)
  show_topic "${2:-}"
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
  echo "Usage: gt_demo.sh <start|end|clean|remove|status|upstream-release|core-scripts|module-scripts NAME|tour-acts|show TOPIC>"
  exit 1
  ;;

esac
