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
# Only install.sh's OWN entries are wired here. The three enforcement hooks are
# declared in the same list but owned by vault_init.py install-core-rules, which
# runs against a vault -- this script may be run before one exists.
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
EOF

# Verify the wiring took, rather than trusting that it did. This is the one place
# the check can run from OUTSIDE the hooks it is checking: at session start the
# SessionStart entries verify themselves, which cannot report the case where there
# are none. Found on 2026-09-10 -- a machine with every file installed, the
# manifest clean, and no SessionStart entries at all.
#
# Scoped to --owner install.sh: the three ENFORCEMENT hooks are declared in the
# same list but wired by vault_init.py against a vault, which may not exist yet
# when this runs. Asserting all nine here would warn on every first install and
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
VAULT_PATH=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null)
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
echo "Restart Claude Code to load the plugins."
