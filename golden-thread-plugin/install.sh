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
# Deliberate rollback:  ./install.sh 0.11.0  (or GT_VERSION=0.11.0 ./install.sh)
#
# Only the current release and the one before it are kept on disk (2026-09-12: fifteen
# version directories were 9.6 MB of tree that nothing read). Every earlier release is
# still in git, so rolling back further is two steps rather than one:
#
#   git log --oneline -- golden-thread/0.9.14        # find the commit that had it
#   git checkout <commit> -- golden-thread/0.9.14    # restore the directory
#   ./install.sh 0.9.14
#
# Restoring it also makes it the newest-but-one again, never the newest: latest_version
# picks numerically, so a restored 0.9.14 cannot silently become what install.sh
# installs by default.

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

# ---- arguments ---------------------------------------------------------------
#
#   ./install.sh                     install the newest release
#   ./install.sh 0.12.1              install a specific release (rollback)
#   ./install.sh --vault <path>      install AND create/connect that vault
#   ./install.sh --no-vault          install the plugin only, on purpose
#
# --vault exists because an install that ends with no vault ends with the Core-rule
# enforcement hooks UNWIRED: they are wired against a vault. One command should be
# able to finish the job.
VAULT_ARG="${GT_VAULT:-}"
FORCE_MANIFEST=no
NO_VAULT=no
POSITIONAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force-manifest-mismatch) FORCE_MANIFEST=yes; shift ;;
    --vault)    VAULT_ARG="${2:-}"; shift 2 || true ;;
    --vault=*)  VAULT_ARG="${1#--vault=}"; shift ;;
    --no-vault) NO_VAULT=yes; shift ;;
    -h|--help)
      cat <<'USAGE'
install.sh — install the gt and gt-wiki Claude Code plugins

  ./install.sh                        install the newest release
  ./install.sh 0.12.1                 install a specific release (deliberate rollback)
  ./install.sh --vault <path>         install AND create or connect that vault
  ./install.sh --force-manifest-mismatch
                                      install even when shipped files disagree with
                                      MANIFEST.json (see below)
  ./install.sh --no-vault             install the plugin only, on purpose
  ./install.sh --help                 this text

A vault is a plain folder of markdown where your memory lives. The Core-rule
enforcement hooks are wired AGAINST a vault, so an install with no vault leaves them
present but inert. --vault finishes the job in one command.

With no vault and no --vault:
  * at a terminal, you are asked where the vault should go;
  * otherwise (an agent, a pipe, CI) the install stops with exit 4 and says what it
    needs, rather than inventing a directory and claiming ~/.claude/vault-config.json.

Environment: GT_VAULT (same as --vault), GT_VERSION (same as the version argument).
USAGE
      exit 0 ;;
    -*) echo "unknown option: $1"; echo "try: install.sh [version] [--vault <path>] [--no-vault]"; exit 1 ;;
    *)  POSITIONAL="$1"; shift ;;
  esac
done
REQUESTED="${POSITIONAL:-${GT_VERSION:-}}"
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

