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

VERSION="0.9.0"
WIKI_VERSION="0.1.0"
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
  chmod +x "$GT_HOOKS"/*.sh
  echo "Installed Core-rule hooks → $GT_HOOKS"
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
echo "  /gt:gt-refresh       check Sources/ for upstream changes and supersede"
echo "  /gt:gt-lint          13 health checks, including core-unenforced"
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
