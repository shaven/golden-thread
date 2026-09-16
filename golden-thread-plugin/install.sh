#!/usr/bin/env bash
set -euo pipefail
# No bytecode from any python this installer runs (0.15.0): __pycache__ left in the plugin
# cache and marketplace varied run to run, and running a vault tool dirtied the vault.
export PYTHONDONTWRITEBYTECODE=1

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

# A plugin's name is what Claude Code keys it by (cache dir, marketplace entry,
# "<name>@golden-thread-plugin"); the directory name is only the fallback.
plugin_name_of() {  # $1 = version dir
  python3 -c "import json,sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
    n = d.get('name')
except (OSError, ValueError, AttributeError):
    n = None
print(n or '')" "$1/.claude-plugin/plugin.json" 2>/dev/null || true
}

# WHICH plugins this tree ships (0.14.0). Until 0.13.0 this installer named gt and
# gt-wiki by hand in ~22 places, so a third plugin beside them would have been silently
# skipped. The rule is the one dev/plugins.py implements -- every top-level
# <dir>/<N.N.N>/.claude-plugin/plugin.json, newest per dir, sorted by dir name -- and
# tests/test_install_plugins.py asserts the two agree on the real repo. It is repeated
# here rather than called because install.sh must stand alone: it runs from gt-src and
# from zip packages, neither of which is guaranteed to carry dev/.
#
# Output: "<dir>\t<version>\t<name>" per plugin.
discover_plugins() {  # $1 = plugin root
  local root="$1" d dir ver name
  for d in "$root"/*/; do
    [ -d "$d" ] || continue
    dir=$(basename "$d")
    ver=$(latest_version "$root/$dir")
    [ -n "$ver" ] || continue
    name=$(plugin_name_of "$root/$dir/$ver")
    printf '%s\t%s\t%s\n' "$dir" "$ver" "${name:-$dir}"
  done | LC_ALL=C sort -t "$(printf '\t')" -k1,1
}

# MODULES (0.14.0). A discovered plugin whose version dir carries module.json is a module:
# optional, chosen with --with / --without, the choice kept in
# ~/.claude/golden-thread/install-choices.json. gt itself is never a module. The
# effective state (flag this run > recorded choice > the module's default) is asked of
# the gt being installed (`gt_components.py module-states`), and computed here the same
# way only when that gt predates the subcommand (a rollback). A module whose requires_gt
# does not admit the gt being installed is skipped -- treated as off for this run, its
# recorded choice left alone.
#
# One helper, several subcommands, so the reading of module.json is written once:
#   scan <root> <gt-version> <dir> <ver> <name> ...   -> JSON list of modules on stdout
#   resolve <in.json> <out.json> <home> <states-json|-> [with:N|without:N ...]
#   record <home> <name> on|off                        (fallback for record-choice)
#   list <json> <gt-version> | summary <json> | onfiles <json> | names <json>
#   remove <json> <home> <src> <cache-root> <marketplace> <installed> <settings> <hooks>
MODPY=$(cat <<'PYEOF'
import json, os, re, shutil, subprocess, sys, tempfile, time

NAME = re.compile(r"^[a-z][a-z0-9-]*$")
MARKET = "golden-thread-plugin"


def vkey(v):
    return tuple(int(x) for x in v.split("."))


def admits(spec, ver):
    """True / False, or None when the spec cannot be read."""
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        have = vkey(ver)
    except ValueError:
        return None
    for clause in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+\.\d+\.\d+)\s*", clause)
        if not m:
            return None
        op, want = m.group(1), vkey(m.group(2))
        if not {">=": have >= want, "<=": have <= want, "==": have == want,
                ">": have > want, "<": have < want}[op]:
            return False
    return True


def choices_path(home):
    return os.path.join(home, ".claude", "golden-thread", "install-choices.json")


def read_choices(home):
    try:
        with open(choices_path(home), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    c = d.get("choices") if isinstance(d, dict) else None
    return {k: v for k, v in c.items() if v in ("on", "off")} if isinstance(c, dict) else {}


def atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".gt-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def module_requires(vdir):
    """A version dir's requires_gt, or None when it carries no readable module.json."""
    try:
        m = load(os.path.join(vdir, "module.json"))
    except (OSError, ValueError):
        return None
    return (m.get("requires_gt") or "") if isinstance(m, dict) else None


# What each gt release commit carried beside it: the newest release of every other
# plugin in the tree at the commit that added golden-thread/<gt>/ (from git history, checked
# against it by tests/test_install_plugins.py). A rollback installs exactly this, so
# ./install.sh 0.12.8 from a newer tree ends where the 0.12.8 installer did. Releases
# before 0.14.0 predate module.json, so nothing else could say gt-wiki 0.1.2 belongs with
# 0.12.8 -- without the table a rollback removed gt-wiki altogether (0.15.0).
SHIPPED_WITH = {
    "0.12.2": {"gt-wiki": "0.1.2"}, "0.12.3": {"gt-wiki": "0.1.2"},
    "0.12.4": {"gt-wiki": "0.1.2"}, "0.12.5": {"gt-wiki": "0.1.2"},
    "0.12.6": {"gt-wiki": "0.1.2"}, "0.12.7": {"gt-wiki": "0.1.2"},
    "0.12.8": {"gt-wiki": "0.1.2"}, "0.12.9": {"gt-wiki": "0.1.2"},
    "0.13.0": {"gt-wiki": "0.1.3"},
    "0.14.0": {"gt-wiki": "0.2.0", "gt-demo": "0.14.0"},
}
FIRST_MODULE_GT = (0, 14, 0)   # the first gt whose installer read module.json


def plugin_name(vdir):
    try:
        return load(os.path.join(vdir, ".claude-plugin", "plugin.json")).get("name")
    except (OSError, ValueError, AttributeError):
        return None


def cmd_pick(a):
    """The version of one plugin to install beside gt <gtver>: its newest release, unless
    that is a module whose requires_gt refuses gtver (a pinned gt: a rollback). Then, in
    order: the release SHIPPED_WITH records for that gt; the newest OLDER release that is a
    module admitting it; for a gt from before modules, the newest older release with no
    module.json (a plain plugin, which the installer of that gt installed unconditionally).
    Prints "<version>\t<newest>\t<newest's requires_gt>"; the first two differ only when an
    older release was chosen. Nothing fits -> the newest, which scan then skips."""
    root, d, newest, gtver = a
    base = os.path.join(root, d)
    spec = module_requires(os.path.join(base, newest))
    if spec is None or admits(spec, gtver) is True:
        print("%s\t%s\t" % (newest, newest))
        return 0
    older = []
    for v in os.listdir(base):
        if re.fullmatch(r"\d+\.\d+\.\d+", v) and vkey(v) < vkey(newest) \
                and os.path.isfile(os.path.join(base, v, ".claude-plugin", "plugin.json")):
            older.append(v)
    older.sort(key=vkey, reverse=True)
    shipped = SHIPPED_WITH.get(gtver, {}).get(plugin_name(os.path.join(base, newest)) or "")
    if shipped in older:
        print("%s\t%s\t%s" % (shipped, newest, spec))
        return 0
    for v in older:
        s = module_requires(os.path.join(base, v))
        if s is not None and admits(s, gtver) is True:
            print("%s\t%s\t%s" % (v, newest, spec))
            return 0
    try:
        pre_modules = vkey(gtver) < FIRST_MODULE_GT
    except ValueError:
        pre_modules = False
    for v in older if pre_modules else []:
        if module_requires(os.path.join(base, v)) is None:
            print("%s\t%s\t%s" % (v, newest, spec))
            return 0
    print("%s\t%s\t" % (newest, newest))
    return 0


def cmd_moved(a):
    """Skills the previous gt had that the gt being installed no longer ships, and the
    plugin that now provides each: "Moved: /gt:X -> /<plugin>:X" (0.15.0). For an upgrader,
    /gt:gt-watch otherwise just stopped resolving, and nothing said where it went.
    Args: <old gt skills dir> <new gt skills dir> <states.tsv> then <dir> <ver> <name> ...
    of every other plugin discovered, on or off."""
    old, new, states, rest = a[0], a[1], a[2], a[3:]

    def skills(p):
        try:
            return {s for s in os.listdir(p) if os.path.isfile(os.path.join(p, s, "SKILL.md"))}
        except OSError:
            return set()
    gone = sorted(skills(old) - skills(new))
    if not gone:
        return 0
    off = {}
    try:
        for line in open(states, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 4 and f[1] == "off":
                off[f[3]] = f[0]
    except OSError:
        pass
    for s in gone:
        for d, ver, name in zip(rest[0::3], rest[1::3], rest[2::3]):
            if os.path.isfile(os.path.join(d, ver, "skills", s, "SKILL.md")):
                tail = (" (module %s is off: ./install.sh --with %s)" % (off[name], off[name])
                        if name in off else "")
                print("Moved: /gt:%s → /%s:%s%s" % (s, name, s, tail))
                break
    return 0


def cmd_moved_annotate(a):
    """Add the "(module X is off: ...)" tail to Moved lines from the FINAL module states.
    Moved lines are computed before the machine migrations (the old gt cache is still on
    disk then) with no states, so a migration that turns a module on -- farm for upgraders --
    cannot leave a notice saying it is off. Args: <states.tsv>; lines on stdin."""
    off = {}
    try:
        for line in open(a[0], encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 4 and f[1] == "off":
                off[f[3]] = f[0]
    except OSError:
        pass
    for line in sys.stdin.read().splitlines():
        m = re.match(r"^Moved: /gt:\S+ → /([^:]+):\S+$", line)
        if m and m.group(1) in off:
            line += " (module %s is off: ./install.sh --with %s)" % (off[m.group(1)],
                                                                      off[m.group(1)])
        print(line)
    return 0


def cmd_newer_owned(a):
    """What gt releases NEWER than the one being installed put in the hooks dir or wired,
    for a rollback (0.15.0). Installing an older gt from a newer tree left the newer
    release guard_protected_paths.sh wired and its files in the hooks dir, which the older
    gt calls drift every session. Printed as JSON: {"files": {name: [sha256, ...]},
    "registrations": [script, ...]} -- a file counts as ours only when its bytes match a
    copy some newer release (or any module release) in the tree ships. Read, not run:
    HOOK_REGISTRATIONS and HOOK_DIR_SCRIPTS are literals in each gt_components.py.
    Args: <plugin root> <core dir> <version being installed>"""
    import ast, hashlib
    root, core, ver = a
    files, regs = {}, set()

    def add(path, name=None):
        try:
            h = hashlib.sha256(open(path, "rb").read()).hexdigest()
        except OSError:
            return
        files.setdefault(name or os.path.basename(path), set()).add(h)

    base = os.path.join(root, core)
    for v in os.listdir(base):
        if not re.fullmatch(r"\d+\.\d+\.\d+", v) or vkey(v) <= vkey(ver):
            continue
        vd = os.path.join(base, v)
        hd = os.path.join(vd, "hooks")
        for n in (os.listdir(hd) if os.path.isdir(hd) else []):
            if os.path.isfile(os.path.join(hd, n)) and not n.endswith(".pyc"):
                add(os.path.join(hd, n))
        try:
            tree = ast.parse(open(os.path.join(vd, "scripts", "gt_components.py"),
                                  encoding="utf-8").read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            name = getattr(node.targets[0], "id", "")
            if name not in ("HOOK_REGISTRATIONS", "HOOK_DIR_SCRIPTS"):
                continue
            try:
                val = ast.literal_eval(node.value)
            except ValueError:
                continue
            if name == "HOOK_DIR_SCRIPTS":
                for s in val:
                    add(os.path.join(vd, "scripts", s))
            else:
                for r in val:
                    if isinstance(r, dict) and r.get("script"):
                        regs.add(r["script"])
                        add(os.path.join(vd, "hooks", r["script"]))
    for d in os.listdir(root):
        if not d.startswith("golden-thread-") or not os.path.isdir(os.path.join(root, d)):
            continue
        for v in os.listdir(os.path.join(root, d)):
            vd = os.path.join(root, d, v)
            try:
                m = load(os.path.join(vd, "module.json"))
            except (OSError, ValueError):
                continue
            for h in m.get("hooks") or []:
                if isinstance(h, dict) and h.get("script"):
                    regs.add(h["script"])
                    add(os.path.join(vd, "hooks", h["script"]))
                    add(os.path.join(vd, "scripts", h["script"]))
            for s in m.get("hookdir_scripts") or []:
                add(os.path.join(vd, s))
    print(json.dumps({"files": {k: sorted(v) for k, v in sorted(files.items())},
                      "registrations": sorted(regs)}))
    return 0


def cmd_scan(a):
    root, gtver, rest = a[0], a[1], a[2:]
    mods, seen = [], {}
    for d, ver, plugin in zip(rest[0::3], rest[1::3], rest[2::3]):
        vdir = os.path.join(root, d, ver)
        mj = os.path.join(vdir, "module.json")
        if not os.path.isfile(mj):
            continue
        try:
            m = load(mj)
            assert isinstance(m, dict), "not a JSON object"
        except (OSError, ValueError, AssertionError) as exc:
            print("✗ %s is unreadable: %s" % (mj, exc), file=sys.stderr)
            return 3
        name = m.get("name")
        if not isinstance(name, str) or not NAME.match(name) or name == "gt":
            print("✗ %s: unusable module name %r" % (mj, name), file=sys.stderr)
            return 3
        if m.get("plugin") != plugin or m.get("version") != ver:
            print("✗ %s: plugin/version (%r %r) disagree with plugin.json and the directory "
                  "(%s %s)" % (mj, m.get("plugin"), m.get("version"), plugin, ver),
                  file=sys.stderr)
            return 3
        if name in seen:
            print("✗ two plugins declare module '%s': %s and %s" % (name, seen[name], d),
                  file=sys.stderr)
            return 3
        seen[name] = d
        hook_scripts = []
        for h in m.get("hooks") or []:
            s = h.get("script") if isinstance(h, dict) else None
            if not s:
                continue
            src = next((p for p in (os.path.join(vdir, "hooks", s), os.path.join(vdir, "scripts", s))
                        if os.path.isfile(p)), None)
            hook_scripts.append({"event": h.get("event"), "script": s, "src": src})
        mods.append({
            "name": name, "plugin": plugin, "dir": d, "version": ver,
            "default": "on" if m.get("default") == "on" else "off",
            "requires_gt": m.get("requires_gt") or "",
            "admits": admits(m.get("requires_gt"), gtver),
            "hookdir_scripts": [os.path.join(vdir, "scripts", s)
                                for s in (m.get("hookdir_scripts") or [])],
            "hook_scripts": hook_scripts,
        })
    print(json.dumps(mods, indent=1))
    return 0


def cmd_resolve(a):
    src, out, home, states_raw, flags = a[0], a[1], a[2], a[3], a[4:]
    mods = load(src)
    flag = {}
    for f in flags:
        kind, _, n = f.partition(":")
        flag[n] = "on" if kind == "with" else "off"
    try:
        states = json.loads(states_raw) if states_raw not in ("", "-") else {}
        if not isinstance(states, dict):
            states = {}
    except ValueError:
        states = {}
    recorded = read_choices(home)
    for m in mods:
        n = m["name"]
        why = "flag" if n in flag else "recorded" if n in recorded else "default"
        state = flag.get(n) or recorded.get(n) or m["default"]
        m["reason"] = ""
        got = states.get(n)
        # module-states judges each module's NEWEST release. When pick chose an older one
        # for a pinned gt, a "requires gt" refusal is about a release not being installed,
        # so it is not this module's state -- the precedence above stands.
        if isinstance(got, dict) and got.get("version") not in (None, m["version"]) \
                and str(got.get("reason", "")).startswith("requires gt"):
            got = None
        if isinstance(got, dict):              # module-states --detail
            if got.get("state") in ("on", "off"):
                state = got["state"]
            if str(got.get("reason", "")).startswith("invalid"):
                why, m["reason"] = "invalid", got["reason"]
        elif got in ("on", "off"):
            state = got
        if m["admits"] is not True:
            state, why = "off", "requires"
        elif why == "invalid":
            state = "off"
        m["state"], m["why"] = state, why
    atomic(out, mods)
    for m in mods:
        print("%s\t%s\t%s\t%s" % (m["name"], m["state"], m["why"], m["plugin"]))
    return 0


def cmd_record(a):
    home, name, state = a
    p = choices_path(home)
    try:
        d = load(p)
    except FileNotFoundError:
        d = {}
    if not isinstance(d, dict) or not isinstance(d.get("choices", {}), dict):
        print("✗ %s is not in the expected shape; choice not recorded" % p, file=sys.stderr)
        return 1
    d.setdefault("version", 1)
    d.setdefault("choices", {})[name] = state
    atomic(p, d)
    return 0


def why_text(m, gtver=None):
    return {"flag": "--with this run" if m["state"] == "on" else "--without this run",
            "recorded": "your recorded choice",
            "default": "module default",
            "requires": "skipped: needs gt %s%s" % (m["requires_gt"],
                                                    ", installing %s" % gtver if gtver else ""),
            "invalid": "skipped: %s" % m.get("reason", "invalid module.json"),
            }[m["why"]]


def cmd_list(a):
    mods, gtver = load(a[0]), a[1]
    if not mods:
        print("No modules in this tree.")
        return 0
    print("Modules (for gt %s):" % gtver)
    for m in mods:
        print("  %-10s %-12s %-8s %-4s %s" % (m["name"], m["plugin"], m["version"], m["state"],
                                              why_text(m, gtver)))
    print("Change with: ./install.sh --with NAME | --without NAME  (the choice is remembered)")
    return 0


def cmd_summary(a):
    mods = load(a[0])
    parts = []
    for m in mods:
        s = "%s %s" % (m["name"], m["state"])
        if m["why"] == "requires":
            s += " (needs gt %s)" % m["requires_gt"]
        elif m["why"] == "invalid":
            s += " (invalid module.json)"
        elif m["why"] in ("flag", "recorded") and m["state"] != m["default"]:
            s += " (by your choice)"
        elif m["why"] == "default" and m["state"] == "off":
            s += " (off by default)"
        parts.append(s)
    if parts:
        print("Modules: " + ", ".join(parts))
    return 0


def cmd_onfiles(a):
    for m in load(a[0]):
        if m["state"] != "on":
            continue
        for p in m["hookdir_scripts"] + [h["src"] for h in m["hook_scripts"] if h["src"]]:
            if os.path.isfile(p):
                print(p)
    return 0


def cmd_names(a):
    print(", ".join(m["name"] for m in load(a[0])))
    return 0


def cmd_remove(a):
    modjson, home, src, cache_root, marketplace, installed, settings, hooks = a
    mods = load(modjson)
    off = [m for m in mods if m["state"] == "off"]
    if not off:
        return 0
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backups = os.path.join(home, ".claude", "golden-thread", "backups")
    lines = {m["name"]: [] for m in off}

    def backup(path):
        os.makedirs(backups, exist_ok=True)
        bak = os.path.join(backups, "%s.%s.module-off" % (os.path.basename(path), stamp))
        if not os.path.exists(bak):
            shutil.copy2(path, bak)

    # Plugin copies (cache every version, marketplace dir). These are copies of the source
    # tree install.sh would lay down again with --with; the JSON registries and hook files
    # below are what get backed up.
    for m in off:
        base = os.path.join(cache_root, m["plugin"])
        if os.path.isdir(base):
            for v in sorted(os.listdir(base)):
                shutil.rmtree(os.path.join(base, v), ignore_errors=True)
                lines[m["name"]].append("removed cache %s" % os.path.join(base, v))
            shutil.rmtree(base, ignore_errors=True)
        mdir = os.path.join(marketplace, "plugins", m["plugin"])
        if os.path.isdir(mdir):
            shutil.rmtree(mdir, ignore_errors=True)
            lines[m["name"]].append("removed marketplace dir %s" % mdir)

    keys = {"%s@%s" % (m["plugin"], MARKET): m["name"] for m in off}
    mj = os.path.join(marketplace, ".claude-plugin", "marketplace.json")
    for path, what in ((mj, "marketplace"), (installed, "installed"), (settings, "settings")):
        if not os.path.isfile(path):
            continue
        try:
            d = load(path)
        except (OSError, ValueError) as exc:
            print("⚠ %s unreadable (%s) — module entries not removed from it" % (path, exc))
            continue
        changed = False
        if what == "marketplace" and isinstance(d.get("plugins"), list):
            plugins = {"%s@%s" % (m["plugin"], MARKET): m["name"] for m in off}
            keep = []
            for e in d["plugins"]:
                k = "%s@%s" % (e.get("name"), MARKET) if isinstance(e, dict) else None
                if k in plugins:
                    lines[plugins[k]].append("removed marketplace entry %s" % e.get("name"))
                    changed = True
                else:
                    keep.append(e)
            d["plugins"] = keep
        elif what == "installed" and isinstance(d.get("plugins"), dict):
            for k, n in keys.items():
                if k in d["plugins"]:
                    del d["plugins"][k]
                    lines[n].append("removed installed_plugins.json entry %s" % k)
                    changed = True
        elif what == "settings" and isinstance(d.get("enabledPlugins"), dict):
            for k, n in keys.items():
                if k in d["enabledPlugins"]:
                    del d["enabledPlugins"][k]
                    lines[n].append("removed settings.json enabledPlugins %s" % k)
                    changed = True
        if changed:
            backup(path)
            atomic(path, d)

    # Hook-dir files the off module installed, unless gt or an on module ships that name.
    shipped = {"gt_paths.py"}
    if os.path.isdir(os.path.join(src, "hooks")):
        shipped |= set(os.listdir(os.path.join(src, "hooks")))
    try:
        shipped |= set(subprocess.check_output(
            ["python3", os.path.join(src, "scripts", "gt_components.py"), "hookdir-scripts",
             "--home", home],
            text=True, stderr=subprocess.DEVNULL).split())
    except (OSError, subprocess.SubprocessError):
        pass
    for m in mods:
        if m["state"] == "on":
            shipped |= {os.path.basename(p) for p in m["hookdir_scripts"]}
            shipped |= {h["script"] for h in m["hook_scripts"]}
    hook_bak = os.path.join(backups, "hooks-module-off.%s" % stamp)
    for m in off:
        names = {os.path.basename(p) for p in m["hookdir_scripts"]} | {h["script"] for h in m["hook_scripts"]}
        for n in sorted(names - shipped):
            p = os.path.join(hooks, n)
            if os.sep in n or not os.path.isfile(p):
                continue
            os.makedirs(hook_bak, exist_ok=True)
            shutil.copy2(p, os.path.join(hook_bak, n))
            os.remove(p)
            lines[m["name"]].append("removed hooks-dir file %s (backup: %s)" % (n, hook_bak))

    # Crontab lines a module installed (0.15.0: gt_watch.py install-cron). The convention a
    # module follows is to end its line with "# <plugin>" (gt-watch writes "# gt-watch").
    # A line is removed only when it ends with exactly that tag AND runs something under
    # THIS home's ~/.claude/ -- the crontab is per user, not per HOME, so a line for another
    # home's install (or a throwaway test home's neighbour: the real one) is never ours.
    # Every other line is written back unchanged, and the old crontab is backed up first.
    # Only asked when this run removed something of the module (it was installed here), and
    # skipped when the gt being installed still ships one of the module's scripts itself (a
    # rollback to a gt from before the module existed still runs the line). No crontab
    # binary, or `crontab -l` failing (no crontab at all), means nothing to remove.
    have_cron = shutil.which("crontab") is not None
    mine = os.path.join(os.path.realpath(home), ".claude") + os.sep
    for m in off if have_cron else []:
        if not lines[m["name"]]:
            continue
        names = {os.path.basename(p) for p in m["hookdir_scripts"]} | {h["script"] for h in m["hook_scripts"]}
        if any(os.path.isfile(os.path.join(src, "scripts", n)) for n in names):
            continue
        tag = "# %s" % m["plugin"]
        try:
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if cur.returncode != 0:
            continue
        old = cur.stdout.splitlines()
        keep = [l for l in old if not (l.rstrip().endswith(tag)
                                       and (mine in l or os.path.join(home, ".claude") + os.sep in l))]
        if len(keep) == len(old):
            continue
        os.makedirs(backups, exist_ok=True)
        bak = os.path.join(backups, "crontab.%s.module-off" % stamp)
        with open(bak, "w", encoding="utf-8") as fh:
            fh.write(cur.stdout)
        body = "\n".join(keep) + "\n" if keep else ""
        try:
            w = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True, timeout=30)
            ok = w.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            lines[m["name"]].append("removed %d crontab line(s) tagged '%s' (backup: %s)"
                                    % (len(old) - len(keep), tag, bak))
            # Kept so turning the module back on puts exactly these lines back (0.15.0):
            # until then --with watch reinstalled the module and never its cron fetch.
            gone = [l for l in old if l not in keep]
            state = cron_state_path(home, m["name"])
            try:
                prev = open(state, encoding="utf-8").read().splitlines()
            except OSError:
                prev = []
            os.makedirs(os.path.dirname(state), exist_ok=True)
            fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("".join(l + "\n" for l in prev + [g for g in gone if g not in prev]))
            os.chmod(state, 0o600)
        else:
            print("⚠ crontab refused the update — the %s line(s) tagged '%s' are still there"
                  % (m["name"], tag))

    for m in off:
        if lines[m["name"]]:
            print("Module %s is off (%s) — removing what an earlier install left:"
                  % (m["name"], why_text(m)))
            for l in lines[m["name"]]:
                print("  %s" % l)
    return 0


def cron_state_path(home, name):
    return os.path.join(home, ".claude", "golden-thread", "module-cron", "%s.lines" % name)


def cmd_restore_cron(a):
    """Put back the crontab lines `remove` took out of a module that is ON again.

    Each line is re-added verbatim unless `crontab -l` already has it; every other line is
    written back unchanged, the old crontab backed up first. The home rule is the removal's:
    only a line running something under THIS home's ~/.claude/ is ever written. The state
    file is deleted once its lines are all present, and kept (with a warning) when crontab
    refuses, so the next install tries again."""
    modjson, home = a
    if shutil.which("crontab") is None:
        return 0
    mine = (os.path.join(os.path.realpath(home), ".claude") + os.sep,
            os.path.join(home, ".claude") + os.sep)
    for m in load(modjson):
        state = cron_state_path(home, m["name"])
        if m["state"] != "on" or not os.path.isfile(state):
            continue
        try:
            want = [l for l in open(state, encoding="utf-8").read().splitlines()
                    if l.strip() and any(p in l for p in mine)]
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        old = cur.stdout.splitlines() if cur.returncode == 0 else []
        add = [l for l in want if l not in old]
        if add:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backups = os.path.join(home, ".claude", "golden-thread", "backups")
            os.makedirs(backups, exist_ok=True)
            bak = os.path.join(backups, "crontab.%s.module-on" % stamp)
            with open(bak, "w", encoding="utf-8") as fh:
                fh.write(cur.stdout if cur.returncode == 0 else "")
            try:
                w = subprocess.run(["crontab", "-"], input="\n".join(old + add) + "\n",
                                   capture_output=True, text=True, timeout=30)
                ok = w.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok:
                print("⚠ crontab refused the update — module %s's %d line(s) were not put back "
                      "(kept in %s for the next install)" % (m["name"], len(add), state))
                continue
            print("Module %s is on — restored %d crontab line(s) an earlier --without removed "
                  "(backup: %s)" % (m["name"], len(add), bak))
        os.remove(state)
    return 0


CMDS = {"scan": cmd_scan, "resolve": cmd_resolve, "record": cmd_record, "list": cmd_list,
        "summary": cmd_summary, "onfiles": cmd_onfiles, "names": cmd_names,
        "remove": cmd_remove, "pick": cmd_pick, "restore-cron": cmd_restore_cron,
        "moved": cmd_moved, "moved-annotate": cmd_moved_annotate,
        "newer-owned": cmd_newer_owned}
sys.exit(CMDS[sys.argv[1]](sys.argv[2:]))
PYEOF
)
modpy() { python3 -c "$MODPY" "$@"; }

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
LIST_PLUGINS=no
LIST_MODULES=no
MODULE_FLAGS=()          # "with:NAME" / "without:NAME", in the order given
ORIG_ARGS=("$@")         # kept for the re-run after a migration changes a module choice
POSITIONAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force-manifest-mismatch) FORCE_MANIFEST=yes; shift ;;
    --list-plugins) LIST_PLUGINS=yes; shift ;;
    --list-modules) LIST_MODULES=yes; shift ;;
    --with|--without)
      if [ -z "${2:-}" ] || [ "${2#-}" != "$2" ]; then
        echo "✗ $1 needs a module name (see ./install.sh --list-modules)"; exit 1
      fi
      MODULE_FLAGS+=("${1#--}:$2"); shift 2 ;;
    --with=*)    MODULE_FLAGS+=("with:${1#--with=}"); shift ;;
    --without=*) MODULE_FLAGS+=("without:${1#--without=}"); shift ;;
    --vault)    VAULT_ARG="${2:-}"; shift 2 || true ;;
    --vault=*)  VAULT_ARG="${1#--vault=}"; shift ;;
    --no-vault) NO_VAULT=yes; shift ;;
    -h|--help)
      cat <<'USAGE'
install.sh — install the Golden Thread Claude Code plugins (gt and every plugin
             shipped beside it, e.g. gt-wiki)

  ./install.sh                        install the newest release
  ./install.sh 0.12.1                 install a specific gt release (deliberate rollback);
                                      the other plugins install their newest
  ./install.sh --vault <path>         install AND create or connect that vault
  ./install.sh --force-manifest-mismatch
                                      install even when shipped files disagree with
                                      MANIFEST.json (see below)
  ./install.sh --no-vault             install the plugin only, on purpose
  ./install.sh --list-plugins         print "<dir> <version> <name>" for every plugin
                                      this tree ships, install nothing
  ./install.sh --with NAME            install module NAME (repeatable); remembered
  ./install.sh --without NAME         remove module NAME and keep it off (repeatable);
                                      remembered in ~/.claude/golden-thread/install-choices.json
  ./install.sh --list-modules         print each module, its state and why, install nothing
  ./install.sh --help                 this text

Modules are the optional plugins beside gt (wiki, demo, watch, report-card, farm, flow). State is, in
order: --with/--without in this run, your recorded choice, the module's default. gt
itself is not a module and cannot be removed this way.

A vault is a plain folder of markdown where your memory lives. The Core-rule
enforcement hooks are wired AGAINST a vault, so an install with no vault leaves them
present but inert. --vault finishes the job in one command.

With no vault and no --vault:
  * at a terminal, you are asked where the vault should go;
  * otherwise (an agent, a pipe, CI) the install stops with exit 4 and says what it
    needs, rather than inventing a directory and claiming ~/.claude/vault-config.json.

If the release ships machine migrations, they run after the plugin files and hooks are
in place; a failed one stops the install with exit 7 (nothing is rolled back).

Environment: GT_VAULT (same as --vault), GT_VERSION (same as the version argument).
USAGE
      exit 0 ;;
    -*) echo "unknown option: $1"; echo "try: install.sh [version] [--vault <path>] [--no-vault] [--with|--without NAME] [--list-modules]"; exit 1 ;;
    *)  POSITIONAL="$1"; shift ;;
  esac
done
if [ "$LIST_PLUGINS" = yes ]; then
  discover_plugins "$SCRIPT_DIR" | tr '\t' ' '
  exit 0
fi

REQUESTED="${POSITIONAL:-${GT_VERSION:-}}"
CORE_DIR="golden-thread"
VERSION="${REQUESTED:-$(latest_version "$SCRIPT_DIR/$CORE_DIR")}"

# The plugin set: gt (the core, always index 0, at the pinned version when one was asked
# for) followed by every other discovered plugin at its newest. Parallel arrays rather
# than an associative one: macOS still ships bash 3.2.
#
# Each other plugin installs its newest release that ADMITS this gt (0.15.0): a module
# whose newest release's requires_gt refuses $VERSION falls back to its newest older
# release that accepts it. Only a pinned gt can meet that -- the tree's newest gt is what
# every module's newest release is built for -- and without it a rollback skipped gt-wiki
# and gt-demo although the tree still carried the releases that work with that gt.
PLUGIN_DIRS=("$CORE_DIR"); PLUGIN_VERS=("$VERSION"); PLUGIN_NAMES=("gt")
PICK_NOTES=""
while IFS="$(printf '\t')" read -r _dir _ver _name; do
  [ -n "$_dir" ] && [ "$_dir" != "$CORE_DIR" ] || continue
  IFS="$(printf '\t')" read -r _pick _newest _spec < <(modpy pick "$SCRIPT_DIR" "$_dir" "$_ver" "$VERSION")
  if [ -n "$_pick" ] && [ "$_pick" != "$_ver" ]; then
    PICK_NOTES="${PICK_NOTES}Chose $_name $_pick, not the newest $_ver: $_ver needs gt $_spec, and gt $VERSION is being installed
"
    _ver="$_pick"
  fi
  PLUGIN_DIRS+=("$_dir"); PLUGIN_VERS+=("$_ver"); PLUGIN_NAMES+=("$_name")
done < <(discover_plugins "$SCRIPT_DIR")
PLUGIN_COUNT=${#PLUGIN_DIRS[@]}

i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  root="$SCRIPT_DIR/${PLUGIN_DIRS[$i]}"; ver="${PLUGIN_VERS[$i]}"; label="${PLUGIN_NAMES[$i]}"
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
  name=$(plugin_name_of "$root/$ver"); name="${name:-${PLUGIN_DIRS[$i]}}"
  # The name becomes a path segment under ~/.claude/plugins, including one that is
  # rm -rf'd when superseded, so it is held to a plain identifier.
  if ! [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "✗ Plugin in $root/$ver has an unusable name '$name' in plugin.json."
    exit 1
  fi
  j=0
  while [ "$j" -lt "$i" ]; do
    if [ "${PLUGIN_NAMES[$j]}" = "$name" ]; then
      echo "✗ Two plugin directories declare the name '$name': ${PLUGIN_DIRS[$j]} and ${PLUGIN_DIRS[$i]}."
      exit 1
    fi
    j=$((j + 1))
  done
  PLUGIN_NAMES[$i]="$name"
  i=$((i + 1))
done

MARKET_NAME="golden-thread-plugin"
PLUGIN_KEY="${PLUGIN_NAMES[0]}@$MARKET_NAME"
SRC="$SCRIPT_DIR/$CORE_DIR/$VERSION"
CACHE_ROOT="$HOME/.claude/plugins/cache/$MARKET_NAME"
CACHE="$CACHE_ROOT/${PLUGIN_NAMES[0]}/$VERSION"
MARKETPLACE="$HOME/.claude/plugins/marketplaces/$MARKET_NAME"
SETTINGS="$HOME/.claude/settings.json"
INSTALLED="$HOME/.claude/plugins/installed_plugins.json"
KNOWN="$HOME/.claude/plugins/known_marketplaces.json"

# The gt this machine had BEFORE this install, read now, before anything below rewrites
# installed_plugins.json or prunes the old cache (0.15.0). A machine migration needs it --
# an upgrader from a gt that shipped /gt:gt-farm keeps the farm module -- and by the time
# migrations run the machine no longer says. Passed through the environment, not a flag,
# so an older release's migrator (a rollback) ignores it instead of refusing an unknown
# option. Kept across the re-run below, which would otherwise read this install's own gt.
if [ -z "${GT_PREVIOUS_RELEASE:-}" ]; then
  GT_PREVIOUS_RELEASE=$(python3 -c "import json,sys
try:
    e = json.load(open(sys.argv[1], encoding='utf-8'))['plugins']['gt@golden-thread-plugin'][0]
    print(e.get('version') or '')
except Exception:
    print('')" "$INSTALLED" 2>/dev/null) || GT_PREVIOUS_RELEASE=""
fi
export GT_PREVIOUS_RELEASE

plugin_src()   { echo "$SCRIPT_DIR/${PLUGIN_DIRS[$1]}/${PLUGIN_VERS[$1]}"; }
plugin_cache() { echo "$CACHE_ROOT/${PLUGIN_NAMES[$1]}/${PLUGIN_VERS[$1]}"; }

# ── Modules: read, validate the flags, resolve each module's state ──────────────
# Nothing here writes: an unknown name or --without gt must leave the machine untouched,
# and --list-modules exits at the end of this block.
GT_TMP=$(mktemp -d "${TMPDIR:-/tmp}/gt-install.XXXXXX")
trap 'rm -rf "$GT_TMP"' EXIT
MODJSON="$GT_TMP/modules.json"
SCAN_ARGS=()
i=1
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  SCAN_ARGS+=("${PLUGIN_DIRS[$i]}" "${PLUGIN_VERS[$i]}" "${PLUGIN_NAMES[$i]}")
  i=$((i + 1))
done
if ! modpy scan "$SCRIPT_DIR" "$VERSION" ${SCAN_ARGS[@]+"${SCAN_ARGS[@]}"} > "$GT_TMP/scan.json"; then
  echo "✗ A module's module.json is not usable (above). Nothing has been installed."
  exit 1
fi
MODULE_NAMES=$(modpy names "$GT_TMP/scan.json")

for f in ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}; do
  n="${f#*:}"
  if [ "$n" = gt ]; then
    echo "✗ --${f%%:*} gt refused: gt is the core plugin, not a module, and is always installed."
    echo "  Modules: ${MODULE_NAMES:-none in this tree}"
    exit 1
  fi
  case ", $MODULE_NAMES," in
    *", $n,"*) ;;
    *) echo "✗ Unknown module '$n'. Nothing has been installed."
       echo "  Modules: ${MODULE_NAMES:-none in this tree}"
       exit 1 ;;
  esac
  for g in ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}; do
    if [ "${g#*:}" = "$n" ] && [ "${g%%:*}" != "${f%%:*}" ]; then
      echo "✗ Both --with $n and --without $n given. Nothing has been installed."
      exit 1
    fi
  done
done

# -> writes $1 (the scan plus state/why per module); prints "name<TAB>state<TAB>why".
# `module-states` of the gt being installed decides; an older gt without it (a rollback)
# gets the same precedence computed by the helper.
resolve_modules() {
  local out="$1" states="-" rc=0 f
  local -a margs=()
  for f in ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}; do margs+=("--${f%%:*}" "${f#*:}"); done
  if [ -n "$MODULE_NAMES" ] && [ -f "$SRC/scripts/gt_components.py" ]; then
    states=$(python3 "$SRC/scripts/gt_components.py" module-states "$SCRIPT_DIR" \
             --home "$HOME" --gt-version "$VERSION" --detail \
             ${margs[@]+"${margs[@]}"} 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ] || [ "${states#\{}" = "$states" ]; then states="-"; fi
  fi
  modpy resolve "$GT_TMP/scan.json" "$out" "$HOME" "$states" ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}
}
resolve_modules "$MODJSON" > "$GT_TMP/states.tsv"

if [ "$LIST_MODULES" = yes ]; then
  modpy list "$MODJSON" "$VERSION"
  exit 0
fi

# Skills the previous gt shipped that this gt does not, and where each went (0.15.0:
# /gt:gt-watch became /gt-watch:gt-watch). Read now, while the old gt cache is still on
# disk and every plugin -- on or off -- is still in the arrays; printed with the summary.
# Exported so the migration re-run below keeps what the first run saw.
if [ -z "${GT_MOVED_NOTES+set}" ]; then
  GT_MOVED_NOTES=""
  if [ -n "${GT_PREVIOUS_RELEASE:-}" ] && [ "$GT_PREVIOUS_RELEASE" != "$VERSION" ]; then
    _old_skills="$CACHE_ROOT/gt/$GT_PREVIOUS_RELEASE/skills"
    [ -d "$_old_skills" ] || _old_skills="$SCRIPT_DIR/$CORE_DIR/$GT_PREVIOUS_RELEASE/skills"
    _margs=()
    i=1
    while [ "$i" -lt "$PLUGIN_COUNT" ]; do
      _margs+=("$SCRIPT_DIR/${PLUGIN_DIRS[$i]}" "${PLUGIN_VERS[$i]}" "${PLUGIN_NAMES[$i]}")
      i=$((i + 1))
    done
    GT_MOVED_NOTES=$(modpy moved "$_old_skills" "$SRC/skills" /dev/null \
                     ${_margs[@]+"${_margs[@]}"} 2>/dev/null) || GT_MOVED_NOTES=""
  fi
fi
export GT_MOVED_NOTES

# The plugin set to INSTALL: gt, every non-module plugin, every module that is on.
# Modules that are off are kept apart, for removal.
OFF_MODULES=""
SKIP_NOTES=""
_dirs=(); _vers=(); _names=()
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  state=on
  while IFS="$(printf '\t')" read -r _m _state _why _plugin; do
    [ -n "$_m" ] && [ "$i" -gt 0 ] && [ "$_plugin" = "${PLUGIN_NAMES[$i]}" ] || continue
    state="$_state"
    if [ "$_state" = off ]; then
      OFF_MODULES="$OFF_MODULES $_m"
      [ "$_why" = invalid ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m: its module.json is invalid (./install.sh --list-modules says why)
"
      [ "$_why" = requires ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m (${PLUGIN_NAMES[$i]} ${PLUGIN_VERS[$i]}): its requires_gt does not admit gt $VERSION — treated as off for this run, your recorded choice unchanged
"
    fi
  done < "$GT_TMP/states.tsv"
  if [ "$state" = on ]; then
    _dirs+=("${PLUGIN_DIRS[$i]}"); _vers+=("${PLUGIN_VERS[$i]}"); _names+=("${PLUGIN_NAMES[$i]}")
  fi
  i=$((i + 1))
done
PLUGIN_DIRS=("${_dirs[@]}"); PLUGIN_VERS=("${_vers[@]}"); PLUGIN_NAMES=("${_names[@]}")
PLUGIN_COUNT=${#PLUGIN_DIRS[@]}

OTHERS=""
i=1
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  OTHERS="$OTHERS, ${PLUGIN_NAMES[$i]} ${PLUGIN_VERS[$i]}"
  i=$((i + 1))
done
if [ -n "$REQUESTED" ]; then
  echo "Installing gt $VERSION (pinned; newest available is $(latest_version "$SCRIPT_DIR/$CORE_DIR"))$OTHERS"
else
  echo "Installing gt $VERSION (newest version directory)$OTHERS"
fi
[ -n "$PICK_NOTES" ] && printf '%s' "$PICK_NOTES"
[ -n "$SKIP_NOTES" ] && printf '%s' "$SKIP_NOTES"

# 0. Remove superseded versions of EVERY plugin so old caches don't linger unreferenced.
# The cache holds only what installed_plugins.json points at; rollback reads the SOURCE
# tree (which keeps the newest-but-one, see above), never an old cache. gt-wiki was
# missed until 0.13.0, so its caches piled up (0.1.0 and 0.1.1 beside the current one).
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  plugin="${PLUGIN_NAMES[$i]}"; keep="${PLUGIN_VERS[$i]}"
  for old in "$CACHE_ROOT/$plugin"/*; do
    [ -d "$old" ] || continue
    if [ "$(basename "$old")" != "$keep" ]; then
      rm -rf "$old"
      echo "Removed superseded $plugin cache → $old"
    fi
  done
  i=$((i + 1))
done

# (Until 0.14.0 an install_demo=no in vault-config.json stripped the demo out of the gt
# plugin here. The demo is now module `demo` (plugin gt-demo); the 0.14.0 machine
# migration records that setting as the module choice, and step 1a below applies it.)
#
# A rollback to a gt that still ships the demo INSIDE gt (before 0.14.0) must honour the
# choice the way that release did, or `./install.sh 0.13.0` brings /gt:gt-demo back over
# an install_demo=no (0.15.0). The choice, most specific first: --with/--without demo this
# run, the recorded module choice, install_demo in vault-config.json. strip_gt_demo runs
# after the cache copy (step 1) and the marketplace copy (step 2b), as 0.13.0 did.
DEMO_IN_GT_OFF=no
if [ -d "$SRC/skills/gt-demo" ]; then
  DEMO_IN_GT_OFF=$(python3 - "$HOME" ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"} <<'PYEOF' 2>/dev/null || echo no
import json, os, sys
home, flags = sys.argv[1], sys.argv[2:]
state = None
for f in flags:
    if f in ("with:demo", "without:demo"):
        state = "on" if f == "with:demo" else "off"
if state is None:
    try:
        state = json.load(open(os.path.join(home, ".claude", "golden-thread",
                                            "install-choices.json")))["choices"].get("demo")
    except Exception:
        state = None
if state is None:
    try:
        v = json.load(open(os.path.join(home, ".claude", "vault-config.json"))).get("install_demo")
        state = "off" if str(v or "yes").strip().lower() == "no" else None
    except Exception:
        state = None
print("yes" if state == "off" else "no")
PYEOF
)
fi
strip_gt_demo() {  # $1 = a gt plugin dir (cache or marketplace)
  [ "$DEMO_IN_GT_OFF" = yes ] || return 0
  rm -rf "${1:?}/skills/gt-demo" "$1/scripts/gt_demo.sh" "$1/templates/demo-pizzabot"
}

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
       echo "REFUSING TO INSTALL — shipped files that RUN, or that security controls READ,"
       echo "do not match MANIFEST.json"
       printf '%s\n' "$out" | sed 's/^/  /'
       echo ""
       echo "Nothing has been installed. Files marked EXECUTES run on every prompt, so"
       echo "installing a set the manifest does not describe means running code nobody has"
       echo "verified. Files marked INPUT TO SECURITY CONTROLS are data, but they are the data"
       echo "that DEFINES what counts as a credential and which paths are ignored — editing one"
       echo "is editing the control, which is why a mismatch refuses rather than warns."
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

# 1a. Module choices. --with / --without are recorded now -- after every refusal above,
# so a refused install records nothing -- through the gt being installed
# (`gt_components.py record-choice`), or the helper when that gt predates it.
for f in ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}; do
  _n="${f#*:}"; _s=on; [ "${f%%:*}" = without ] && _s=off
  if ! python3 "$SRC/scripts/gt_components.py" record-choice "$HOME" "$_n" "$_s" \
       --plugin-root "$SCRIPT_DIR" >/dev/null 2>&1; then
    modpy record "$HOME" "$_n" "$_s" || echo "⚠ could not record the choice $_n=$_s"
  fi
  echo "Recorded module choice: $_n $_s  (~/.claude/golden-thread/install-choices.json)"
done

# A module that is off leaves nothing behind: every cache version, its marketplace entry
# and dir, its installed_plugins entry, its enabledPlugins key and its hooks-dir files
# (JSON files and hook files backed up first; each removal printed). Its hook entries in
# settings.json are removed with the hook registrations in step 6. Only
# <plugin>@golden-thread-plugin keys are touched; nothing of anyone else's.
modpy remove "$MODJSON" "$HOME" "$SRC" "$CACHE_ROOT" "$MARKETPLACE" "$INSTALLED" "$SETTINGS" \
  "$HOME/.claude/golden-thread/hooks"

# File modes are SET, never inherited (0.15.0, R1). `cp` gives a new file the source's mode
# and leaves an existing file's mode alone, so a fresh install copied an untracked 0700
# source tree as 0700 (hooks 0711 after chmod +x) while an upgrade kept the 0755/0644 the
# older release left -- the same release, two different machines. Directories 0755,
# *.sh and *.py 0755, every other file 0644, whatever the source or the old copy said.
set_modes() {  # $@ = files or directory trees install.sh itself wrote
  local p
  for p in "$@"; do
    [ -e "$p" ] || continue
    if [ -d "$p" ]; then
      find "$p" -type d -name __pycache__ -prune -exec rm -rf {} +
      find "$p" -type d -exec chmod 755 {} +
      find "$p" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod 755 {} +
      find "$p" -type f ! -name '*.sh' ! -name '*.py' -exec chmod 644 {} +
    else
      case "$p" in *.sh|*.py) chmod 755 "$p" ;; *) chmod 644 "$p" ;; esac
    fi
  done
}

# 1. Install every plugin's files into the cache.
#
# Each directory is REPLACED, not merged into: a cache of the same version from an earlier
# install otherwise keeps files the release no longer ships -- which is how the gt plugin
# would go on carrying the demo after 0.14.0 moved it into the gt-demo module.
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  p_src=$(plugin_src "$i"); p_cache=$(plugin_cache "$i")
  mkdir -p "$p_cache"
  # module.json and demo/ travel too: gt-demo reads module.json and the module's tour act
  # (demo/act.md) from the CACHE to assemble the tour for whatever is installed (0.14.0).
  # packs/ holds the pluggable definitions gt_registry.py resolves at runtime.
  # Without it the registry finds nothing on an installed machine and every
  # contributed definition is inert (found by security review, 2026-09-16).
  for dir in .claude-plugin skills scripts templates commands hooks demo packs; do
    rm -rf "${p_cache:?}/$dir"
    [ -d "$p_src/$dir" ] && cp -r "$p_src/$dir" "$p_cache/"
  done
  # MANIFEST.json travels for the same reason packs/ does, and it is load-bearing: it is the
  # ONLY runtime integrity control on an installed pack. Without it gt_registry.py cannot
  # verify a single pack, and since it now fails closed, every shipped pack would refuse to
  # load on an installed machine (both halves found by security review, 2026-09-16).
  for f in module.json MANIFEST.json; do
    rm -f "${p_cache:?}/$f"
    [ -f "$p_src/$f" ] && cp "$p_src/$f" "$p_cache/$f"
  done
  set_modes "$p_cache"
  echo "Installed ${PLUGIN_NAMES[$i]} plugin files → $p_cache"
  i=$((i + 1))
done
strip_gt_demo "$(plugin_cache 0)"

# 1b. Install the Core-rule hooks to a STABLE location outside the vault.
# settings.json references these by absolute path, so the path must survive project
# renames, merges and vault moves. The scripts locate the rules at run time.
GT_HOOKS="$HOME/.claude/golden-thread/hooks"
# Installing a gt OLDER than the newest in this tree (a rollback): what the newer releases
# put in the hooks dir and wired is removed below when the older gt does not ship it, so
# the rolled-back machine matches that release (0.15.0). Only bytes a release shipped count.
GT_NEWER_OWNED=""
if [ "$VERSION" != "$(latest_version "$SCRIPT_DIR/$CORE_DIR")" ]; then
  GT_NEWER_OWNED="$GT_TMP/newer-owned.json"
  modpy newer-owned "$SCRIPT_DIR" "$CORE_DIR" "$VERSION" > "$GT_NEWER_OWNED" 2>/dev/null \
    || GT_NEWER_OWNED=""
fi
export GT_NEWER_OWNED
if [ -d "$SRC/hooks" ]; then
  mkdir -p "$GT_HOOKS"
  # Each copied file is recorded so its mode can be SET below -- only these: a file of the
  # user's own in the hooks dir is not ours to chmod.
  GT_HOOK_FILES=()
  for f in "$SRC/hooks"/*; do
    [ -f "$f" ] || continue
    cp "$f" "$GT_HOOKS/" && GT_HOOK_FILES+=("$GT_HOOKS/$(basename "$f")")
  done
  cp "$SRC/scripts/gt_paths.py" "$GT_HOOKS/gt_paths.py" && GT_HOOK_FILES+=("$GT_HOOKS/gt_paths.py")
  # Component drift detection and the other session-start checks run FROM the hooks dir,
  # for the same reason the hooks themselves do: settings.json addresses them by
  # absolute path, so the path must survive a vault move or a project rename.
  # The list lives in gt_components.HOOK_DIR_SCRIPTS, which also maps these files
  # for drift checking -- a second copy here would be a copy that drifts.
  for extra in $(python3 "$SRC/scripts/gt_components.py" hookdir-scripts --home "$HOME"); do
    [ -f "$SRC/scripts/$extra" ] && cp "$SRC/scripts/$extra" "$GT_HOOKS/$extra" \
      && GT_HOOK_FILES+=("$GT_HOOKS/$extra")
  done
  # Modules that are on: their hookdir_scripts, and the scripts their hooks run (declared
  # in module.json, registered in step 6 exactly like gt's own).
  while IFS= read -r extra; do
    [ -n "$extra" ] && cp "$extra" "$GT_HOOKS/$(basename "$extra")" \
      && GT_HOOK_FILES+=("$GT_HOOKS/$(basename "$extra")")
  done < <(modpy onfiles "$MODJSON")
  set_modes ${GT_HOOK_FILES[@]+"${GT_HOOK_FILES[@]}"}
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
  python3 - "$SRC" "$GT_HOOKS" "$MODJSON" <<'EOF' || true
import json, os, shutil, subprocess, sys, time
src, hooks, modjson = sys.argv[1:4]
shipped = {"gt_paths.py"}
for m in json.load(open(modjson, encoding="utf-8")):
    if m["state"] == "on":
        shipped |= {os.path.basename(p) for p in m["hookdir_scripts"]}
        shipped |= {h["script"] for h in m["hook_scripts"]}
hdir = os.path.join(src, "hooks")
shipped |= {n for n in os.listdir(hdir) if os.path.isfile(os.path.join(hdir, n))}
certain = True
try:
    out = subprocess.check_output(
        ["python3", os.path.join(src, "scripts", "gt_components.py"), "hookdir-scripts",
         "--home", os.path.expanduser("~")], text=True)
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
# A rollback: a file a NEWER release in the tree installed, byte-identical to its copy.
newer = {}
if os.environ.get("GT_NEWER_OWNED"):
    try:
        newer = json.load(open(os.environ["GT_NEWER_OWNED"], encoding="utf-8")).get("files", {})
    except (OSError, ValueError, AttributeError):
        newer = {}
if newer:
    import hashlib
    for n in present:
        try:
            h = hashlib.sha256(open(os.path.join(hooks, n), "rb").read()).hexdigest()
        except OSError:
            continue
        if h in set(newer.get(n, [])):
            retired.add(n)
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

# 2. Create marketplace directory structure: one entry per plugin.
#
# Each entry's description is read from that plugin's own plugin.json. Until 0.14.0
# marketplace.json carried hand-typed descriptions for gt and gt-wiki, which had already
# drifted from the manifests -- and a third plugin would have had no entry at all.
mkdir -p "$MARKETPLACE/.claude-plugin"
MARKET_ARGS=()
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  mkdir -p "$MARKETPLACE/plugins/${PLUGIN_NAMES[$i]}/.claude-plugin"
  MARKET_ARGS+=("${PLUGIN_NAMES[$i]}" "$(plugin_src "$i")/.claude-plugin/plugin.json")
  i=$((i + 1))
done
python3 - "$MARKETPLACE/.claude-plugin/marketplace.json" "$MARKET_NAME" "${MARKET_ARGS[@]}" <<'EOF'
import json, sys
out, market, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
plugins = []
for name, manifest in zip(rest[0::2], rest[1::2]):
    try:
        desc = json.load(open(manifest, encoding="utf-8")).get("description") or ""
    except (OSError, ValueError, AttributeError):
        desc = ""
    plugins.append({"name": name, "source": "./plugins/%s" % name, "description": desc})
with open(out, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"name": market,
                         "owner": {"name": "Stacy Haven",
                                   "email": "shaven@shavenconsulting.com"},
                         "plugins": plugins}, indent=2) + "\n")
EOF
# Rewriting a file keeps its old mode, so marketplace.json needs set_modes like the plugin
# dirs below (an upgrade kept a 0600 copy where a fresh install wrote 0644).
set_modes "$MARKETPLACE/.claude-plugin"
chmod 755 "$MARKETPLACE" "$MARKETPLACE/plugins" 2>/dev/null || true

# The plugin manifests are copied from source, not regenerated here. Two
# hand-maintained copies of the same manifest drift - the descriptions had
# already diverged once.
#
# 2b. Populate the marketplace's plugin directories with the ACTUAL plugin.
# marketplace.json declares "source": "./plugins/gt", so that path must hold a
# loadable plugin - not just a manifest. Without this, anything that resolves the
# plugin from the marketplace (rather than from the installed cache) finds zero
# skills, and no /gt: commands appear.
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  p_src=$(plugin_src "$i"); p_market="$MARKETPLACE/plugins/${PLUGIN_NAMES[$i]}"
  cp "$p_src/.claude-plugin/plugin.json" "$p_market/.claude-plugin/plugin.json"
  for dir in skills scripts templates commands hooks demo packs; do
    rm -rf "$p_market/$dir"
    [ -d "$p_src/$dir" ] && cp -r "$p_src/$dir" "$p_market/$dir"
  done
  for f in module.json MANIFEST.json; do          # MANIFEST.json: see the cache loop above
    rm -f "$p_market/$f"
    [ -f "$p_src/$f" ] && cp "$p_src/$f" "$p_market/$f"
  done
  set_modes "$p_market"
  i=$((i + 1))
done
strip_gt_demo "$MARKETPLACE/plugins/gt"
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
REG_ARGS=()
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  REG_ARGS+=("${PLUGIN_NAMES[$i]}@$MARKET_NAME" "$(plugin_cache "$i")" "${PLUGIN_VERS[$i]}")
  i=$((i + 1))
done
python3 - "$KNOWN" "$MARKETPLACE" "$INSTALLED" "$SETTINGS" "$SRC" "$MARKET_NAME" "${REG_ARGS[@]}" <<'EOF'
import json, os, sys, tempfile, time
from datetime import datetime, timezone
known, marketplace, installed, settings, src, market_name = sys.argv[1:7]
_reg = sys.argv[7:]
PLUGINS = list(zip(_reg[0::3], _reg[1::3], _reg[2::3]))    # (key, cache path, version)
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUPS = os.path.expanduser("~/.claude/golden-thread/backups")
# One key order whatever the history (0.15.0, R1): other marketplaces first, then this
# install in plugin order. A gt too old to have order_plugin_keys (a rollback) appends.
sys.path.insert(0, os.path.join(src, "scripts"))
try:
    from gt_components import order_plugin_keys
except ImportError:
    order_plugin_keys = lambda *_a: False
ORDER = [k for k, _p, _v in PLUGINS]

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
for key, path, ver in PLUGINS:
    d.setdefault("plugins", {})[key] = [{
        "scope": "user", "installPath": path, "version": ver,
        "installedAt": now, "lastUpdated": now, "gitCommitSha": "local",
    }]
order_plugin_keys(d.get("plugins"), ORDER, market_name)
save(installed, d)
print("Registered in installed_plugins.json")

d = load(settings, {})
for key, _path, _ver in PLUGINS:
    d.setdefault("enabledPlugins", {})[key] = True
order_plugin_keys(d.get("enabledPlugins"), ORDER, market_name)
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
# PreCompact / SessionEnd -> gt_report_card.py: since 0.15.0 declared by the report-card
#                  module's module.json and wired here exactly like gt's own entries.
python3 - "$SRC" "$SCRIPT_DIR" "$MODJSON" <<'EOF'
import json, os, subprocess, sys, tempfile
src, script_dir, modjson = sys.argv[1:4]
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
     'hook-registrations', src, script_dir, '--home', os.path.expanduser('~')], text=True))
# Module hooks arrive in the same list, tagged "module". One whose module is off in THIS
# run (by choice, or skipped for requires_gt) is not wired, and its entries are removed
# below -- as are entries for any hook an off module declares in module.json.
mods = json.load(open(modjson, encoding='utf-8'))
off_mods = {m['name'] for m in mods if m['state'] == 'off'}
off_scripts = {h['script']: m['name'] for m in mods if m['state'] == 'off'
               for h in m['hook_scripts']}
for r in regs:
    if r.get('module') in off_mods:
        off_scripts.setdefault(r['script'], r['module'])
regs = [r for r in regs if r.get('module') not in off_mods]
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
if os.environ.get('GT_NEWER_OWNED'):
    # A rollback: entries a newer release in the tree wired that this gt does not register.
    try:
        with open(os.environ['GT_NEWER_OWNED'], encoding='utf-8') as fh:
            retired |= set(json.load(fh).get('registrations', [])) - known
    except (OSError, ValueError, AttributeError, TypeError):
        pass
off_scripts = {k: v for k, v in off_scripts.items() if k not in known}

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

removed, unknown, bak, mod_removed = [], [], None, []
for event, blocks in list(hooks.items()):
    if not isinstance(blocks, list):
        continue
    before = len(removed) + len(mod_removed)
    for b in blocks:
        if not isinstance(b, dict) or not isinstance(b.get('hooks'), list):
            continue
        keep = []
        for h in b['hooks']:
            s = gt_script(h.get('command') if isinstance(h, dict) else None)
            if s is not None and s in off_scripts:
                mod_removed.append((off_scripts[s], '%s/%s' % (event, s)))
                continue
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
    if not blocks and len(removed) + len(mod_removed) > before:
        del hooks[event]

# One canonical order for gt's entries (0.15.0, R1): the user's own blocks first, then gt's
# in hook-registrations order. Appending made the order depend on history -- an upgrade
# from 0.13.0/0.14.0 left guard_protected_paths.sh last in PreToolUse, a fresh install
# first. vault_init applies the same ordering after it wires the enforcement hooks. A gt
# too old to have order_hook_blocks (a rollback) keeps its append order.
sys.path.insert(0, os.path.join(src, 'scripts'))
try:
    import gt_components as _gc
    _gc.order_hook_blocks(hooks, [(r['event'], r['script']) for r in regs], HOOKS_DIR)
except (ImportError, AttributeError):
    pass
if (removed or mod_removed) and os.path.exists(p):
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
for mod, r in sorted(mod_removed):
    print('Module %s is off: removed hook entry %s (backup: %s)' % (mod, r, bak))
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
  if python3 "$SRC/scripts/gt_components.py" wiring "$SRC" --owner install.sh --home "$HOME" >/dev/null 2>&1; then
    echo "Verified hook wiring → every hook this installer owns is connected"
  else
    echo "⚠ Hook wiring INCOMPLETE after install — these will NEVER RUN:"
    python3 "$SRC/scripts/gt_components.py" wiring "$SRC" --owner install.sh --home "$HOME" 2>&1 | sed 's/^/    /' || true
  fi
fi

# 6b. Machine migrations (0.14.0, Requirement R1: upgrades converge).
#
# gt_upgrade migrations are stamped in a VAULT; some changes a release makes belong to the
# MACHINE (~/.claude) instead, and someone jumping several releases in one install must
# still get each of them. The release ships gt_machine_migrate.py to apply exactly those.
#
# Placed here: after the plugin files and hooks are installed and wired, because a
# migration may rely on them; BEFORE the vault step, so a vault is never set up on a
# machine left half-migrated.
#
# A failure STOPS the install, loudly and after printing what the migrator said. Nothing
# is rolled back -- the files above are the new release's and are correct -- but carrying
# on to "installed." would report success over a machine that is not in the state the
# release assumes. Exit 7 is this case and only this case (4 = no vault, 6 = manifest).
#
# Absent script = an older release being installed as a rollback: say nothing.
run_machine_migrations() {
  local mig="$SRC/scripts/gt_machine_migrate.py" out rc=0
  [ -f "$mig" ] || return 0
  out=$(python3 "$mig" --home "$HOME" run --release "$SRC" 2>&1) || rc=$?
  echo ""
  echo "Machine migrations:"
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | sed 's/^/  /'
  elif [ "$rc" -eq 0 ]; then
    echo "  nothing to apply"
  fi
  if [ "$rc" -ne 0 ]; then
    echo ""
    echo "════════════════════════════════════════════════════════════════════════"
    echo "INSTALL INCOMPLETE — machine migration failed; nothing was rolled back; fix and re-run install.sh"
    echo "════════════════════════════════════════════════════════════════════════"
    echo "The migrator exited $rc ($( [ "$rc" -eq 2 ] && echo "could not evaluate" || echo "a migration failed" ))."
    echo "Plugin files and hooks for gt $VERSION are in place; the vault step did not run."
    echo "  Status:  python3 \"$mig\" status --release \"$SRC\""
    exit 7
  fi
  return 0
}
run_machine_migrations

# A migration may have recorded a module choice (0.14.0: install_demo=no in
# vault-config.json becomes demo=off). The plugin set above was chosen before it ran, so
# if any module's state now differs, run the install once more from the top: the second
# run finds nothing to migrate and lays the machine down in the converged state.
# GT_INSTALL_RERUN stops a second re-run if a migration kept flipping a choice.
resolve_modules "$GT_TMP/after.json" > "$GT_TMP/after.tsv"
if [ "$(cut -f1,2 "$GT_TMP/states.tsv")" != "$(cut -f1,2 "$GT_TMP/after.tsv")" ]; then
  if [ -z "${GT_INSTALL_RERUN:-}" ]; then
    echo ""
    echo "Machine migrations changed a module choice — running the install steps again to apply it:"
    cut -f1,2 "$GT_TMP/after.tsv" | awk -F'\t' '{print "  " $1 " " $2}' || true
    echo ""
    rm -rf "$GT_TMP"
    export GT_INSTALL_RERUN=1
    exec bash "$SCRIPT_DIR/install.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
  fi
  echo "⚠ Module choices changed again after a re-run; left as they are. Re-run install.sh to converge."
fi

# Crontab lines a module's earlier --without removed go back once it is on again (0.15.0).
# After the re-run above, so they are restored against the converged module set, and after
# step 1, so the hooks-dir script a restored line runs is already in place.
modpy restore-cron "$MODJSON" "$HOME"

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
# Set by setup_vault only when `vault_init fresh` ran in THIS run: the resolved vault path,
# and whether it was already inside a git repo beforehand. Never inferred from contents.
VAULT_CREATED_NOW=""
VAULT_GIT_PREEXISTED=""
# The vault's git state BEFORE this install touched it (connect, core-rule seeding, the
# githooks and vault-tool refresh below all write into it). The upgrade step decides
# "clean" from this snapshot, so the install's OWN refreshes never block an upgrade, while
# the owner's uncommitted work still does. One vault only: the first path snapshotted
# wins unless a different path is snapshotted before it is touched.
VAULT_SNAP_PATH=""
VAULT_SNAP_STATE=""      # clean | dirty | nogit | absent | unreadable

real_path() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || printf '%s\n' "$1"; }

snapshot_vault_state() {  # $1 = vault path, before anything writes into it
  local real porcelain
  real=$(real_path "$1")
  [ "$real" = "$VAULT_SNAP_PATH" ] && return 0
  VAULT_SNAP_PATH="$real"
  if [ ! -d "$real" ]; then
    VAULT_SNAP_STATE=absent
  elif ! git -C "$real" rev-parse --git-dir >/dev/null 2>&1; then
    VAULT_SNAP_STATE=nogit
  elif porcelain=$(git -C "$real" status --porcelain 2>/dev/null); then
    if [ -z "$porcelain" ]; then VAULT_SNAP_STATE=clean; else VAULT_SNAP_STATE=dirty; fi
  else
    VAULT_SNAP_STATE=unreadable
  fi
}

# Every vault file this install may write, backed up BEFORE the first write (0.15.0).
# gt_upgrade's own vault tarball came after the core-rule, CLAUDE.md, git-hook and
# vault-tool refreshes, so it never held the originals those replaced. One backup per
# vault per run; pruned at the end when nothing in it changed.
VAULT_PREWRITE_PATH=""
VAULT_PREWRITE_BACKUP=""
backup_vault_before_writes() {  # $1 = vault path, before anything writes into it
  local real
  real=$(real_path "$1")
  [ -d "$real" ] && [ "$real" != "$VAULT_PREWRITE_PATH" ] || return 0
  [ -f "$SRC/scripts/vault_refresh.py" ] || return 0
  VAULT_PREWRITE_PATH="$real"
  VAULT_PREWRITE_BACKUP="$HOME/.claude/golden-thread/backups/install-vault-files-$(date +%Y%m%d_%H%M%S).tar.gz"
  python3 "$SRC/scripts/vault_refresh.py" backup --vault "$real" --out "$VAULT_PREWRITE_BACKUP" \
    >/dev/null 2>&1 || { echo "⚠ could not back up the vault's files before installing — continuing"; VAULT_PREWRITE_BACKUP=""; }
}

# What install-core-rules put back, by name (0.15.0): a Core rule re-created or the
# CLAUDE.md enforcement section re-inserted was silent, so a deliberate deletion simply
# came back. stdin = its JSON result.
report_core_rules() {
  python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for r in rows:
    path, act, note = r.get("path", ""), r.get("action"), r.get("note", "")
    name = os.path.basename(path)
    if act == "created" and os.sep + "core-rules" + os.sep in path and name.endswith(".md"):
        print("Added Core rule → %s (not in the vault: new in this release, or removed."
              % name)
        print("  To keep a rule removed, list its file name in %s)"
              % os.path.join(os.path.dirname(os.path.dirname(path)), ".gt-removed"))
    elif act == "updated" and name == "CLAUDE.md":
        print("Inserted into CLAUDE.md → the \"First: is enforcement active?\" section")
        print("  (to keep it out, add claude-md-enforcement-section to .gt-removed next to core-rules/)")
    elif act == "kept-removed":
        print(("⚠ " if "WARNING" in note else "") + "Left removed → %s: %s" % (name, note))
' 2>/dev/null || true
}

setup_vault() {  # $1 = path to create or connect
  local target="$1" mode out
  snapshot_vault_state "$target"
  backup_vault_before_writes "$target"
  if [ -d "$target/Projects" ] || [ -f "$target/index.md" ]; then
    mode=connect
  else
    mode=fresh
  fi
  echo ""
  if [ "$mode" = fresh ]; then
    echo "Creating a vault at $target"
    # Was the target already inside a git repo? Only a repo THIS run created may be given
    # an initial commit by the vault-upgrade step (see apply_vault_upgrades).
    local git_before=no
    git -C "$target" rev-parse --git-dir >/dev/null 2>&1 && git_before=yes
    python3 "$SRC/scripts/vault_init.py" fresh --vault "$target" --domain "Personal" \
      >/dev/null 2>&1 || { echo "⚠ could not create the vault at $target"; return 1; }
    VAULT_CREATED_NOW=$(real_path "$target")
    VAULT_GIT_PREEXISTED="$git_before"
  else
    echo "Connecting the existing vault at $target"
    python3 "$SRC/scripts/vault_init.py" connect --vault "$target" \
      >/dev/null 2>&1 || { echo "⚠ could not connect $target"; return 1; }
  fi
  if out=$(python3 "$SRC/scripts/vault_init.py" install-core-rules --vault "$target" 2>/dev/null); then
    echo "Wired enforcement hooks → ~/.claude/settings.json"
    # A vault created just now has every rule by definition; only a connected one can
    # have had something put back.
    [ "$mode" = fresh ] || printf '%s' "$out" | report_core_rules
  else
    echo "⚠ vault ready, but the enforcement hooks could not be wired"
  fi
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
    printf '%s' "$out" | report_core_rules
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
  snapshot_vault_state "$VAULT_PATH"
  backup_vault_before_writes "$VAULT_PATH"
  wire_enforcement_hooks "$VAULT_PATH"
fi

# Ask git, not the filesystem: .git is a FILE for a worktree, a submodule or a
# --separate-git-dir checkout, and `-d .git` skipped all of those silently.
if [ -n "$VAULT_PATH" ] && git -C "$VAULT_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  # .githooks/, the vault tools and core.hooksPath, refreshed by CONTENT (0.15.0): a copy
  # gt shipped is replaced, the owner's own is kept (or, for a committed hook edit, backed
  # up first). The rules and why: scripts/vault_refresh.py. Until 0.15.0 this block copied
  # the hooks over unconditionally, replaced any tool older ON DISK than the template, and
  # set core.hooksPath whatever it held -- three ways to lose an owner's edit.
  if [ -f "$SRC/scripts/vault_refresh.py" ]; then
    python3 "$SRC/scripts/vault_refresh.py" refresh --vault "$VAULT_PATH" 2>&1 \
      || echo "⚠ the vault's git hooks and tools could not be refreshed — run install.sh again to retry"
  fi
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
# user actually got: a module that is off has no cache and no entry in the plugin set,
# so its skills drop out of the list for free, with no second condition to keep in step.
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

i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  # A module with no skills (gt-report-card is hooks only) gets no empty heading.
  if ls "$(plugin_cache "$i")/skills"/*/SKILL.md >/dev/null 2>&1; then
    echo "${PLUGIN_NAMES[$i]} skills:"
    list_skills "$(plugin_cache "$i")/skills" "/${PLUGIN_NAMES[$i]}:"
    echo ""
  fi
  i=$((i + 1))
done
# One line for the modules: what is on, what is off and why. Off modules are absent from
# the skill lists above because they are absent from the cache.
if [ -n "$MODULE_NAMES" ]; then
  modpy summary "$MODJSON"
  echo "  (change with ./install.sh --with NAME / --without NAME; see --list-modules)"
  echo ""
fi
# Commands that left gt for a module since the gt this machine had (computed above).
if [ -n "${GT_MOVED_NOTES:-}" ]; then
  printf '%s\n' "$GT_MOVED_NOTES" | modpy moved-annotate "$GT_TMP/states.tsv" \
    || printf '%s\n' "$GT_MOVED_NOTES"
  echo "  (the old names no longer resolve; use the new ones)"
  echo ""
fi
if [ "$DEMO_IN_GT_OFF" = yes ]; then
  echo "Demo not installed (demo choice is off) — /gt:gt-demo left out of this gt."
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

# Vault upgrades (0.14.0, Requirement R1: an install from any older release equals a fresh
# install of the newest). Until 0.14.0 this only REPORTED, so a release's migrations were
# applied only by someone remembering /gt:gt-upgrade.
#
# Owner decision 2026-09-14, "Apply when clean":
#   * a vault THIS run created gets an initial commit first (only when this run's
#     vault_init also created its git repo, the repo root is the vault, and there is no
#     commit yet -- `git add -A` must never sweep up anything that was already there);
#   * a git vault that was clean BEFORE this install touched it (VAULT_SNAP_*): `gt_upgrade
#     run` (it backs the vault up first), with --allow-dirty when the only uncommitted
#     files are this install's own refreshes. Those refreshes and the upgrade results are
#     left UNCOMMITTED, never staged, for the owner to review. A step needing a person is
#     left as gt_upgrade leaves it and reported;
#   * a vault that already had uncommitted changes, or is not a git repo: nothing applied,
#     pending steps reported with the exact command. The owner's work is never touched.
#   * "needs a person" items (a document with no merge base gt recorded) are never
#     pending steps and never applied here; they are reported on every install.
# `status` is asked first and `run` only when something is pending: `run` stamps the vault
# even when nothing is pending, which would dirty a clean vault on every install.
# Re-read the config: an interactive install may have created the vault after VAULT_PATH
# was first read. Nothing here fails the install.
upgrade_pending_list() {  # stdin = `gt_upgrade status` output: just the pending steps
  sed -n '/pending step(s):/,$p' | sed '/^needs a person (/,$d' | grep -v '^Rehearse:' \
    | sed '/^[[:space:]]*$/d' | sed 's/^/  /' || true
}

upgrade_attention_list() {  # stdin = gt_upgrade output: the "needs a person" items
  grep '^  needs a person: ' || true
}

commit_created_vault() {  # $1 = vault (resolved)
  local vault="$1" top out rc=0
  local -a id=()
  git -C "$vault" rev-parse --git-dir >/dev/null 2>&1 || return 0
  if [ "$VAULT_GIT_PREEXISTED" = yes ]; then
    echo "New vault is inside a git repository that already existed — not committed on your behalf."
    return 0
  fi
  top=$(git -C "$vault" rev-parse --show-toplevel 2>/dev/null) || top=""
  [ -n "$top" ] && [ "$(real_path "$top")" = "$vault" ] || return 0
  git -C "$vault" rev-parse --verify -q HEAD >/dev/null 2>&1 && return 0
  # Identity for this one commit only (-c), when none is configured. Global config untouched.
  [ -n "$(git -C "$vault" config user.name 2>/dev/null)" ] || id+=(-c "user.name=Golden Thread install")
  [ -n "$(git -C "$vault" config user.email 2>/dev/null)" ] || id+=(-c "user.email=golden-thread@localhost")
  out=$( { git -C "$vault" add -A && \
           git ${id[@]+"${id[@]}"} -C "$vault" commit -q -m "Golden Thread vault created by install.sh $VERSION"; } 2>&1) || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Committed the new vault → $(git -C "$vault" rev-parse --short HEAD 2>/dev/null) (Golden Thread vault created by install.sh $VERSION)"
  else
    echo "⚠ Could not make the new vault's initial commit: $(printf '%s\n' "$out" | tail -1)"
  fi
}

apply_vault_upgrades() {
  local vault real up out rc=0 porcelain run_out run_rc=0 backup attn ours=no
  local -a allow=()
  vault=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null) || vault=""
  [ -n "$vault" ] && [ -d "$vault" ] || return 0
  real=$(real_path "$vault")
  if [ -n "$VAULT_CREATED_NOW" ] && [ "$VAULT_CREATED_NOW" = "$real" ]; then
    commit_created_vault "$real"
  fi
  up="$SRC/scripts/gt_upgrade.py"
  [ -f "$up" ] || { echo "Vault upgrades: check could not run (gt_upgrade.py not shipped)"; echo ""; return 0; }

  out=$(python3 "$up" status --vault "$vault" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "⚠ Vault upgrades: the check could not run (exit $rc) — run /gt:gt-upgrade to look"
    echo ""; return 0
  fi
  attn=$(printf '%s\n' "$out" | upgrade_attention_list)
  if printf '%s\n' "$out" | grep -q 'nothing pending'; then
    echo "Vault upgrades: none pending"
    [ -n "$attn" ] && printf '%s\n' "$attn"
    echo ""; return 0
  fi
  if ! printf '%s\n' "$out" | grep -q 'pending step(s):'; then
    echo "⚠ Vault upgrades: the check could not run (unrecognised output) — run /gt:gt-upgrade to look"
    echo ""; return 0
  fi

  if ! git -C "$vault" rev-parse --git-dir >/dev/null 2>&1; then
    echo "⚠ Vault upgrades pending, not applied — the vault is not a git repository, so an"
    echo "  applied upgrade could not be reviewed or undone with git."
    printf '%s\n' "$out" | upgrade_pending_list
    [ -n "$attn" ] && printf '%s\n' "$attn"
    echo "  To apply (it backs the vault up first): /gt:gt-upgrade"
    echo "    or: python3 \"$up\" --vault \"$vault\" run"
    echo ""; return 0
  fi
  porcelain=$(git -C "$vault" status --porcelain 2>&1) || {
    echo "⚠ Vault upgrades pending, not applied — git could not read the vault's status:"
    printf '%s\n' "$porcelain" | tail -1 | sed 's/^/  /'
    printf '%s\n' "$out" | upgrade_pending_list
    [ -n "$attn" ] && printf '%s\n' "$attn"
    echo "  Run /gt:gt-upgrade to apply"
    echo ""; return 0; }
  if [ -n "$porcelain" ]; then
    # Uncommitted now. Whose? Only a snapshot taken before this install wrote into the
    # vault can say. Clean then = every change is the install's own refresh.
    if [ "$VAULT_SNAP_PATH" = "$real" ] && [ "$VAULT_SNAP_STATE" = clean ]; then
      ours=yes
      allow=(--allow-dirty)
    else
      echo "⚠ Vault upgrades pending, not applied — the vault has uncommitted changes (yours are never touched)."
      printf '%s\n' "$out" | upgrade_pending_list
      [ -n "$attn" ] && printf '%s\n' "$attn"
      echo "  Commit or stash your changes, then run /gt:gt-upgrade"
      echo "    or: python3 \"$up\" --vault \"$vault\" run"
      echo ""; return 0
    fi
  fi

  echo "Vault upgrades — applying to $vault:"
  if [ "$ours" = yes ]; then
    echo "  (the vault was clean before this install; its uncommitted files are this install's own refreshes:)"
    printf '%s\n' "$porcelain" | sed 's/^/    /'
  fi
  run_out=$(python3 "$up" --vault "$vault" run ${allow[@]+"${allow[@]}"} 2>&1) || run_rc=$?
  # The attention section is printed once, from the follow-up status below.
  printf '%s\n' "$run_out" | sed '/^needs a person (/,$d' | sed '/^[[:space:]]*$/d' | sed 's/^/  /' || true
  backup=$(printf '%s\n' "$run_out" | sed -n 's/^backup: //p' | head -1)
  if [ "$run_rc" -eq 1 ] && printf '%s\n' "$run_out" | grep -q 'need a person:'; then
    echo "  Left for you: the step(s) above that need a person. Run /gt:gt-upgrade to finish"
  elif [ "$run_rc" -eq 2 ] && printf '%s\n' "$run_out" | grep -q 'REFUSED'; then
    echo "⚠ Vault upgrades not applied — gt_upgrade refused (see above). Run /gt:gt-upgrade"
  elif [ "$run_rc" -ne 0 ]; then
    echo "⚠ Vault upgrade stopped (exit $run_rc): $(printf '%s\n' "$run_out" | sed '/^[[:space:]]*$/d' | tail -1)"
    [ -n "$backup" ] && echo "  The vault was backed up before anything changed: $backup"
    echo "  Run /gt:gt-upgrade to look"
  fi

  rc=0
  out=$(python3 "$up" status --vault "$vault" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "⚠ Vault upgrades: the follow-up check could not run (exit $rc) — run /gt:gt-upgrade to look"
  elif printf '%s\n' "$out" | grep -q 'nothing pending'; then
    echo "Vault upgrades: none pending"
  elif printf '%s\n' "$out" | grep -q 'pending step(s):'; then
    echo "⚠ Vault upgrades still pending:"
    printf '%s\n' "$out" | upgrade_pending_list
    echo "  Run /gt:gt-upgrade to finish"
  fi
  [ "$rc" -eq 0 ] && printf '%s\n' "$out" | upgrade_attention_list
  if [ -n "$(git -C "$vault" status --porcelain 2>/dev/null)" ]; then
    if [ "$ours" = yes ]; then
      echo "Uncommitted for your review: this install's vault file refreshes AND the applied upgrades (nothing was staged or committed)."
    else
      echo "Uncommitted for your review: the applied upgrades (nothing was staged or committed)."
    fi
    echo "Review and commit the vault: git -C \"$vault\" status"
  fi
  echo ""
}
apply_vault_upgrades || true

# The pre-write backup stays only when this run changed a file it holds (0.15.0).
if [ -n "$VAULT_PREWRITE_BACKUP" ] && [ -f "$VAULT_PREWRITE_BACKUP" ]; then
  python3 "$SRC/scripts/vault_refresh.py" prune --vault "$VAULT_PREWRITE_PATH" \
    --backup "$VAULT_PREWRITE_BACKUP" 2>/dev/null || true
fi

echo "Restart Claude Code to load the plugins."