# 0. Remove superseded gt AND gt-wiki versions so old caches don't linger unreferenced.
# The cache holds only what installed_plugins.json points at; rollback reads the SOURCE
# tree (which keeps the newest-but-one, see above), never an old cache. gt-wiki was
# missed until 0.13.0, so its caches piled up (0.1.0 and 0.1.1 beside the current one).
for spec in "gt:$VERSION" "gt-wiki:$WIKI_VERSION"; do
  plugin="${spec%%:*}"; keep="${spec#*:}"
  for old in "$HOME/.claude/plugins/cache/golden-thread-plugin/$plugin"/*; do
    [ -d "$old" ] || continue
    if [ "$(basename "$old")" != "$keep" ]; then
      rm -rf "$old"
      echo "Removed superseded $plugin cache → $old"
    fi
  done
done

# Read install_demo setting (default: yes). A user who has set install_demo=no
# in vault-config.json gets the plugin without the demo skill, script, and templates.
VAULT_CONFIG="$HOME/.claude/vault-config.json"
INSTALL_DEMO="yes"
if [ -f "$VAULT_CONFIG" ]; then
  _val=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(str(d.get('install_demo') or 'yes').strip().lower())" \
    "$VAULT_CONFIG" 2>/dev/null || echo "yes")
  [ "$_val" = "no" ] && INSTALL_DEMO="no"
fi
if [ "$INSTALL_DEMO" = "no" ]; then
  echo "install_demo=no — skipping demo skill, script, and templates"
fi

# Do the files about to be installed match the manifest shipping beside them?
#
# CALLED BEFORE ANYTHING IS COPIED. A refusal has to mean nothing was installed, and the
# first version of this check ran at step 1c -- after the cache copy -- so it "refused" a
# tree it had already installed. Same ordering mistake as the 0.12.5 machine profile,
# found the same way: by running it and looking, not by reading it.
#
# Since 0.12.6 the installer no longer regenerates the manifest, so source and manifest
# CAN disagree and nothing at install time noticed. dev/release-check.sh catches it, but
# only on the machine that runs the gate: a second machine installing from gt-src would
# install files the manifest does not describe, be told nothing, and then report component
# drift at every session start. On 2026-09-12 exactly that shipped -- 0.12.4 was committed
# with a stale manifest and only the gate saw it.
#
# Policy (owner decision, 2026-09-12): REFUSE when a file that EXECUTES disagrees, WARN
# when only copied files do, and treat a developer's uncommitted or untracked edit as the
# warning case. That last clause is what keeps this usable -- editing a script and
# installing to test it is the normal loop here, and a gate that fires on the normal loop
# gets overridden by reflex and then ignored.
verify_source_tree() {
  [ -f "$SRC/scripts/gt_components.py" ] || return 0
  [ -f "$SRC/MANIFEST.json" ] || return 0        # written later, when absent
  # `set -e` aborts on a failing command substitution, so the status must be captured in
  # the same statement. The first version wrote `OUT=$(...)` then `RC=$?` on the next
  # line, and every refusal killed the script with the helper's own exit code before the
  # policy below ever ran.
  local out rc=0
  out=$(python3 "$SRC/scripts/gt_components.py" verify-source "$SRC" 2>&1) || rc=$?
  case $rc in
    0) [ -n "${out##source matches*}" ] && {
         echo "Manifest: local edits present, installing anyway:"
         printf '%s\n' "$out" | sed 's/^/  /'; }
       return 0 ;;
    4) echo "⚠ Manifest mismatch in copied files — installing anyway:"
       printf '%s\n' "$out" | sed 's/^/  /'
       return 0 ;;
    3) if [ "$FORCE_MANIFEST" = yes ]; then
         echo "⚠ Manifest mismatch in EXECUTABLE files — overridden by --force-manifest-mismatch:"
         printf '%s\n' "$out" | sed 's/^/  /'
         return 0
       fi
       echo ""
       echo "REFUSING TO INSTALL — shipped files that EXECUTE do not match MANIFEST.json"
       printf '%s\n' "$out" | sed 's/^/  /'
       echo ""
       echo "Nothing has been installed. These files run on every prompt, so installing a"
       echo "set the manifest does not describe means running code nobody has verified, and"
       echo "every later component check reporting drift this could have caught once."
       echo ""
       echo "  Meant to change them?   regenerate the manifest with the command above,"
       echo "                          then re-run this installer"
       echo "  Installing anyway?      ./install.sh --force-manifest-mismatch"
       exit 6 ;;
    *) echo "Manifest check could not run (exit $rc) — continuing:"
       printf '%s\n' "$out" | sed 's/^/  /'
       return 0 ;;
  esac
}
verify_source_tree

# 1. Install plugin files into cache
mkdir -p "$CACHE"
for dir in .claude-plugin skills scripts templates commands hooks; do
  [ -d "$SRC/$dir" ] && cp -r "$SRC/$dir" "$CACHE/"
done
# The demo is removed AFTER the copy rather than filtered during it: a filter has to
# know every file type it lets through, and one that copied only *.md and *.json
# silently dropped the rest. Removing afterwards also clears a demo left behind by an
# earlier install_demo=yes run.
[ "$INSTALL_DEMO" = "no" ] && rm -rf "$CACHE/skills/gt-demo" "$CACHE/scripts/gt_demo.sh" "$CACHE/templates/demo-pizzabot"
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
  # The list lives in gt_components.HOOK_DIR_SCRIPTS, which also maps these files
  # for drift checking -- a second copy here would be a copy that drifts.
  for extra in $(python3 "$SRC/scripts/gt_components.py" hookdir-scripts); do
    [ -f "$SRC/scripts/$extra" ] && cp "$SRC/scripts/$extra" "$GT_HOOKS/$extra"
  done
  chmod +x "$GT_HOOKS"/*.sh 2>/dev/null || true
  chmod +x "$GT_HOOKS"/*.py 2>/dev/null || true
  echo "Installed Core-rule hooks → $GT_HOOKS"

  # Files an OLDER release put in the hooks dir that this one no longer ships (0.13.0).
  # An upgrade must CONVERGE on what a fresh install of this release leaves, so legacy
  # files cannot just accumulate -- but the hooks dir has also held a file that existed
  # NOWHERE else (2026-08-29: guard_session_claims.sh, installed but absent from source),
  # so "not shipped" alone is never grounds to delete. The rule:
  #   * listed in this release's retired.json AND not shipped now -> removed, after a
  #     copy to ~/.claude/golden-thread/backups/hooks-retired.<stamp>/;
  #   * anything else not shipped -> "unknown file, left in place".
  # retired.json ships IN the release because install.sh runs from gt-src, which has no
  # git history to ask. If the shipped set cannot be established, nothing is removed.
  python3 - "$SRC" "$GT_HOOKS" <<'EOF' || true
import json, os, shutil, subprocess, sys, time
src, hooks = sys.argv[1:3]
shipped = {"gt_paths.py"}
hdir = os.path.join(src, "hooks")
shipped |= {n for n in os.listdir(hdir) if os.path.isfile(os.path.join(hdir, n))}
certain = True
try:
    out = subprocess.check_output(
        ["python3", os.path.join(src, "scripts", "gt_components.py"), "hookdir-scripts"],
        text=True)
    shipped |= {n for n in out.split()
                if os.path.isfile(os.path.join(src, "scripts", n))}
except (OSError, subprocess.SubprocessError):
    certain = False
retired = set()
try:
    with open(os.path.join(src, "retired.json"), encoding="utf-8") as fh:
        retired = {e["installed_as"] for e in json.load(fh).get("hook_files", [])
                   if isinstance(e, dict) and e.get("installed_as")}
except FileNotFoundError:
    pass
except (OSError, ValueError, KeyError, TypeError) as exc:
    print("⚠ retired.json unreadable (%s) — no hook files removed" % exc)
    certain = False
present = sorted(n for n in os.listdir(hooks)
                 if os.path.isfile(os.path.join(hooks, n)) and not n.startswith(".")
                 and not n.endswith(".pyc") and n not in shipped)
gone = [n for n in present if certain and n in retired and os.sep not in n]
unknown = [n for n in present if n not in gone]
if gone:
    bak = os.path.expanduser("~/.claude/golden-thread/backups/hooks-retired.%s"
                             % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(bak, exist_ok=True)
    print("Hooks dir: removed %d file(s) this release retired (backup: %s):" % (len(gone), bak))
    for n in gone:
        shutil.copy2(os.path.join(hooks, n), os.path.join(bak, n))
        os.remove(os.path.join(hooks, n))
        print("  %s" % n)
if unknown:
    print("Hooks dir: %d unknown file(s), not shipped by this release, left in place:"
          % len(unknown))
    for n in unknown:
        print("  %s" % n)
EOF
fi

# 1c. Component MANIFEST. gt_components.py compares what is INSTALLED against
# these hashes at session start.
#
# It used to be REGENERATED here on every run, so that it always described the
# version actually being shipped -- a hand-maintained manifest would drift, which is
# the failure the whole mechanism exists to detect. That reasoning was right when it
# was written and is now covered better elsewhere: dev/release-check.sh verifies
# "MANIFEST.json files map matches the tree" on every release, and that gate did not
# exist then. The drift protection is kept; only its location moved.
#
# Regenerating here cost more than it bought. Every install rewrote the file's
# `generated` timestamp INSIDE THE SOURCE TREE, so each run dirtied git, and
# dev/sync-gt-src.sh -- which refuses to publish from an uncommitted tree -- had to
# be unblocked by committing the noise. On 2026-09-11 one session made four commits
# whose entire content was a changed timestamp. Anyone installing from the shared
# gt-src copy was also silently modifying it.
#
# So: write it only when it is ABSENT, because gt_components.compare() cannot do
# anything without one. Never overwrite a manifest that is already there.
if [ -f "$SRC/scripts/gt_components.py" ]; then
  if [ -f "$SRC/MANIFEST.json" ]; then
    echo "Component MANIFEST present → left untouched (verified by dev/release-check.sh)"
  else
    python3 "$SRC/scripts/gt_components.py" manifest "$SRC" >/dev/null 2>&1 \
      && echo "Wrote component MANIFEST → $SRC/MANIFEST.json  (none was present)"
  fi

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
[ "$INSTALL_DEMO" = "no" ] && rm -rf "$MARKETPLACE/plugins/gt/skills/gt-demo" \
  "$MARKETPLACE/plugins/gt/scripts/gt_demo.sh" "$MARKETPLACE/plugins/gt/templates/demo-pizzabot"
echo "Populated marketplace plugin directories with skills/scripts/templates"

echo "Created marketplace entries → $MARKETPLACE"

# 3-5. Register in known_marketplaces.json, installed_plugins.json and
# settings.json (enabledPlugins).
#
# Values reach Python through argv, never by interpolating shell variables into
# Python source. 0.9.6 wrote `path = '$KNOWN'`, so any path holding an apostrophe
# (an iCloud or OneDrive folder called "Sam's Projects") became a syntax error
# halfway through an install that had already copied the hooks -- a half-installed
# plugin with no message saying so. Now that strangers clone this, their folder
# names are not ours to assume.
#
# Each file is written to a sibling temp file and renamed into place, and the
# original is backed up once per install under ~/.claude/golden-thread/backups/.
# A crash mid-write used to leave settings.json truncated.
python3 - "$KNOWN" "$MARKETPLACE" "$INSTALLED" "$CACHE" "$VERSION" "$WIKI_CACHE" "$WIKI_VERSION" "$SETTINGS" "$PLUGIN_KEY" "$WIKI_PLUGIN_KEY" <<'EOF'
import json, os, sys, tempfile, time
from datetime import datetime, timezone
(known, marketplace, installed, cache, version, wiki_cache, wiki_version,
 settings, plugin_key, wiki_plugin_key) = sys.argv[1:11]
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUPS = os.path.expanduser("~/.claude/golden-thread/backups")

def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)

def save(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.makedirs(BACKUPS, exist_ok=True)
        bak = os.path.join(BACKUPS, "%s.%s" % (os.path.basename(path), STAMP))
        if not os.path.exists(bak):
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".gt-")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(d, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

d = load(known, {})
d["golden-thread-plugin"] = {
    "source": {"source": "directory", "path": marketplace},
    "installLocation": marketplace,
    "lastUpdated": "2026-08-15T00:00:00.000Z",
}
save(known, d)
print("Registered in known_marketplaces.json")

d = load(installed, {"version": 2, "plugins": {}})
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
for key, path, ver in ((plugin_key, cache, version), (wiki_plugin_key, wiki_cache, wiki_version)):
    d.setdefault("plugins", {})[key] = [{
        "scope": "user", "installPath": path, "version": ver,
        "installedAt": now, "lastUpdated": now, "gitCommitSha": "local",
    }]
save(installed, d)
print("Registered in installed_plugins.json")

d = load(settings, {})
d.setdefault("enabledPlugins", {})[plugin_key] = True
d["enabledPlugins"][wiki_plugin_key] = True
save(settings, d)
print("Registered in settings.json  (backups in %s)" % BACKUPS)
EOF


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
python3 - "$SRC" "$SCRIPT_DIR" <<'EOF'
import json, os, subprocess, sys, tempfile
src, script_dir = sys.argv[1:3]
p = os.path.expanduser('~/.claude/settings.json')
d = json.load(open(p)) if os.path.exists(p) else {}
hooks = d.setdefault('hooks', {})

# The list of what to register is NOT written here. It lives in
# gt_components.HOOK_REGISTRATIONS, which is also what gets baked into
# MANIFEST.json and what the session-start wiring check compares against. A copy
# in shell would be a copy that drifts -- and a drift between "what the installer
# wires" and "what the checker expects wired" would make the checker cry wolf on
# every start, or worse, stay silent about the entry it forgot.
#
# Only install.sh's OWN entries are wired here. The four enforcement hooks are
# declared in the same list but owned by vault_init.py, which needs a vault --
# this script may run before one exists. Step 7 wires them once the vault path is
# known, so an UPGRADE registers a newly shipped hook instead of leaving it inert.
regs = json.loads(subprocess.check_output(
    ['python3', os.path.join(src, 'scripts', 'gt_components.py'),
     'hook-registrations', src, script_dir], text=True))
want = [(r['event'], r['script'], r['command'])
        for r in regs if r.get('owner') == 'install.sh']

changed = []
for event, script, cmd in want:
    arr = hooks.setdefault(event, [])
    # Replace the entry for THIS script only, not every golden-thread entry for the
    # event -- SessionStart has four, and wiping by event would delete the
    # siblings registered moments earlier in this same loop.
    arr[:] = [e for e in arr
              if not any(script in (h.get('command') or '')
                         for h in e.get('hooks', []))]
    arr.append({'hooks': [{'type': 'command', 'command': cmd}]})
    changed.append('%s/%s' % (event, script))

# Prune entries an OLDER release wired and this one dropped (0.13.0). Until now the
# installer only ever added, so a hook removed from HOOK_REGISTRATIONS stayed in
# settings.json pointing at a file nothing maintains, and an upgrade never converged
# on what a fresh install leaves. The rule is narrow on purpose:
#   * only a command that points INTO the gt hooks dir is a candidate -- a user's own
#     hooks are never ours to judge, whatever they run;
#   * a script this release registers (under ANY owner, so the enforcement hooks
#     vault_init wires later are not stripped here) survives under the events it is
#     registered for, and is removed from any other event -- gt owns that script;
#   * a script in retired.json's hook_registrations is removed;
#   * anything else in the hooks dir is UNKNOWN and left in place, reported. Same rule
#     as hooks-dir files: gt deletes only what it knows it once shipped.
import shlex, time
HOOKS_DIR = os.path.realpath(os.path.expanduser('~/.claude/golden-thread/hooks'))
known = {r['script'] for r in regs}
pairs = {(r['event'], r['script']) for r in regs}
try:
    with open(os.path.join(src, 'retired.json'), encoding='utf-8') as fh:
        retired = {x for x in json.load(fh).get('hook_registrations', [])
                   if isinstance(x, str)} - known
except FileNotFoundError:
    retired = set()
except (OSError, ValueError, AttributeError) as exc:
    print('⚠ retired.json unreadable (%s) — no retired hook entries removed' % exc)
    retired = set()

def gt_script(command):
    """-> basename of the gt-hooks-dir script this command runs, or None."""
    try:
        tokens = shlex.split(command or '')
    except ValueError:
        tokens = (command or '').split()
    for tok in tokens:
        t = os.path.expanduser(tok.replace('${HOME}', '~').replace('$HOME', '~'))
        if not os.path.isabs(t):
            continue
        parent = os.path.realpath(os.path.dirname(t))
        if parent == HOOKS_DIR:
            return os.path.basename(t)
    return None

removed, unknown, bak = [], [], None
for event, blocks in list(hooks.items()):
    if not isinstance(blocks, list):
        continue
    before = len(removed)
    for b in blocks:
        if not isinstance(b, dict) or not isinstance(b.get('hooks'), list):
            continue
        keep = []
        for h in b['hooks']:
            s = gt_script(h.get('command') if isinstance(h, dict) else None)
            if s is not None and (s in retired
                                  or (s in known and (event, s) not in pairs)):
                removed.append('%s/%s' % (event, s))
                continue
            if s is not None and s not in known:
                unknown.append('%s/%s' % (event, s))
            keep.append(h)
        b['hooks'] = keep
    # A block emptied by the prune goes too; one that still holds a user's hook stays.
    blocks[:] = [b for b in blocks
                 if not (isinstance(b, dict) and isinstance(b.get('hooks'), list)
                         and not b['hooks'])]
    if not blocks and len(removed) > before:
        del hooks[event]
if removed and os.path.exists(p):
    backups = os.path.expanduser('~/.claude/golden-thread/backups')
    os.makedirs(backups, exist_ok=True)
    bak = os.path.join(backups, 'settings.json.%s.pre-prune' % time.strftime('%Y%m%d_%H%M%S'))
    with open(p, 'rb') as s_in, open(bak, 'wb') as s_out:
        s_out.write(s_in.read())

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix='.gt-')
with os.fdopen(fd, 'w') as fh:
    fh.write(json.dumps(d, indent=2) + '\n')
    fh.flush()
    os.fsync(fh.fileno())
os.replace(tmp, p)

# Say what was wired, by event and by count. The old message printed a bare list
# after the "Restart Claude Code" line, where it read as trailing noise past the
# end of the output -- so an install that wired nothing looked no different from
# one that wired everything.
by_event = {}
for c in changed:
    ev, sc = c.split('/', 1)
    by_event.setdefault(ev, []).append(sc)
print('Registered %d hooks in ~/.claude/settings.json:' % len(changed))
for ev in sorted(by_event):
    print('  %-16s %s' % (ev, ', '.join(sorted(by_event[ev]))))
if removed:
    print('Removed %d hook entr%s this release no longer ships (backup: %s):'
          % (len(removed), 'y' if len(removed) == 1 else 'ies', bak))
    for r in sorted(removed):
        print('  %s' % r)
if unknown:
    print('Unknown hook entr%s pointing into the gt hooks dir, left in place:'
          % ('y' if len(unknown) == 1 else 'ies'))
    for r in sorted(unknown):
        print('  %s' % r)
EOF

# Verify the wiring took, rather than trusting that it did. This is the one place
# the check can run from OUTSIDE the hooks it is checking: at session start the
# SessionStart entries verify themselves, which cannot report the case where there
# are none. Found on 2026-09-10 -- a machine with every file installed, the
# manifest clean, and no SessionStart entries at all.
#
# Scoped to --owner install.sh: the four ENFORCEMENT hooks are wired by vault_init
# against a vault, which may not exist yet when this runs (step 7 wires them when
# it does). Asserting all eleven here would warn on every vault-less install and
# train the reader to ignore the one warning that matters.
#
# `|| true` is not laziness: this whole script runs under `set -e`, and the
# reporting branch ends in a pipeline that exits non-zero BY DESIGN. Without it a
# wiring problem would abort the install at the last step -- turning a warning
# about hooks into a failure to install them.
if [ -f "$SRC/scripts/gt_components.py" ]; then
  if python3 "$SRC/scripts/gt_components.py" wiring "$SRC" --owner install.sh >/dev/null 2>&1; then
    echo "Verified hook wiring → every hook this installer owns is connected"
  else
    echo "⚠ Hook wiring INCOMPLETE after install — these will NEVER RUN:"
    python3 "$SRC/scripts/gt_components.py" wiring "$SRC" --owner install.sh 2>&1 | sed 's/^/    /' || true
  fi
fi

# 7. Wire the VAULT's git repo for per-edit attribution, if it is one.
#
# .git/hooks is not tracked and does not survive a clone, so the hooks ship in a
# tracked .githooks/ and core.hooksPath points at it. That config is per-clone
# local state -- which is exactly why it belongs in the installer rather than in a
# README nobody re-reads on a new machine.
# The default only ever appears in a PROMPT or an instruction, never as something this
# script picks on its own: an installer that invents a directory and claims the global
# vault-config.json without being asked is how people learn not to trust installers.
DEFAULT_VAULT="$HOME/Documents/GoldenThread"

setup_vault() {  # $1 = path to create or connect
  local target="$1" mode
  if [ -d "$target/Projects" ] || [ -f "$target/index.md" ]; then
    mode=connect
  else
    mode=fresh
  fi
  echo ""
  if [ "$mode" = fresh ]; then
    echo "Creating a vault at $target"
    python3 "$SRC/scripts/vault_init.py" fresh --vault "$target" --domain "Personal" \
      >/dev/null 2>&1 || { echo "⚠ could not create the vault at $target"; return 1; }
  else
    echo "Connecting the existing vault at $target"
    python3 "$SRC/scripts/vault_init.py" connect --vault "$target" \
      >/dev/null 2>&1 || { echo "⚠ could not connect $target"; return 1; }
  fi
  python3 "$SRC/scripts/vault_init.py" install-core-rules --vault "$target" >/dev/null 2>&1 \
    && echo "Wired enforcement hooks → ~/.claude/settings.json" \
    || echo "⚠ vault ready, but the enforcement hooks could not be wired"
  echo "Vault ready: $target"
  if [ -f "$target/OPEN-IN-OBSIDIAN.md" ]; then
    echo "Obsidian is optional but recommended — see $target/OPEN-IN-OBSIDIAN.md"
  fi
  return 0
}

# --vault (or $GT_VAULT) was given: do the whole job in one command. Runs before the
# config is read, so the path the user named is the one that gets set up and wired.
if [ -n "$VAULT_ARG" ] && [ "$NO_VAULT" != yes ]; then
  setup_vault "$VAULT_ARG" || true
fi
VAULT_PATH=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null)
# Wire the ENFORCEMENT hooks whenever a vault EXISTS — nothing more is required.
#
# These are owned by vault_init.py, which until 0.12.1 wired them only when a vault was
# CREATED, so an upgrade copied a new hook and never registered it (0.12.0 shipped
# guard_vault_writes.sh inert on every existing machine). 0.12.1 fixed that but put the
# call inside the block gated on the vault being a GIT REPO — so a vault that is not a
# repo still installed with the hooks inert, and the installer then claimed there was
# no vault at all. Git decides whether the ATTRIBUTION hooks can be wired; it has
# nothing to do with an entry in settings.json.
#
# Idempotent: rule files are seeded only when absent, never overwritten, and an entry
# already present is reported as skipped.
wire_enforcement_hooks() {  # $1 = vault path
  local vault="$1" out rc
  [ -f "$SRC/scripts/vault_init.py" ] || return 0
  out=$(python3 "$SRC/scripts/vault_init.py" install-core-rules --vault "$vault" 2>&1) && rc=0 || rc=$?
  if [ "${rc:-1}" -eq 0 ]; then
    if printf '%s' "$out" | grep -q '"action": "updated"'; then
      echo "Wired enforcement hooks → ~/.claude/settings.json"
    else
      echo "Enforcement hooks already wired"
    fi
  else
    echo "⚠ Could not wire the enforcement hooks. Run this, then restart:"
    echo "  python3 \"$SRC/scripts/vault_init.py\" install-core-rules --vault \"$vault\""
  fi
}

if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then
  wire_enforcement_hooks "$VAULT_PATH"
fi

# Ask git, not the filesystem: .git is a FILE for a worktree, a submodule or a
# --separate-git-dir checkout, and `-d .git` skipped all of those silently.
if [ -n "$VAULT_PATH" ] && git -C "$VAULT_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  mkdir -p "$VAULT_PATH/.githooks" "$VAULT_PATH/Projects/golden-thread/tools"
  if [ -d "$SRC/templates/githooks" ]; then
    cp "$SRC/templates/githooks/"* "$VAULT_PATH/.githooks/" 2>/dev/null || true
    chmod +x "$VAULT_PATH/.githooks/"* 2>/dev/null || true
  fi
  # Tools are SEEDED when absent. When present, the vault's copy is compared to
  # the template BY CONTENT, with the same rule gt_components applies to hooks:
  #
  #   identical            -> verified, nothing to do
  #   differs, vault NEWER -> "ahead": a local edit. Reported, never overwritten,
  #                           because clobbering it would repeat the mistake this
  #                           mechanism exists to fix -- an update that silently
  #                           reverts work only present on one machine.
  #   differs, vault OLDER -> "stale": predates the template. Backed up OUTSIDE the
  #                           vault, then replaced.
  #
  # 0.9.6 asked instead whether the file *named* one function (`grep "def X"`),
  # which a broken draft, a commented-out sketch or a renamed helper all satisfy,
  # and left its backup inside the git-tracked tools/ directory for the next
  # `git add -A` to commit. A contract on a symbol is not a contract on behaviour.
  BACKUPS="$HOME/.claude/golden-thread/backups"
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
      state=$(python3 - "$t" "$dest" <<'EOF'
import hashlib, os, sys
t, d = sys.argv[1:3]
h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
if h(t) == h(d):
    print("same")
elif os.path.getmtime(d) > os.path.getmtime(t):
    print("ahead")
else:
    print("stale")
EOF
)
      case "$state" in
        same)  echo "Vault tool verified → $base matches the $VERSION template" ;;
        ahead) echo "⚠ Vault tool AHEAD → $base differs from the $VERSION template and is newer;"
               echo "  left in place. Diff it against $t"
               echo "  and fold the change back into the plugin if it is a fix." ;;
        *)     mkdir -p "$BACKUPS"
               bak="$BACKUPS/$base.$(date +%Y%m%d_%H%M%S)"
               cp "$dest" "$bak"
               cp "$t" "$dest"
               echo "⚠ Vault tool REPLACED → $base predates the $VERSION template."
               echo "  Your previous copy is at $bak -- diff it if you had local edits." ;;
      esac
    done
  fi
  git -C "$VAULT_PATH" config core.hooksPath .githooks 2>/dev/null \
    && echo "Wired vault git attribution → $VAULT_PATH (.githooks)"

fi

if [ -z "$VAULT_PATH" ] || [ ! -d "$VAULT_PATH" ]; then
  # No vault. The four enforcement hooks are wired AGAINST a vault, so an install that
  # stops here leaves them inert — which is the state that produced a false "broken
  # install" report on 2026-09-12. Three ways out, and which one applies depends on
  # who is running this.
  if [ "$NO_VAULT" = yes ]; then
    echo ""
    echo "Installed without a vault (--no-vault). The Core-rule enforcement hooks are"
    echo "present but NOT wired; they are wired against a vault. Run /gt:gt-init, or"
    echo "  ./install.sh --vault <path>"
    echo "when you have one."
  elif [ -t 0 ] && [ -t 1 ]; then
    # A person is watching. Ask; never invent a directory unasked.
    echo ""
    echo "No Golden Thread vault is configured on this machine."
    echo "A vault is a plain folder of markdown — it is where your memory lives, and"
    echo "the Core-rule enforcement hooks are wired against it."
    printf 'Create one at %s? [Y = create / n = skip / or type a path]: ' "$DEFAULT_VAULT"
    read -r ANSWER </dev/tty || ANSWER=""
    case "$ANSWER" in
      ""|y|Y|yes|YES)   CHOSEN="$DEFAULT_VAULT" ;;
      n|N|no|NO)        CHOSEN="" ;;
      *)                CHOSEN="$ANSWER" ;;
    esac
    if [ -n "$CHOSEN" ]; then
      setup_vault "$CHOSEN" || true
    else
      echo "Skipped. Run /gt:gt-init in Claude Code when you are ready; that wires them."
    fi
  else
    # NOT a terminal: an agent, a pipe or CI is running this. Do not guess a path and
    # do not claim the global config. Stop, and say exactly what is needed, so the
    # caller can put the question to the person instead of deciding for them.
    echo ""
    echo "════════════════════════════════════════════════════════════════════════"
    echo "INSTALL INCOMPLETE — a vault is needed to finish"
    echo "════════════════════════════════════════════════════════════════════════"
    echo "The plugin is installed, but the Core-rule enforcement hooks are NOT wired:"
    echo "they are wired against a vault, and this machine has none configured."
    echo ""
    echo "This is not a failure — it needs one decision that is not mine to make:"
    echo "WHERE the vault should live. A vault is a plain folder of markdown files."
    echo ""
    echo "  Already have one?   ./install.sh --vault /path/to/existing-vault"
    echo "  Want a new one?     ./install.sh --vault \"$DEFAULT_VAULT\""
    echo "  Deliberately none?  ./install.sh --no-vault"
    echo ""
    echo "ASK THE USER which they want before re-running. Exit 4 means exactly this."
    echo "════════════════════════════════════════════════════════════════════════"
    exit 4
  fi
fi


# ── Summary ────────────────────────────────────────────────────────────────
#
# LAST, deliberately. This block used to sit before steps 6 and 7, so hook
# registration and vault-tool seeding printed BELOW "Restart Claude Code to load
# the plugins." -- past what reads as the end of the output. The line that tells
# you whether the hooks were wired is the line most worth seeing, and it was the
# one printed after the goodbye.
echo ""
echo "Golden Thread $VERSION installed."
echo ""
# The skill lists are DERIVED from what was just installed, not typed here. A
# hardcoded list is a second copy of the truth, and it goes stale silently: gt 0.10.0
# shipped /gt:gt-watch and this summary did not mention it, so the installer told a
# new user a skill did not exist that did. Same failure as a hardcoded VERSION, and
# the same fix -- read what is on disk.
#
# Reads the installed CACHE rather than the source, so what is listed is what the
# user actually got: with install_demo=no, gt-demo has already been removed from the
# cache by then and drops out of the list for free, with no second condition to keep
# in step.
list_skills() {  # $1 = skills dir, $2 = command prefix
  local d="$1" prefix="$2" name desc
  [ -d "$d" ] || return 0
  for sk in "$d"/*/; do
    name=$(basename "$sk")
    [ -f "$sk/SKILL.md" ] || continue
    # First sentence of the description, trimmed; quotes optional in the frontmatter.
    desc=$(python3 - "$sk/SKILL.md" <<'PYEOF'
import re, sys
LIMIT = 66
try:
    t = open(sys.argv[1], encoding="utf-8", errors="replace").read(4000)
except OSError:
    raise SystemExit
m = re.search(r'^description:\s*(.+?)\s*$', t, re.M)
if not m:
    raise SystemExit
d = m.group(1).strip().strip('"').strip("'")
# First sentence, where a sentence ends in ". " after a word character. Several
# descriptions open with a clause list ("Commands: add, remove, ...") or a colon,
# so cut at the first of sentence-end / " -- " / ": " and take whichever comes first.
for pat in (r'(?<=\w)\.\s', r'\s[-—]{1,2}\s', r':\s'):
    parts = re.split(pat, d, maxsplit=1)
    if len(parts) > 1 and len(parts[0]) >= 20:
        d = parts[0]
        break
d = d.rstrip(' .:,;-—')
# Truncate on a WORD boundary. A mid-word cut reads as corruption rather than
# brevity, and this line is the first thing a new user sees.
if len(d) > LIMIT:
    d = d[:LIMIT].rsplit(' ', 1)[0].rstrip(' .,;:-—') + '…'
print(d)
PYEOF
)
    printf '  %-24s %s\n' "$prefix$name" "$desc"
  done
}

