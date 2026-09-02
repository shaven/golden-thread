#!/usr/bin/env bash
set -euo pipefail

# ── Preflight ──────────────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
  echo "✗ Python 3 is required. Install it from https://python.org and re-run."
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(sys.version_info.minor + sys.version_info.major * 100)')
if [ "$PY_VER" -lt 308 ]; then
  echo "✗ Python 3.8 or later is required (found $(python3 --version))."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$SCRIPT_DIR/golden-thread" ]; then
  echo "✗ Run this script from the golden-thread-plugin directory."
  exit 1
fi

# ── Version selection ──────────────────────────────────────────────────────
#
# The version installed is the newest version DIRECTORY present, not a constant
# maintained by hand. A hardcoded VERSION silently reinstalls a stale release
# every time a new version dir is added and the line is not bumped -- which is
# exactly how 0.6.0 stayed installed for a fortnight while 0.9.4 sat checked in
# beside it, hooks referenced from settings.json pointing at files that had never
# been copied.
#
# Deliberate rollback:  ./install.sh 0.9.3   (or GT_VERSION=0.9.3 ./install.sh)

# Sorted numerically per field, NOT lexically: a lexical sort puts 0.9.4 above
# 0.10.0 and would start reinstalling the older release the moment a minor
# version reaches double digits.
latest_version() {
  local root="$1" d name
  for d in "$root"/*/; do
    name=$(basename "$d")
    [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    # A version dir without a manifest is an archive or a work in progress, not
    # something installable.
    [ -f "$d/.claude-plugin/plugin.json" ] || continue
    echo "$name"
  done | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}

# The directory name decides what gets copied; plugin.json's version field is what
# every consumer reads back afterwards. If they disagree one of them is lying, and
# guessing which would defeat the point of detecting it.
assert_manifest_version() {
  local dir="$1" expected="$2" label="$3" actual
  actual=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('version',''))" \
           "$dir/.claude-plugin/plugin.json" 2>/dev/null) || actual=""
  if [ "$actual" != "$expected" ]; then
    echo "✗ $label version mismatch: directory is $expected, plugin.json says '${actual:-<none>}'."
    echo "  Reconcile them before installing — consumers read the manifest, not the path."
    exit 1
  fi
}

REQUESTED="${1:-${GT_VERSION:-}}"
VERSION="${REQUESTED:-$(latest_version "$SCRIPT_DIR/golden-thread")}"
WIKI_VERSION="${GT_WIKI_VERSION:-$(latest_version "$SCRIPT_DIR/golden-thread-wiki")}"

for spec in "gt:$VERSION:$SCRIPT_DIR/golden-thread" "gt-wiki:$WIKI_VERSION:$SCRIPT_DIR/golden-thread-wiki"; do
  label="${spec%%:*}"; rest="${spec#*:}"; ver="${rest%%:*}"; root="${rest#*:}"
  if [ -z "$ver" ]; then
    echo "✗ No installable $label version found under $root"
    echo "  (need an N.N.N directory containing .claude-plugin/plugin.json)"
    exit 1
  fi
  if [ ! -d "$root/$ver" ]; then
    echo "✗ $label version $ver requested, but $root/$ver does not exist."
    echo "  Available: $(ls -1 "$root" 2>/dev/null | tr '\n' ' ')"
    exit 1
  fi
  assert_manifest_version "$root/$ver" "$ver" "$label"
done

if [ -n "$REQUESTED" ]; then
  echo "Installing gt $VERSION (pinned; newest available is $(latest_version "$SCRIPT_DIR/golden-thread"))"
else
  echo "Installing gt $VERSION (newest version directory), gt-wiki $WIKI_VERSION"
fi

PLUGIN_KEY="gt@golden-thread-plugin"
WIKI_PLUGIN_KEY="gt-wiki@golden-thread-plugin"
SRC="$SCRIPT_DIR/golden-thread/$VERSION"
WIKI_SRC="$SCRIPT_DIR/golden-thread-wiki/$WIKI_VERSION"
CACHE="$HOME/.claude/plugins/cache/golden-thread-plugin/gt/$VERSION"
WIKI_CACHE="$HOME/.claude/plugins/cache/golden-thread-plugin/gt-wiki/$WIKI_VERSION"
MARKETPLACE="$HOME/.claude/plugins/marketplaces/golden-thread-plugin"
SETTINGS="$HOME/.claude/settings.json"
INSTALLED="$HOME/.claude/plugins/installed_plugins.json"
KNOWN="$HOME/.claude/plugins/known_marketplaces.json"

# 0. Remove superseded gt versions so old caches don't linger unreferenced
for old in "$HOME/.claude/plugins/cache/golden-thread-plugin/gt"/*; do
  [ -d "$old" ] || continue
  if [ "$(basename "$old")" != "$VERSION" ]; then
    rm -rf "$old"
    echo "Removed superseded gt cache → $old"
  fi
done

# 1. Install plugin files into cache
mkdir -p "$CACHE"
for dir in .claude-plugin skills scripts templates commands hooks; do
  [ -d "$SRC/$dir" ] && cp -r "$SRC/$dir" "$CACHE/"
done
echo "Installed gt plugin files → $CACHE"

# 1b. Install the Core-rule hooks to a STABLE location outside the vault.
# settings.json references these by absolute path, so the path must survive project
# renames, merges and vault moves. The scripts locate the rules at run time.
GT_HOOKS="$HOME/.claude/golden-thread/hooks"
if [ -d "$SRC/hooks" ]; then
  mkdir -p "$GT_HOOKS"
  find "$SRC/hooks" -maxdepth 1 -type f -exec cp {} "$GT_HOOKS/" \;
  cp "$SRC/scripts/gt_paths.py" "$GT_HOOKS/gt_paths.py"
  # Component drift detection + the session report card run FROM the hooks dir,
  # for the same reason the hooks themselves do: settings.json addresses them by
  # absolute path, so the path must survive a vault move or a project rename.
  for extra in gt_components.py gt_report_card.py gt_settings.py gt_workers.py gt_version_check.py; do
    [ -f "$SRC/scripts/$extra" ] && cp "$SRC/scripts/$extra" "$GT_HOOKS/$extra"
  done
  chmod +x "$GT_HOOKS"/*.sh 2>/dev/null || true
  chmod +x "$GT_HOOKS"/*.py 2>/dev/null || true
  echo "Installed Core-rule hooks → $GT_HOOKS"
fi

# 1c. Component MANIFEST. gt_components.py compares what is INSTALLED against
# these hashes at session start. Generated at install time so it always describes
# the version actually being shipped -- a hand-maintained manifest would drift,
# which is the failure this whole mechanism exists to detect.
if [ -f "$SRC/scripts/gt_components.py" ]; then
  python3 "$SRC/scripts/gt_components.py" manifest "$SRC" >/dev/null 2>&1 \
    && echo "Wrote component MANIFEST → $SRC/MANIFEST.json"
fi

mkdir -p "$WIKI_CACHE"
for dir in .claude-plugin skills scripts templates commands; do
  [ -d "$WIKI_SRC/$dir" ] && cp -r "$WIKI_SRC/$dir" "$WIKI_CACHE/"
done
echo "Installed gt-wiki plugin files → $WIKI_CACHE"

# 2. Create marketplace directory structure
mkdir -p "$MARKETPLACE/.claude-plugin"
mkdir -p "$MARKETPLACE/plugins/gt/.claude-plugin"
mkdir -p "$MARKETPLACE/plugins/gt-wiki/.claude-plugin"

cat > "$MARKETPLACE/.claude-plugin/marketplace.json" <<JSON
{
  "name": "golden-thread-plugin",
  "owner": {
    "name": "Stacy Haven",
    "email": "shaven@shavenconsulting.com"
  },
  "plugins": [
    {
      "name": "gt",
      "source": "./plugins/gt",
      "description": "Vault-based AI memory system. Turns an Obsidian vault into the single source of truth for all Claude Code sessions across projects."
    },
    {
      "name": "gt-wiki",
      "source": "./plugins/gt-wiki",
      "description": "LLM-powered knowledge base with immutable sources, interlinked pages, and a maintenance loop."
    }
  ]
}
JSON

# The plugin manifests are copied from source, not regenerated here. Two
# hand-maintained copies of the same manifest drift - the descriptions had
# already diverged once.
cp "$SRC/.claude-plugin/plugin.json" "$MARKETPLACE/plugins/gt/.claude-plugin/plugin.json"
cp "$WIKI_SRC/.claude-plugin/plugin.json" "$MARKETPLACE/plugins/gt-wiki/.claude-plugin/plugin.json"

# 2b. Populate the marketplace's plugin directories with the ACTUAL plugin.
# marketplace.json declares "source": "./plugins/gt", so that path must hold a
# loadable plugin - not just a manifest. Without this, anything that resolves the
# plugin from the marketplace (rather than from the installed cache) finds zero
# skills, and no /gt: commands appear.
for dir in skills scripts templates commands hooks; do
  rm -rf "$MARKETPLACE/plugins/gt/$dir"
  [ -d "$SRC/$dir" ] && cp -r "$SRC/$dir" "$MARKETPLACE/plugins/gt/$dir"
  rm -rf "$MARKETPLACE/plugins/gt-wiki/$dir"
  [ -d "$WIKI_SRC/$dir" ] && cp -r "$WIKI_SRC/$dir" "$MARKETPLACE/plugins/gt-wiki/$dir"
done
echo "Populated marketplace plugin directories with skills/scripts/templates"

echo "Created marketplace entries → $MARKETPLACE"

# 3. Register in known_marketplaces.json
python3 - <<EOF
import json, os
path = '$KNOWN'
os.makedirs(os.path.dirname(path), exist_ok=True)
d = json.load(open(path)) if os.path.exists(path) else {}
d['golden-thread-plugin'] = {
    'source': {'source': 'directory', 'path': '$MARKETPLACE'},
    'installLocation': '$MARKETPLACE',
    'lastUpdated': '2026-08-15T00:00:00.000Z'
}
open(path, 'w').write(json.dumps(d, indent=2) + '\n')
print('Registered in known_marketplaces.json')
EOF

# 4. Register in installed_plugins.json
python3 - <<EOF
import json, os
from datetime import datetime, timezone
path = '$INSTALLED'
os.makedirs(os.path.dirname(path), exist_ok=True)
d = json.load(open(path)) if os.path.exists(path) else {'version': 2, 'plugins': {}}
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
d['plugins']['$PLUGIN_KEY'] = [{
    'scope': 'user',
    'installPath': '$CACHE',
    'version': '$VERSION',
    'installedAt': now,
    'lastUpdated': now,
    'gitCommitSha': 'local'
}]
d['plugins']['$WIKI_PLUGIN_KEY'] = [{
    'scope': 'user',
    'installPath': '$WIKI_CACHE',
    'version': '$WIKI_VERSION',
    'installedAt': now,
    'lastUpdated': now,
    'gitCommitSha': 'local'
}]
open(path, 'w').write(json.dumps(d, indent=2) + '\n')
print('Registered in installed_plugins.json')
EOF

# 5. Register in settings.json (enabledPlugins)
python3 - <<EOF
import json, os
path = '$SETTINGS'
os.makedirs(os.path.dirname(path), exist_ok=True)
d = json.load(open(path)) if os.path.exists(path) else {}
d.setdefault('enabledPlugins', {})['$PLUGIN_KEY'] = True
d['enabledPlugins']['$WIKI_PLUGIN_KEY'] = True
open(path, 'w').write(json.dumps(d, indent=2) + '\n')
print('Registered in settings.json')
EOF

echo ""
echo "Golden Thread $VERSION installed."
echo ""
echo "gt skills:"
echo "  /gt:gt-init          set up the vault, wire a project, write vault-config.json"
echo "  /gt:gt-open          load a project — source.md first, memory index only"
echo "  /gt:gt-create        scaffold a project and freeze its idea.md"
echo "  /gt:gt-ingest        import an existing project's notes (copies, never moves)"
echo "  /gt:gt-work          write the session back to research/decisions/design"
echo "  /gt:gt-promote       graduate a fact up a level, or out to a repo CLAUDE.md"
echo "  /gt:gt-validate      re-derive a claim with a fresh-context validator"
echo "  /gt:gt-query         look a topic up across Knowledge and project memory"
echo "  /gt:gt-review        sweep daily notes for uncaptured tasks and ideas"
echo "  /gt:gt-farm          hand bulk or mechanical work to an external AI as a packet"
echo "  /gt:gt-refresh       check Sources/ for upstream changes and supersede"
echo "  /gt:gt-lint          14 health checks, including core-unenforced"
echo "  /gt:gt-settings      inspect or switch off anything this plugin does on its own"
echo "  /gt:gt-runbook-lint  find facts duplicated across runbooks"
echo ""
echo "gt-wiki skills:"
echo "  /gt-wiki:gt-wiki-init     set up a new wiki vault"
echo "  /gt-wiki:gt-wiki          query the wiki"
echo "  /gt-wiki:gt-wiki-ingest   add a source to the wiki"
echo "  /gt-wiki:gt-wiki-lint     health-check the wiki"
echo "  /gt-wiki:gt-wiki-refresh  check sources for upstream changes"
echo ""
echo "Restart Claude Code to load the plugins."

# 6. Register the SessionStart / PreCompact / SessionEnd hooks.
#
# SessionStart  -> gt_components.py check   : is what is INSTALLED still what is
#                  CHECKED IN? Found live on 2026-08-29 that guard_session_claims.sh
#                  was installed but absent from the plugin source entirely, and
#                  validate_response.sh had drifted -- two of three enforcement
#                  mechanisms existing on one machine only.
# PreCompact    -> gt_report_card.py        : fires on BOTH `/compact` and the
#                  automatic compaction near the context limit, which is the point:
#                  a report card produced at the very end of a session competes for
#                  the context it needs to be written.
# SessionEnd    -> gt_report_card.py        : backstop for sessions that never compact.
python3 - <<EOF
import json, os
p = os.path.expanduser('~/.claude/settings.json')
d = json.load(open(p)) if os.path.exists(p) else {}
hooks = d.setdefault('hooks', {})
gt = os.path.expanduser('~/.claude/golden-thread/hooks')
# (event, script filename, command). The script is named EXPLICITLY rather than
# parsed back out of the command: the previous version derived it with
# cmd.split('/')[-1], which returns the last segment of the whole string -- for any
# command ending in a quoted path argument that is the ARGUMENT, not the script.
# gt_components resolved to '0.9.4' and gt_version_check to 'golden-thread-plugin',
# a substring of the gt_components command, so registering one would have deleted
# the other from the same loop that had just added it.
#
# Paths are QUOTED: the plugin source lives under "Golden Thread", and an unquoted
# path split on that space so the checker was handed "Golden" and reported a bogus
# no-manifest drift on every session start.
#
# Three independent SessionStart concerns, deliberately separate commands: a failure
# in one must not suppress the others.
want = [
    ('SessionStart', 'gt_components.py',
     'python3 "%s/gt_components.py" check "%s"' % (gt, '$SRC')),
    ('SessionStart', 'gt_workers.py',
     'python3 "%s/gt_workers.py" check' % gt),
    # Handed the SOURCE ROOT, not a version dir -- deliberately. gt_components is
    # pinned to the version it was installed against, which is exactly what makes it
    # blind to a newer release sitting beside it; passing the root keeps this hook
    # correct without being rewritten on every version bump.
    ('SessionStart', 'gt_version_check.py',
     'python3 "%s/gt_version_check.py" check "%s"' % (gt, '$SCRIPT_DIR')),
    ('PreCompact', 'gt_report_card.py', 'python3 "%s/gt_report_card.py"' % gt),
    ('SessionEnd', 'gt_report_card.py', 'python3 "%s/gt_report_card.py"' % gt),
]
changed = []
for event, script, cmd in want:
    arr = hooks.setdefault(event, [])
    # Replace the entry for THIS script only, not every golden-thread entry for the
    # event -- SessionStart now has three, and wiping by event would delete the
    # siblings registered moments earlier in this same loop.
    arr[:] = [e for e in arr
              if not any(script in (h.get('command') or '')
                         for h in e.get('hooks', []))]
    arr.append({'hooks': [{'type': 'command', 'command': cmd}]})
    changed.append('%s/%s' % (event, script))
open(p, 'w').write(json.dumps(d, indent=2) + '\n')
print('Registered hooks: ' + ', '.join(sorted(changed)))
EOF

# 7. Wire the VAULT's git repo for per-edit attribution, if it is one.
#
# .git/hooks is not tracked and does not survive a clone, so the hooks ship in a
# tracked .githooks/ and core.hooksPath points at it. That config is per-clone
# local state -- which is exactly why it belongs in the installer rather than in a
# README nobody re-reads on a new machine.
VAULT_PATH=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null)
if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH/.git" ]; then
  mkdir -p "$VAULT_PATH/.githooks" "$VAULT_PATH/Projects/golden-thread/tools"
  if [ -d "$SRC/templates/githooks" ]; then
    cp "$SRC/templates/githooks/"* "$VAULT_PATH/.githooks/" 2>/dev/null || true
    chmod +x "$VAULT_PATH/.githooks/"* 2>/dev/null || true
  fi
  # Tools are only SEEDED, never overwritten: a vault's copy may have been fixed
  # locally, and clobbering it here would repeat the mistake this release exists
  # to fix -- an update that silently reverts work only present on one machine.
  #
  # Seeding alone is not enough once a CORRECTNESS fix lands, though. A vault
  # seeded before one keeps the broken copy through every future reinstall and
  # nothing says so. That is how safe_write's truncating append would have
  # survived 0.9.6 on every machine that already had a vault: reinstalling looks
  # like it updated the tools, and for the file that mattered it did nothing.
  #
  # So a tool may declare the function that must be present. A copy missing it
  # predates the fix and is repaired rather than left -- after backing the local
  # file up, so a genuine local edit is recoverable instead of lost. Tools with
  # no declared contract keep the old seed-only behaviour untouched.
  #
  # Only list a function here when its absence means a KNOWN defect. A contract
  # asserted for its own sake would "repair" files whose only sin was being edited.
  TOOL_CONTRACTS="safe_write.py:_resolve_append"
  if [ -d "$SRC/templates/tools" ]; then
    for t in "$SRC/templates/tools/"*.py; do
      [ -f "$t" ] || continue
      base=$(basename "$t")
      dest="$VAULT_PATH/Projects/golden-thread/tools/$base"
      if [ ! -f "$dest" ]; then
        cp "$t" "$dest"
        echo "Seeded vault tool → $base"
        continue
      fi
      want=""
      for pair in $TOOL_CONTRACTS; do
        case "$pair" in "$base":*) want="${pair#*:}" ;; esac
      done
      [ -n "$want" ] || continue
      if grep -q "def $want" "$dest"; then
        echo "Vault tool verified → $base defines $want()"
      else
        bak="$dest.bak_$(date +%Y%m%d_%H%M%S)_missing_$want"
        cp "$dest" "$bak"
        cp "$t" "$dest"
        echo "⚠ Vault tool REPLACED → $base was missing $want() and predates a"
        echo "  correctness fix. Your previous copy is kept at $(basename "$bak")"
        echo "  -- diff it if you had local edits."
      fi
    done
  fi
  git -C "$VAULT_PATH" config core.hooksPath .githooks 2>/dev/null \
    && echo "Wired vault git attribution → $VAULT_PATH (.githooks)"
fi