echo "gt skills:"
list_skills "$CACHE/skills" "/gt:"
echo ""
echo "gt-wiki skills:"
list_skills "$WIKI_CACHE/skills" "/gt-wiki:"
echo ""
if [ "$INSTALL_DEMO" = "no" ]; then
echo "Demo not installed (install_demo=no in vault-config.json)."
echo "Set install_demo=yes and re-run install.sh to add /gt:gt-demo."
echo ""
fi
# Measure this machine, so `parallel_max: auto` means THIS machine.
#
# PLACED HERE, AFTER the vault is configured, and that position is the whole point.
# In 0.12.5 this ran as step 6b, BEFORE setup_vault created vault-config.json, so on a
# fresh machine there was no config to write into and the step silently skipped: the
# profile only ever appeared on a machine that already had one. Found 2026-09-12 by
# installing into a throwaway HOME and looking for the value rather than trusting the
# installer's own output, which said nothing either way.
#
# It writes `parallel_profile` only. `parallel_work` and `parallel_max` are the user's
# preferences and are never touched: re-running this installer must not undo a ceiling
# somebody set on purpose.
if [ -f "$HOME/.claude/vault-config.json" ]; then
  python3 "$SRC/scripts/gt_settings.py" detect-machine --write 2>/dev/null \
    | sed 's/^/  /' || true
fi

# Pending vault upgrades. install.sh never ran gt_upgrade, so a release's migrations
# were only ever applied by someone remembering /gt:gt-upgrade -- and one vault sat with
# a step pending from 0.12.0 onward, reported nowhere an installer's reader looks.
# `status` is READ-ONLY; `run` is never called from here, because applying a migration
# is a change to the owner's vault and wants a clean tree and a person's go-ahead.
# Re-read the config: an interactive install may have created the vault after
# VAULT_PATH was first read. A failed check is reported, never fatal.
report_vault_upgrades() {
  local vault out rc=0
  vault=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null) || vault=""
  [ -n "$vault" ] && [ -d "$vault" ] || return 0
  [ -f "$SRC/scripts/gt_upgrade.py" ] || { echo "Vault upgrades: check could not run (gt_upgrade.py not shipped)"; return 0; }
  out=$(python3 "$SRC/scripts/gt_upgrade.py" status --vault "$vault" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "⚠ Vault upgrades: the check could not run (exit $rc) — run /gt:gt-upgrade to look"
  elif printf '%s\n' "$out" | grep -q 'pending step(s):'; then
    echo "⚠ Vault upgrades pending for $vault:"
    # From the "N pending step(s):" line through the step list; the rehearsal hint is
    # dropped because the skill is the supported way in.
    printf '%s\n' "$out" | sed -n '/pending step(s):/,$p' | grep -v '^Rehearse:' \
      | sed '/^[[:space:]]*$/d' | sed 's/^/  /' || true
    echo "  Run /gt:gt-upgrade to apply"
  elif printf '%s\n' "$out" | grep -q 'nothing pending'; then
    echo "Vault upgrades: none pending"
  else
    echo "⚠ Vault upgrades: the check could not run (unrecognised output) — run /gt:gt-upgrade to look"
  fi
  echo ""
}
report_vault_upgrades || true

echo "Restart Claude Code to load the plugins."
