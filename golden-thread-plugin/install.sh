#!/usr/bin/env bash
set -euo pipefail
# No bytecode from any python this installer runs (0.15.0): __pycache__ left in the plugin
# cache and marketplace varied run to run, and running a vault tool dirtied the vault.
export PYTHONDONTWRITEBYTECODE=1

# ── Preflight ──────────────────────────────────────────────────────────────

# WINDOWS (Git Bash, 0.19.2): find a REAL interpreter once, before anything else runs (owner,
# 2026-10-03). gt does not ship or download a Python; it requires one that is installed.
# Every `python3` below then means that interpreter, through the function this defines.
#
# Three things break the plain `python3` the rest of this file calls, all seen on a
# Windows 11 VM:
#   * python.org's installer provides python.exe and py.exe, never python3.exe, so
#     `python3` resolves to %LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe -- the Store
#     stub, which prints "Python was not found" and exits 9009 (49 under bash).
#   * Anything under ...\WindowsApps\ does not count, a REAL Microsoft Store Python
#     included: its file-system virtualisation of AppData makes writes under the user
#     profile unreliable, and every file this installer writes is under the user profile.
#   * Native Windows Python writes CRLF. `$(...)` strips only the LAST newline, so a
#     multi-line answer kept a \r on every line but the last (`gt_report_card.py$'\r'`,
#     and `cp` failed). The function strips \r from stdout, so a value bash reads is the
#     value Python printed.
# macOS and Linux never enter this block: their `python3` is used exactly as before.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    GT_PYTHON="" _gt_found_store="" _gt_found_old=""
    _gt_py_probe='import sys; print(sys.version_info[0] * 100 + sys.version_info[1]); print(sys.executable)'
    _gt_try_python() {  # $1 = a candidate on PATH, rest = its fixed arguments ("-3" for py)
      local cand="$1" out ver exe; shift
      # Lower-cased with tr, not ${x,,}: install.sh must still PARSE on macOS's bash 3.2.
      case "$(printf '%s' "$cand" | tr '[:upper:]' '[:lower:]')" in */microsoft/windowsapps/*) _gt_found_store="$cand"; return 1 ;; esac
      out=$("$cand" "$@" -c "$_gt_py_probe" 2>/dev/null | tr -d '\r') || return 1
      ver=$(printf '%s\n' "$out" | sed -n 1p); exe=$(printf '%s\n' "$out" | sed -n 2p)
      [[ "$ver" =~ ^[0-9]+$ ]] && [ -n "$exe" ] || return 1
      # The py launcher (or a clean-looking PATH entry) can still land on a Store Python.
      case "$(printf '%s' "$exe" | tr '[:upper:]' '[:lower:]')" in *\\windowsapps\\*|*/windowsapps/*) _gt_found_store="$exe"; return 1 ;; esac
      if [ "$ver" -lt 308 ]; then _gt_found_old="$exe (3.$((ver % 100)))"; return 1; fi
      command -v cygpath >/dev/null 2>&1 && exe=$(cygpath -u "$exe")
      GT_PYTHON="$exe"
    }
    _gt_resolve_python() {
      local c
      for c in $(type -aP python3 2>/dev/null) $(type -aP python 2>/dev/null); do
        _gt_try_python "$c" && return 0
      done
      c=$(type -P py 2>/dev/null) && [ -n "$c" ] && _gt_try_python "$c" -3 && return 0
      return 1
    }
    _gt_ifs="$IFS"; IFS=$'\n'           # "C:/Program Files/..." is one candidate, not two
    _gt_resolve_python || true
    IFS="$_gt_ifs"
    if [ -z "$GT_PYTHON" ]; then
      echo "✗ No usable Python found. gt needs Python 3.8 or newer."
      [ -n "$_gt_found_store" ] && echo "  Found $_gt_found_store -- the Microsoft Store Python (or its \"Python was not found\" stub). The Microsoft Store Python does not count."
      [ -n "$_gt_found_old" ] && echo "  Found $_gt_found_old -- too old."
      echo "  Install Python 3.8 or newer from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
      echo "  reopen the terminal, and run the installer again."
      exit 1
    fi
    export GT_PYTHON
    # UTF-8 mode: piped, Windows Python encodes stdout as cp1252 and the first "⚠" or "→"
    # any script printed was a UnicodeEncodeError (vault_refresh, gt_daily, gt_upgrade).
    # It also makes open() read and write UTF-8 by default, as it does on macOS and Linux.
    export PYTHONUTF8=1
    # Unbuffered: stdout now goes through a pipe, and a block-buffered Python would hold a
    # progress line back until it exited.
    export PYTHONUNBUFFERED=1
    # An argument holding an apostrophe ("/c/Users/x/Sam's Projects/...") is one MSYS does NOT
    # convert to a Windows path for a native program (0.20.1), so Python was handed "/c/..."
    # and could not open it: an install from such a folder stopped at the version check.
    # cygpath converts exactly those; every other argument is MSYS's to convert, as before.
    python3() {
      local _a; local -a _v=()
      for _a in "$@"; do
        case "$_a" in /*\'*) _a=$(cygpath -m "$_a" 2>/dev/null || printf '%s' "$_a") ;; esac
        _v+=("$_a")
      done
      "$GT_PYTHON" ${_v[@]+"${_v[@]}"} | tr -d '\r'; return "${PIPESTATUS[0]}"
    }
    export -f python3
    # To stderr (0.20.1): a diagnostic, not output. On stdout it became the first line of
    # --list-plugins / --list-modules, which are machine-readable ("<dir> <version> <name>").
    echo "Python: $GT_PYTHON" >&2
    ;;
esac

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

# Commands shown to the PERSON, in the shell they ran this from (0.20.1, usability run M14).
# install.cmd sets GT_LAUNCHER=install.cmd: that person is in cmd.exe or PowerShell, where
# `python3` is the Microsoft Store stub, POSIX paths mean nothing and `./install.sh` does not
# run -- so hints there use the py launcher (or the interpreter's full path), Windows paths and
# install.cmd. Anywhere else: python3, the path as it is, and the installer by its full path.
if [ "${GT_LAUNCHER:-}" = install.cmd ] && command -v cygpath >/dev/null 2>&1; then
  if command -v py >/dev/null 2>&1; then U_PY="py -3"
  else U_PY="\"$(cygpath -w "${GT_PYTHON:-python}")\""; fi
  U_INSTALL="\"$(cygpath -w "$SCRIPT_DIR/install.cmd")\""
  upath() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
else
  U_PY="python3"
  U_INSTALL="bash \"$SCRIPT_DIR/install.sh\""
  upath() { printf '%s' "$1"; }
fi
export GT_U_INSTALL="$U_INSTALL"

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
# The PUBLIC build carries only the current release and the one before it (owner, 2026-09-14,
# and 2026-10-25: dev/public_build.py --dry-run lists what it drops; 2026-09-12 fifteen version
# directories were 9.6 MB of tree that nothing read). The maintainers' tree keeps older folders
# until they are pruned. Every earlier release is still in git, so rolling back further is
# two steps rather than one:
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
U_INSTALL = os.environ.get("GT_U_INSTALL") or "./install.sh"   # how the person runs it


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
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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
                tail = (" (module %s is off: %s --with %s)" % (off[name], U_INSTALL, off[name])
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
            line += " (module %s is off: %s --with %s)" % (off[m.group(1)], U_INSTALL,
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
        # so it is not this module's state -- the precedence above stands. The same holds for
        # "invalid" (0.21.0): an older gt's validator does not know keys a newer module adds
        # (`lockdown`), so it calls the NEWEST module.json invalid -- a release not installed.
        if isinstance(got, dict) and got.get("version") not in (None, m["version"]) \
                and str(got.get("reason", "")).startswith(("requires gt", "invalid")):
            got = None
        if isinstance(got, dict):              # module-states --detail
            if got.get("state") in ("on", "off"):
                state = got["state"]
            if str(got.get("reason", "")).startswith("invalid"):
                why, m["reason"] = "invalid", got["reason"]
            elif str(got.get("reason", "")).startswith("POSIX-only"):   # 0.20.1: lotr on Windows
                why, m["reason"] = "platform", got["reason"]
            elif str(got.get("reason", "")).startswith("needs Claude Code"):   # 0.21.2: the pane
                why, m["reason"] = "claude", got["reason"]
        elif got in ("on", "off"):
            state = got
        if m["admits"] is not True:
            state, why = "off", "requires"
        elif why in ("invalid", "platform", "claude"):
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
            "platform": "skipped: %s" % m.get("reason", "POSIX-only"),
            "claude": "skipped: %s" % m.get("reason", "needs a newer Claude Code")
                      .replace(" -- skipped", ""),
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
    print("Change with: %s --with NAME | --without NAME  (the choice is remembered)" % U_INSTALL)
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
        elif m["why"] == "platform":
            s += " (POSIX-only: not on Windows)"
        elif m["why"] == "claude":
            s += " (%s)" % m.get("reason", "needs a newer Claude Code").replace(" -- skipped", "")
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


def cmd_bundle(a):
    """bundle <root> everything|all -> one module NAME per line. `everything` is the gt-all
    bundle's dependencies (bundles/gt-all/.claude-plugin/plugin.json, mapped plugin -> module
    name); `all` is every module this tree ships. Newest release of each directory."""
    root, which = a[0], a[1]
    bymplug, byname_default = {}, {}
    for d in sorted(os.listdir(root)):
        dd = os.path.join(root, d)
        if not d.startswith("golden-thread-") or not os.path.isdir(dd):
            continue
        vers = [v for v in os.listdir(dd) if re.fullmatch(r"\d+\.\d+\.\d+", v)
                and os.path.isfile(os.path.join(dd, v, "module.json"))]
        if not vers:
            continue
        v = max(vers, key=lambda x: tuple(int(n) for n in x.split(".")))
        try:
            m = load(os.path.join(dd, v, "module.json"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and NAME.match(str(m.get("name", ""))):
            bymplug[m.get("plugin")] = m["name"]
            byname_default[m["name"]] = m.get("default")
    if which == "all":
        names = sorted(bymplug.values())
    else:
        try:
            deps = load(os.path.join(root, "bundles", "gt-all", ".claude-plugin",
                                     "plugin.json")).get("dependencies") or []
        except (OSError, ValueError, AttributeError):
            deps = []
        names = sorted(bymplug[x] for x in deps if x in bymplug)
        if not names:
            # no bundle file in this tree (an old zip): the same rule dev/plugins.py applies --
            # every module this tree ships (0.22.0: lotr, farm and workflow too)
            names = sorted(byname_default)
    for n in names:
        print(n)
    return 0


def cmd_external_market(a):
    """external-market <known_marketplaces.json> <name> <our-dir> <script-dir> -> prints
    `external` when a marketplace of this name that Claude Code manages (a GitHub clone, a git
    URL, or a directory other than ours) is registered, or when this installer runs from inside
    the marketplaces folder; else `local`. The local one is the directory marketplace this
    installer writes itself (the terminal route)."""
    known, name, ours, here = a
    real = os.path.realpath
    try:
        ent = load(known).get(name) or {}
    except (OSError, ValueError, AttributeError):
        ent = {}
    src = ent.get("source") or {}
    loc = ent.get("installLocation") or src.get("path") or ""
    inside = real(here).startswith(real(os.path.dirname(ours)) + os.sep)
    if inside:
        print("external")
    elif ent and (src.get("source") not in (None, "directory") or (loc and real(loc) != real(ours))):
        print("external")
    else:
        print("local")
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
            [sys.executable, os.path.join(src, "scripts", "gt_components.py"), "hookdir-scripts",
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


# ── --only NAME: one module, without the whole install (0.21.4) ────────────────────────────
def cmd_only_target(a):
    """only-target <root> <name> then <dir> <ver> <plugin> ... of every discovered plugin
    (index 0 is gt). NAME may be a module name (pane), its plugin (gt-pane), one of its
    skills (gt-wiki-ingest) or a slash command (/gt-wiki:gt-wiki-ingest): a skill is
    installed as the module that ships it -- the module is the smallest unit gt installs.
    Prints "<index>\t<module>\t<plugin>\t<how>"; exit 1 with the reason when refused."""
    root, raw, rest = a[0], a[1], a[2:]
    trip = list(zip(rest[0::3], rest[1::3], rest[2::3]))
    name = raw.strip().lstrip("/")
    plug_part, _, skill_part = name.partition(":")
    mods = []                                   # (index, module, plugin, vdir)
    for i, (d, ver, plugin) in enumerate(trip):
        vdir = os.path.join(root, d, ver)
        try:
            m = load(os.path.join(vdir, "module.json"))
            mname = m.get("name") if isinstance(m, dict) else None
        except (OSError, ValueError):
            mname = None
        mods.append((i, mname, plugin, vdir))
    every = ", ".join(sorted(m for _i, m, _p, _v in mods if m))

    def refuse(msg):
        print("✗ --only %s: %s. Nothing has been installed." % (raw, msg))
        print("  Modules: %s" % (every or "none in this tree"))
        return 1

    def pick(i, how):
        _i, mname, plugin, _v = mods[i]
        if i == 0 or plugin == "gt":
            return refuse("that is part of gt itself, not a module; the skills of gt are "
                          "installed and updated by the full install, %s" % U_INSTALL)
        if not mname:
            return refuse("%s is not a module (no module.json); the full install keeps it"
                          % plugin)
        print("%d\t%s\t%s\t%s" % (i, mname, plugin, how))
        return 0

    if name == "gt-all":
        return refuse("gt-all is the bundle of every module, not one; use the full install")
    if not name or name == "gt":
        return refuse("that is gt itself, not a module; use the full install, %s" % U_INSTALL)
    if skill_part:                              # /plugin:skill
        for i, _m, plugin, vdir in mods:
            if plugin == plug_part:
                if not os.path.isfile(os.path.join(vdir, "skills", skill_part, "SKILL.md")):
                    return refuse("%s ships no skill %s" % (plugin, skill_part))
                return pick(i, "skill /%s:%s" % (plugin, skill_part))
        return refuse("no plugin %s in this tree" % plug_part)
    for i, mname, plugin, _v in mods:
        if name == mname:
            return pick(i, "module")
    for i, mname, plugin, _v in mods:
        if name == plugin:
            return pick(i, "plugin")
    owners = [i for i, _m, _p, vdir in mods
              if os.path.isfile(os.path.join(vdir, "skills", name, "SKILL.md"))]
    if len(owners) == 1:
        return pick(owners[0], "skill /%s:%s" % (mods[owners[0]][2], name))
    if len(owners) > 1:
        return refuse("more than one plugin ships a skill called %s (%s); name it as "
                      "/plugin:skill" % (name, ", ".join(mods[i][2] for i in owners)))
    return refuse("no module, plugin or skill of that name in this tree")


def cmd_only_installed(a):
    """only-installed <installed_plugins.json> <cache-root> then plugin names -> one line per
    name: "<name>\t<version or ->\t<yes when its cache dir is there>"."""
    installed, cache_root, names = a[0], a[1], a[2:]
    try:
        plugins = load(installed).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        plugins = {}
    for n in names:
        e = plugins.get("%s@%s" % (n, MARKET))
        e = e[0] if isinstance(e, list) and e and isinstance(e[0], dict) else {}
        v = e.get("version") or "-"
        cached = "yes" if v != "-" and os.path.isdir(os.path.join(cache_root, n, v)) else "no"
        print("%s\t%s\t%s" % (n, v, cached))
    return 0


def cmd_only_freeze(a):
    """only-freeze <modules.json> <target> <installed.tsv> <states.tsv>: every module but the target keeps
    the state it has ON THIS MACHINE. One that resolves on but is not installed (a default-on
    module this machine never had) is set off for this run: not installed, and with nothing of
    it present nothing is removed; its recorded choice is untouched, so the next full install
    adds it. One that is installed but resolves off would be REMOVED by any install, so --only
    refuses (exit 1) instead of doing that unasked."""
    path, target, tsv, states = a
    mods = load(path)
    have = {}
    for line in open(tsv, encoding="utf-8"):
        f = line.rstrip("\n").split("\t")
        if len(f) >= 2:
            have[f[0]] = f[1]
    bad = []
    for m in mods:
        if m["name"] == target:
            continue
        present = have.get(m["plugin"], "-") != "-"
        if m["state"] == "on" and not present:
            m["state"], m["why"] = "off", "only"
            print("Left out: module %s, not on this machine (the full install adds it)" % m["name"])
        elif m["state"] == "off" and present:
            bad.append(m)
    if bad:
        for m in bad:
            print("✗ module %s (%s) is installed here but resolves OFF (%s), so any install "
                  "would remove it." % (m["name"], m["plugin"], m.get("why") or "choice"))
        print("  --only changes one module and nothing else. Settle that first: %s --only %s "
              "keeps it, the full install removes it. Nothing has been installed."
              % (U_INSTALL, bad[0]["name"]))
        return 1
    atomic(path, mods)
    with open(states, "w", encoding="utf-8", newline="\n") as fh:
        for m in mods:
            fh.write("%s\t%s\t%s\t%s\n" % (m["name"], m["state"], m["why"], m["plugin"]))
    return 0


def _sha(p):
    import hashlib
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _body(p):
    """A skill or agent file without its frontmatter: the model policy rewrites the model and
    effort lines of the INSTALLED copy only, so that copy is compared by its body."""
    t = open(p, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end != -1:
            return t[end + 4:]
    return t


COPIED_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands", "hooks", "demo",
               "packs", "agents", "workflows", "help", "walkthroughs", "docs", "tests")
COPIED_FILES = ("module.json", "MANIFEST.json", "retired.json", "SPEC.md")


def _tree(root):
    out = set()
    for d in COPIED_DIRS:
        for dp, dn, fn in os.walk(os.path.join(root, d)):
            dn[:] = [x for x in dn if x != "__pycache__"]
            for f in fn:
                if f.endswith(".pyc") or f == ".DS_Store":
                    continue
                out.add(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
    for f in COPIED_FILES:
        if os.path.isfile(os.path.join(root, f)):
            out.add(f)
    return out


def cmd_only_verify(a):
    """only-verify <modules.json> <module> <home> <cache-root> <marketplace or -> <installed>
    <settings> <gt-src> -> the self-proof of one module: PASS/FAIL rows, exit 1 on any FAIL.
    It proves what a full install lays down for that module: the cache is the release (nothing
    missing, changed or left over), it is the only cached version, it is registered, enabled
    and in the marketplace, its hook-dir scripts are the release copies, its hooks are wired,
    and the choice is recorded so the next full install keeps it."""
    modjson, target, home, cache_root, market, installed, settings, src = a
    m = next((x for x in load(modjson) if x["name"] == target), None)
    rows = []

    def row(ok, what, detail):
        rows.append((ok, what, detail))

    if m is None:
        print("FAIL  module       %s is not in this tree" % target)
        return 1
    plugin, ver = m["plugin"], m["version"]
    vsrc = os.path.join(os.path.dirname(os.path.dirname(src)), m["dir"], ver)
    cache = os.path.join(cache_root, plugin, ver)
    key = "%s@%s" % (plugin, MARKET)
    if not os.path.isdir(cache):
        row(False, "files", "no cache at %s" % cache)
    else:
        want, have = _tree(vsrc), _tree(cache)
        extra_top = sorted(x for x in os.listdir(cache)
                           if x not in COPIED_DIRS + COPIED_FILES + ("__pycache__",)
                           and not x.startswith("."))
        missing, extra, changed = sorted(want - have), sorted(have - want), []
        for rel in sorted(want & have):
            s, c = os.path.join(vsrc, rel), os.path.join(cache, rel)
            if rel.endswith(".md") and rel.split("/")[0] in ("skills", "agents"):
                if _body(s) != _body(c):
                    changed.append(rel)
            elif _sha(s) != _sha(c):
                changed.append(rel)
        probs = []
        if missing:
            probs.append("missing: %s" % ", ".join(missing[:5]))
        if changed:
            probs.append("differs from the release: %s" % ", ".join(changed[:5]))
        if extra or extra_top:
            probs.append("left over: %s" % ", ".join((extra + extra_top)[:5]))
        row(not probs, "files", "; ".join(probs) or "%d file(s), identical to the release"
            % len(want))
    try:
        others = sorted(v for v in os.listdir(os.path.join(cache_root, plugin))
                        if v != ver and not v.startswith("."))
    except OSError:
        others = []
    row(not others, "one-version", ("superseded cache left: %s" % ", ".join(others)) if others
        else "only %s is cached" % ver)
    try:
        e = (load(installed).get("plugins") or {}).get(key)
        e = e[0] if isinstance(e, list) and e and isinstance(e[0], dict) else {}
    except (OSError, ValueError, AttributeError):
        e = {}
    ip = e.get("installPath") or ""
    row(e.get("version") == ver and bool(ip) and os.path.normcase(os.path.normpath(ip))
        == os.path.normcase(os.path.normpath(cache)), "registered",
        "installed_plugins.json %s: %s" % (key, e.get("version")))
    try:
        en = (load(settings).get("enabledPlugins") or {}).get(key)
    except (OSError, ValueError, AttributeError):
        en = None
    row(en is True, "enabled", "settings.json enabledPlugins %s = %s" % (key, en))
    if market != "-":
        try:
            names = [p.get("name") for p in load(os.path.join(
                market, ".claude-plugin", "marketplace.json"))["plugins"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            names = []
        pj = os.path.join(market, "plugins", plugin, ".claude-plugin", "plugin.json")
        same = os.path.isfile(pj) and _sha(pj) == _sha(
            os.path.join(vsrc, ".claude-plugin", "plugin.json"))
        probs = []
        if plugin not in names:
            probs.append("not listed in marketplace.json")
        if not same:
            probs.append("its marketplace plugin.json is not the release copy")
        row(not probs, "marketplace", "; ".join(probs) or "listed and populated")
    hooks = os.path.join(home, ".claude", "golden-thread", "hooks")
    srcs = list(m["hookdir_scripts"]) + [h["src"] for h in m["hook_scripts"] if h["src"]]
    bad = sorted({os.path.basename(p) for p in srcs
                  if not os.path.isfile(os.path.join(hooks, os.path.basename(p)))
                  or _sha(p) != _sha(os.path.join(hooks, os.path.basename(p)))})
    if srcs:
        row(not bad, "hook-scripts", ("not the release copy: %s" % ", ".join(bad)) if bad
            else "%d script(s) in the hooks dir match the release" % len(set(srcs)))
    scripts = {h["script"] for h in m["hook_scripts"]}
    if scripts:
        try:
            r = subprocess.run([sys.executable, os.path.join(src, "scripts", "gt_components.py"),
                                "wiring", src, "--home", home], capture_output=True, text=True,
                               timeout=120)
            unwired = [ln.strip() for ln in r.stdout.splitlines()
                       if set(ln.split()) & scripts]
            row(not unwired, "hooks", "; ".join(unwired) if unwired
                else "%d hook(s) wired" % len(m["hook_scripts"]))
        except (OSError, subprocess.SubprocessError) as exc:
            row(False, "hooks", "could not run the wiring check: %s" % exc)
    ch = read_choices(home).get(target)
    row(ch == "on" or (ch is None and m.get("default") == "on"), "choice",
        "install-choices.json %s = %s" % (target, ch) if ch is not None
        else "no recorded choice; on by default" if m.get("default") == "on"
        else "no recorded choice, and %s is off by default: the next full install would "
             "remove it" % target)
    for ok, what, detail in rows:
        print("%-4s  %-12s %s" % ("PASS" if ok else "FAIL", what, detail))
    return 0 if all(ok for ok, _w, _d in rows) else 1


CMDS = {"scan": cmd_scan, "resolve": cmd_resolve, "record": cmd_record, "list": cmd_list,
        "only-target": cmd_only_target, "only-installed": cmd_only_installed,
        "only-freeze": cmd_only_freeze, "only-verify": cmd_only_verify,
        "summary": cmd_summary, "onfiles": cmd_onfiles, "names": cmd_names,
        "bundle": cmd_bundle, "external-market": cmd_external_market,
        "remove": cmd_remove, "pick": cmd_pick, "restore-cron": cmd_restore_cron,
        "moved": cmd_moved, "moved-annotate": cmd_moved_annotate,
        "newer-owned": cmd_newer_owned}
sys.exit(CMDS[sys.argv[1]](sys.argv[2:]))
PYEOF
)
if [ -n "${GT_PYTHON:-}" ]; then
  # Windows (0.20.1): MODPY is ~28 KB, and MSYS escapes every quote in it on the way to a native
  # process, so `-c "$MODPY"` plus a few long paths passed CreateProcess's 32767-character limit
  # ("Argument list too long", exit 126). There the code goes in a file, which the -c stub execs:
  # sys.argv and sys.path are what -c gives. $$ is the same in every subshell, so the one file
  # serves the <(...) and $(...) calls too; gt_on_exit removes it.
  _GT_MODPY_FILE="${TMPDIR:-/tmp}/gt-modpy.$$.py"
  printf '%s\n' "$MODPY" > "$_GT_MODPY_FILE"
  trap 'rm -f "$_GT_MODPY_FILE"' EXIT
  modpy() {
    python3 -c "import sys; exec(compile(open(sys.argv.pop(1), encoding='utf-8').read(), 'install.sh MODPY', 'exec'))" \
      "$_GT_MODPY_FILE" "$@"
  }
else
  modpy() { python3 -c "$MODPY" "$@"; }
fi

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
REQUIRE_CHECKSUM="${GT_REQUIRE_CHECKSUM:+yes}"; REQUIRE_CHECKSUM="${REQUIRE_CHECKSUM:-no}"
LIST_MODULES=no
MODULE_FLAGS=()          # "with:NAME" / "without:NAME", in the order given
MODEL_PROFILE=""         # --model-profile average|very-high|inherit (0.19.1)
LOCKDOWN=""              # --lockdown strict|trust-local|trust-network|insecure|paranoid (0.20.5; paranoid 0.21.0)
PERSONA=""               # --persona a,b  (0.21.0): installed persona names, or none
PERSONA_SET=no           # yes once --persona was given (an empty/none value is still a choice)
SANDBOX_MODE=""          # --sandbox-mode off|on (0.21.0, item #33: every install question has a flag)
PARANOID=""              # --paranoid off|on (0.21.0: not asked; the paranoid level answers it)
EXPIRY_GUARD=""          # --expiry-guard auto|warn-only|off
TOKEN_PROFILE=""         # --token-profile minimum|standard|maximum (0.21.0, item #43; `standard` was `calibrated` before 0.22.0)
PERSONA_ALIGNED=""       # --persona-aligned on|off (0.21.0, item #45)
REPLY_LENGTH=""          # --reply-length normal|brief|terse (0.21.0, item #39)
PARALLEL_MAX=""          # --parallel-max auto|N (0.22.0, phase 129)
OPEN_GUIDE=""            # --open-guide yes|no: open the Getting started page at the end (item #36)
OBSIDIAN_PLUGINS=""      # --obsidian-plugins yes|no: download the pinned Terminal and Claudian plugins (0.22.0, phase 89b; off by default)
TELEMETRY=""             # --telemetry off|local (0.22.0, phase 107b: off by default; nothing is sent in this release)
STATUSLINE=""            # --statusline yes|no
AST_GREP=""              # --ast-grep yes|no
ALL_MODULES=no           # --all-modules (0.22.0): every module the gt-all bundle names, in one flag
CORE_ONLY=no             # --core-only (0.22.0): gt alone, every module off
STARTER=no               # --starter (0.22.0, phase 116): core only, three questions, nothing at machine level
ANSWERS_FILE=""          # --answers FILE: the JSON that `--questions --json` described, filled in
ACCEPT_DEFAULTS=no       # --accept-defaults: the explicit opt-out of the exit-5 stop (CI)
QUESTIONS=no             # --questions: print every install question, install nothing
JSON_OUT=no              # --json (with --questions)
UNINSTALL=no             # --uninstall: run gt_uninstall.py instead (0.20.1)
UNINSTALL_ARGS=()        # what is passed through to it: --check/--dry-run, --yes, ...
ONLY=""                  # --only NAME (0.21.4): add or update one module, nothing else
ONLY_CHECK=no            # --only NAME --check / --dry-run: say what it would do, change nothing
VERBOSE=no               # --verbose: --only prints the installer's whole log, not just its own lines
ORIG_ARGS=("$@")         # kept for the re-run after a migration changes a module choice
POSITIONAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force-manifest-mismatch) FORCE_MANIFEST=yes; shift ;;
    --list-plugins) LIST_PLUGINS=yes; shift ;;
    --list-modules) LIST_MODULES=yes; shift ;;
    --with|--without)
      if [ -z "${2:-}" ] || [ "${2#-}" != "$2" ]; then
        echo "✗ $1 needs a module name (see $U_INSTALL --list-modules)"; exit 1
      fi
      MODULE_FLAGS+=("${1#--}:$2"); shift 2 ;;
    --with=*)    MODULE_FLAGS+=("with:${1#--with=}"); shift ;;
    --without=*) MODULE_FLAGS+=("without:${1#--without=}"); shift ;;
    --all-modules) ALL_MODULES=yes; shift ;;
    --core-only)   CORE_ONLY=yes; shift ;;
    --starter)     STARTER=yes; shift ;;
    --vault)    VAULT_ARG="${2:-}"; shift 2 || true ;;
    --vault=*)  VAULT_ARG="${1#--vault=}"; shift ;;
    --no-vault) NO_VAULT=yes; shift ;;
    --model-profile)   MODEL_PROFILE="${2:-}"; shift 2 || true ;;
    --model-profile=*) MODEL_PROFILE="${1#--model-profile=}"; shift ;;
    --lockdown)   LOCKDOWN="${2:-}"; shift 2 || true ;;
    --lockdown=*) LOCKDOWN="${1#--lockdown=}"; shift ;;
    --persona)    PERSONA="${2:-}"; PERSONA_SET=yes; shift 2 || true ;;
    --persona=*)  PERSONA="${1#--persona=}"; PERSONA_SET=yes; shift ;;
    --sandbox-mode)   SANDBOX_MODE="${2:-}"; shift 2 || true ;;
    --sandbox-mode=*) SANDBOX_MODE="${1#--sandbox-mode=}"; shift ;;
    --paranoid)   PARANOID="${2:-}"; shift 2 || true ;;
    --paranoid=*) PARANOID="${1#--paranoid=}"; shift ;;
    --expiry-guard)   EXPIRY_GUARD="${2:-}"; shift 2 || true ;;
    --expiry-guard=*) EXPIRY_GUARD="${1#--expiry-guard=}"; shift ;;
    --token-profile)   TOKEN_PROFILE="${2:-}"; shift 2 || true ;;
    --token-profile=*) TOKEN_PROFILE="${1#--token-profile=}"; shift ;;
    --persona-aligned)   PERSONA_ALIGNED="${2:-}"; shift 2 || true ;;
    --persona-aligned=*) PERSONA_ALIGNED="${1#--persona-aligned=}"; shift ;;
    --reply-length)   REPLY_LENGTH="${2:-}"; shift 2 || true ;;
    --reply-length=*) REPLY_LENGTH="${1#--reply-length=}"; shift ;;
    --parallel-max)   PARALLEL_MAX="${2:-}"; shift 2 || true ;;
    --parallel-max=*) PARALLEL_MAX="${1#--parallel-max=}"; shift ;;
    --open-guide)   OPEN_GUIDE="${2:-}"; shift 2 || true ;;
    --open-guide=*) OPEN_GUIDE="${1#--open-guide=}"; shift ;;
    --obsidian-plugins)   OBSIDIAN_PLUGINS="${2:-}"; shift 2 || true ;;
    --obsidian-plugins=*) OBSIDIAN_PLUGINS="${1#--obsidian-plugins=}"; shift ;;
    --telemetry)   TELEMETRY="${2:-}"; shift 2 || true ;;
    --telemetry=*) TELEMETRY="${1#--telemetry=}"; shift ;;
    --statusline)   STATUSLINE="${2:-}"; shift 2 || true ;;
    --statusline=*) STATUSLINE="${1#--statusline=}"; shift ;;
    --ast-grep)   AST_GREP="${2:-}"; shift 2 || true ;;
    --ast-grep=*) AST_GREP="${1#--ast-grep=}"; shift ;;
    --answers)    ANSWERS_FILE="${2:-}"; shift 2 || true ;;
    --answers=*)  ANSWERS_FILE="${1#--answers=}"; shift ;;
    --accept-defaults) ACCEPT_DEFAULTS=yes; shift ;;
    --questions)  QUESTIONS=yes; shift ;;
    --json)       JSON_OUT=yes; UNINSTALL_ARGS+=("$1"); shift ;;
    --require-checksum) REQUIRE_CHECKSUM=yes; shift ;;
    --uninstall) UNINSTALL=yes; shift ;;
    --verbose) VERBOSE=yes; shift ;;
    --only)    ONLY="${2:-}"; [ -n "$ONLY" ] || { echo "✗ --only needs a module, plugin or skill name (see $U_INSTALL --list-modules)"; exit 1; }; shift 2 ;;
    --only=*)  ONLY="${1#--only=}"; [ -n "$ONLY" ] || { echo "✗ --only needs a module, plugin or skill name"; exit 1; }; shift ;;
    --check|--dry-run|--yes|-y|--keep-vault-config|--discard-queued|--purge-backups|--purge-lotr)
      UNINSTALL_ARGS+=("$1"); shift ;;
    --hook-mode)   GT_HOOK_MODE="${2:-}"; shift 2 || true ;;
    --hook-mode=*) GT_HOOK_MODE="${1#--hook-mode=}"; shift ;;
    -h|--help)
      if [ "${2:-}" = security ]; then
        # owner 2026-10-10: every security setting, what it does and how to change each. Read
        # from this tree's settings registry (gt_settings.py security --full), so it cannot drift.
        _gt_set="$SCRIPT_DIR/golden-thread/$(latest_version "$SCRIPT_DIR/golden-thread")/scripts/gt_settings.py"
        echo "install.sh --help security"
        python3 -B "$_gt_set" security --full
        exit $?
      fi
      # The module list is READ from this tree (0.20.1: a typed list missed lotr, usage and
      # visualize). Every <dir>/<newest>/module.json's "name".
      _gt_mods=$(for _d in "$SCRIPT_DIR"/*/; do
                   _v=$(latest_version "$_d"); [ -n "$_v" ] || continue
                   [ -f "$_d/$_v/module.json" ] || continue
                   python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("name") or "")' \
                     "$_d/$_v/module.json" 2>/dev/null || true
                 done | sed '/^$/d' | LC_ALL=C sort | tr '\n' ',' | sed 's/,$//; s/,/, /g')
      sed "s/__GT_MODULE_LIST__/${_gt_mods:-none}/" <<'USAGE'
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
  ./install.sh --require-checksum     REFUSE to install unless every file matches the tree's
                                      SHA256SUMS (see below); also GT_REQUIRE_CHECKSUM=1
  GT_INSTALL_FROM_SHARED=1 ./install.sh  install from the shared transfer copy (gt-src) anyway;
                                      without it, a tree with SOURCE.json at its root is
                                      refused with exit 9 (copy it with copygt.sh first)
  ./install.sh --list-plugins         print "<dir> <version> <name>" for every plugin
                                      this tree ships, install nothing
  ./install.sh --with NAME            install module NAME (repeatable); remembered
  ./install.sh --without NAME         remove module NAME and keep it off (repeatable);
                                      remembered in ~/.claude/golden-thread/install-choices.json
  ./install.sh --starter              the small install for a person who is not a power user: core
                                      only, asks the notes location, the security level and the
                                      model size (nothing else), and adds nothing at machine level
                                      (no scheduled job, credential helper, sandbox entry or git
                                      hooks path). Undo: ./install.sh --uninstall
  ./install.sh --all-modules          install gt and EVERY module the gt-all bundle names (wiki, flow,
                                      usage, visualize, report-card, watch, demo, pane, lotr, farm,
                                      workflow, recall); remembered. Installing a module turns none of its
                                      behaviour on.
                                      At a terminal, a new install asks this first: everything /
                                      let me choose / core only
  ./install.sh --core-only            install gt alone and keep every module off; remembered
  ./install.sh --list-modules         print each module, its state and why, install nothing
  ./install.sh --only NAME            add or update ONE module and nothing else: no questions, the
                                      vault untouched, gt and the other modules left as they are.
                                      NAME is a module (pane), its plugin (gt-pane) or one of its
                                      skills (gt-wiki-ingest); remembered like --with NAME. Ends with
                                      a self-proof of that module; a failure is rolled back. Needs
                                      gt installed at this tree's release (else: the full install)
  ./install.sh --only NAME --check    say what --only would do, change nothing: exit 0 installed,
                                      current and complete; 3 it would install, update or repair
  ./install.sh --only NAME --verbose  the same, printing the installer's whole log (by default
                                      --only shows the module's lines and its self-proof)
  ./install.sh --model-profile P      the model and effort each skill runs at: average (a new
                                      install's default), very-high, or inherit; remembered
  ./install.sh --lockdown L           how much Claude may run without asking: strict (the
                                      default), trust-local, trust-network, insecure, or
                                      paranoid (strict plus locks on gt's own code); asked
                                      at a terminal when not given (gt_lockdown.py table)
  ./install.sh --paranoid on|off      the paranoid switch on its own, e.g. trust-local plus
                                      paranoid; not asked (the paranoid level answers it)
  ./install.sh --questions [--json]   print every install question (security level first), with
                                      what each choice means; install nothing. The answers are
                                      flags: --lockdown, --sandbox-mode, --paranoid,
                                      --token-profile, --model-profile, --persona, --persona-aligned, --reply-length,
                                      --expiry-guard, --parallel-max, --statusline, --ast-grep, --obsidian-plugins, --telemetry, --open-guide;
                                      or --answers FILE (the JSON back)
  ./install.sh --accept-defaults      NOT at a terminal, a new install stops with exit 5 until the
                                      security questions are answered; this takes the defaults on
                                      purpose (CI), and the summary says they were accepted
  ./install.sh --uninstall --check    list everything gt put on this machine; change nothing
  ./install.sh --uninstall            remove it (asks once; --yes when not at a terminal),
                                      then re-check that nothing is left. The vault is never
                                      touched. Also: --keep-vault-config, --discard-queued,
                                      --purge-backups, --purge-lotr (gt_uninstall.py --help)
  ./install.sh --hook-mode M          plugin: the hooks ship in the plugins' hooks/hooks.json
                                      and settings.json is not written (the default from
                                      Claude Code 2.1.136); settings: wire settings.json as
                                      before (the default below it, or when the version cannot
                                      be read). Also GT_HOOK_MODE.
  ./install.sh --help                 this text
  ./install.sh --help security        every security setting: what it does and how to change it
                                      (long: pipe it to less)

A tree published by dev/sync-gt-src.sh carries SHA256SUMS at the repository root. Before
anything is installed, every file listed there is checked, and a file missing, changed, or not
where the list puts it is NAMED. By default the install then continues -- someone who simply
downloaded the repository is never blocked by it. With --require-checksum a mismatch (or no
SHA256SUMS, or no hash tool) stops the install with exit 8 before anything is copied: use it
on the machine that receives a publish. A tree with no SHA256SUMS installs as it stands.

Modules are the optional plugins beside gt (in this tree: __GT_MODULE_LIST__). State is, in
order: --with/--without in this run, your recorded choice, the module's default. gt
itself is not a module and cannot be removed this way.

A vault is a plain folder of markdown where your memory lives. The Core-rule
enforcement hooks are wired AGAINST a vault, so an install with no vault leaves them
present but inert. --vault finishes the job in one command.

With no vault and no --vault:
  * at a terminal, you are asked where the vault should go (~/GoldenThread offered);
  * otherwise (an agent, a pipe, CI) the vault is set up at ~/GoldenThread
    (%USERPROFILE%\GoldenThread on Windows), and the path is printed on stderr (0.21.4).
    Only if that cannot be done does the install stop with exit 4 and say what it needs.
A vault path in Documents or on a synced or shared drive (iCloud, Dropbox, OneDrive, Google
Drive, Box, a network share) is warned about, with the reason; you can keep it.

Exit 5 (0.21.0): not at a terminal, a NEW install with a security question unanswered stops
BEFORE installing anything and names the flags that answer it. An install run by Claude should
ask the person first (install.sh --questions --json). Over an existing gt, an unanswered
question keeps the current value. At a terminal every unanswered question is asked.

If the release ships machine migrations, they run after the plugin files and hooks are
in place; a failed one stops the install with exit 7.

Any failure after the installer starts writing (every non-zero exit except 4) is ROLLED
BACK (0.20.1): settings.json, the plugin registrations, marketplace and cache,
~/.claude/golden-thread (not its backups/) and vault-config.json are put back as they were.
The vault is not rolled back; it has its own pre-write backup.

With a vault configured, the install ends by running the installed
`gt_doctor.py post-install --stage install` (the release gate) and prints its table. Rows that
cannot be true until /gt:gt-upgrade has run show PENDING. Any FAIL row stops with exit 9
(and is rolled back); the full gate, after the upgrade and a restart, is
  python3 ~/.claude/golden-thread/hooks/gt_doctor.py post-install --vault <vault>

Environment: GT_VAULT (same as --vault), GT_VERSION (same as the version argument).
USAGE
      exit 0 ;;
    -*) echo "unknown option: $1"; echo "try: install.sh [version] [--vault <path>] [--no-vault] [--with|--without NAME] [--list-modules] [--uninstall [--check]]"; exit 1 ;;
    *)  POSITIONAL="$1"; shift ;;
  esac
done
# A bad --lockdown stops here, before anything is installed (0.20.5).
case "$LOCKDOWN" in
  ""|strict|trust-local|trust-network|insecure|paranoid) ;;
  *) echo "✗ --lockdown: '$LOCKDOWN' is not one of strict, trust-local, trust-network, insecure, paranoid"
     exit 1 ;;
esac
case "${GT_HOOK_MODE:-}" in
  ""|plugin|settings) ;;
  *) echo "✗ --hook-mode: ${GT_HOOK_MODE} is not plugin or settings"; exit 1 ;;
esac
# --only NAME (0.21.4): one module, without the whole install. It changes that module and
# nothing else, so every flag that would change something else is refused here, before anything
# is read or written: the questions, the vault, other modules, a pinned release.
if [ -n "$ONLY" ]; then
  _gt_only_bad=""
  for _a in ${UNINSTALL_ARGS[@]+"${UNINSTALL_ARGS[@]}"}; do
    case "$_a" in
      --check|--dry-run) ONLY_CHECK=yes ;;
      *) _gt_only_bad="$_gt_only_bad $_a" ;;
    esac
  done
  UNINSTALL_ARGS=()
  [ "$UNINSTALL" = yes ] && _gt_only_bad="$_gt_only_bad --uninstall"
  [ "$QUESTIONS" = yes ] && _gt_only_bad="$_gt_only_bad --questions"
  [ "$LIST_MODULES" = yes ] && _gt_only_bad="$_gt_only_bad --list-modules"
  [ "$LIST_PLUGINS" = yes ] && _gt_only_bad="$_gt_only_bad --list-plugins"
  [ "${#MODULE_FLAGS[@]}" -gt 0 ] && _gt_only_bad="$_gt_only_bad --with/--without"
  [ "$ALL_MODULES" = yes ] && _gt_only_bad="$_gt_only_bad --all-modules"
  [ "$CORE_ONLY" = yes ] && _gt_only_bad="$_gt_only_bad --core-only"
  [ "$STARTER" = yes ] && _gt_only_bad="$_gt_only_bad --starter"
  [ -n "$POSITIONAL" ] && _gt_only_bad="$_gt_only_bad $POSITIONAL"
  [ "$NO_VAULT" = yes ] && _gt_only_bad="$_gt_only_bad --no-vault"
  [ -n "${VAULT_ARG:-}" ] && [ "${VAULT_ARG:-}" != "${GT_VAULT:-}" ] && _gt_only_bad="$_gt_only_bad --vault"
  [ -n "$ANSWERS_FILE" ] && _gt_only_bad="$_gt_only_bad --answers"
  for _v in "$LOCKDOWN" "$SANDBOX_MODE" "$PARANOID" "$MODEL_PROFILE" "$EXPIRY_GUARD" "$TOKEN_PROFILE" \
            "$PERSONA_ALIGNED" "$REPLY_LENGTH" "$PARALLEL_MAX" "$OPEN_GUIDE" "$STATUSLINE" "$AST_GREP" "$OBSIDIAN_PLUGINS" "$TELEMETRY"; do
    if [ -n "$_v" ]; then _gt_only_bad="$_gt_only_bad (a setting)"; break; fi
  done
  [ "$PERSONA_SET" = yes ] && _gt_only_bad="$_gt_only_bad --persona"
  if [ -n "$_gt_only_bad" ]; then
    echo "✗ --only adds or updates one module and changes nothing else; it does not combine with:$_gt_only_bad"
    echo "  Settings: gt_settings.py set KEY VALUE. Everything else: the full install, $U_INSTALL"
    exit 1
  fi
  VAULT_ARG=""                 # GT_VAULT in the environment: --only never touches the vault
  ACCEPT_DEFAULTS=no           # an installed machine keeps its answers; nothing is defaulted over them
fi
if [ "$LIST_PLUGINS" = yes ]; then
  discover_plugins "$SCRIPT_DIR" | tr '\t' ' '
  exit 0
fi
# --uninstall (0.20.1, usability run M15): one validated command instead of a paragraph of
# manual steps. gt_uninstall.py does the work -- the newest release's copy in this tree, else
# the installed one -- with --check for a dry run, backups, a summary and a re-scan.
if [ "$UNINSTALL" = yes ]; then
  _gt_un="$SCRIPT_DIR/golden-thread/$(latest_version "$SCRIPT_DIR/golden-thread")/scripts/gt_uninstall.py"
  [ -f "$_gt_un" ] || _gt_un="$HOME/.claude/golden-thread/hooks/gt_uninstall.py"
  if [ ! -f "$_gt_un" ]; then
    echo "✗ gt_uninstall.py was not found in this tree or in ~/.claude/golden-thread/hooks."
    exit 1
  fi
  _gt_rc=0
  python3 "$_gt_un" ${UNINSTALL_ARGS[@]+"${UNINSTALL_ARGS[@]}"} || _gt_rc=$?
  exit "$_gt_rc"
fi
if [ "${#UNINSTALL_ARGS[@]}" -gt 0 ] && [ "$QUESTIONS" != yes ]; then
  echo "✗ ${UNINSTALL_ARGS[0]} is an --uninstall option; for an install it means nothing."
  echo "  try: $U_INSTALL --uninstall ${UNINSTALL_ARGS[*]}"
  exit 1
fi

# ── Checksum: confirm the tree is the one that was published (0.17.3) ─────────
#
# Owner, 2026-09-28: "I want the checksum to be automatic ... that way we know all the files
# are there and in the right places" -- and, the same hour, "someone who downloads it from
# github [must not] have an issue". Both hold: dev/sync-gt-src.sh writes SHA256SUMS at the
# REPOSITORY root (one level above this script since gt-src took the repo layout), hashed
# from the commit, and every path in it is checked here before anything is copied. A mismatch
# is NAMED, loudly. It only STOPS the install under --require-checksum (GT_REQUIRE_CHECKSUM=1),
# which the build note tells the receiving machine to use. Without it a stranger's download is
# never refused -- a later commit to the receiving repo, a CRLF checkout on Windows or a
# missing hash tool would otherwise turn an honest install into exit 8. Files present but NOT
# listed are named, never refused: the receiving repository may hold its own.
#
# Every conditional is an `if`: this script runs under set -e, where a bare `a && b` whose
# left side is false aborts the install (the 0.17.1 installer bug).
# ── Feedback: never silent (owner, 2026-09-29) ─────────────────────────────────
# An install over a 0.9.4 vault sat with no output through its vault work, twice, and was
# stopped as hung. Every slow step now says what it is about to do BEFORE it starts, reports
# how long it took, and -- while it runs quietly -- prints a "still working" line every 15s.
# Not verbose: one line in, one line out, a heartbeat only when a step actually runs long.
with_heartbeat() {  # $1 = what is happening (e.g. "backing up the vault"); rest = command
  local label="$1"; shift
  local start=$SECONDS next=15 pid rc=0
  "$@" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    if [ $((SECONDS - start)) -ge "$next" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  ...still $label ($((SECONDS - start))s)"
      next=$((next + 15))
    fi
  done
  wait "$pid" || rc=$?
  HEARTBEAT_ELAPSED=$((SECONDS - start))
  return "$rc"
}

# Each interactive question opens with two rows of asterisks, so on screen it is plain where
# one question ends and the next begins (owner 2026-10-10). gt_install_questions.py does the
# same for the questions it asks (QUESTION_BREAK).
question_break() {
  local row="************************************************************************"
  echo ""
  echo "$row"
  echo "$row"
}

checksum_problem() {   # $1 = what went wrong; stops only when verification was required
  if [ "$REQUIRE_CHECKSUM" = yes ]; then
    echo "✗ checksum: $1 -- NOTHING was installed (--require-checksum)."
    echo "  Copy gt-src again (the whole tree, onto the repository root), or drop --require-checksum."
    exit 8
  fi
  echo "⚠ checksum: $1 -- installing anyway. This install is NOT verified;"
  echo "  pass --require-checksum to refuse instead."
}
checksum_block() {
  # 0.20.2 (owner, 2026-10-06: "We just need to use it to pass the code back and forth"): the
  # shared gt-src folder is a transfer copy, not an install source. dev/sync-gt-src.sh leaves
  # SOURCE.json at its root and copygt.sh does not carry it into the repository it copies to,
  # so SOURCE.json beside this tree means "you are inside the transfer copy". Refused before
  # anything is copied; GT_INSTALL_FROM_SHARED=1 installs from it deliberately.
  if [ -f "$SCRIPT_DIR/../SOURCE.json" ] || [ -f "$SCRIPT_DIR/SOURCE.json" ]; then
    if [ "${GT_INSTALL_FROM_SHARED:-}" = 1 ]; then
      echo "⚠ installing from the shared transfer copy (GT_INSTALL_FROM_SHARED=1)"
    else
      echo "✗ This is the shared transfer copy (gt-src). It passes code between machines; it is"
      echo "  not installed from. Copy it into your repository with copygt.sh, then run install.sh"
      echo "  there. To install from this copy anyway: GT_INSTALL_FROM_SHARED=1 bash install.sh"
      echo "  NOTHING was installed."
      exit 9
    fi
  fi
  SUMS_ROOT=""
  for _d in "$SCRIPT_DIR/.." "$SCRIPT_DIR"; do
    if [ -f "$_d/SHA256SUMS" ]; then SUMS_ROOT="$(cd "$_d" && pwd)"; break; fi
  done
  if [ -z "$SUMS_ROOT" ]; then
    if [ "$REQUIRE_CHECKSUM" = yes ]; then
      checksum_problem "no SHA256SUMS beside this tree, so there is nothing to verify against"
    fi
    echo "checksum: no SHA256SUMS beside this tree -- not a published gt-src; installed as it stands"
  else
    SUMCMD=""
    if command -v shasum >/dev/null 2>&1; then SUMCMD="shasum -a 256"
    elif command -v sha256sum >/dev/null 2>&1; then SUMCMD="sha256sum"; fi
    if [ -z "$SUMCMD" ]; then
      checksum_problem "neither shasum nor sha256sum is installed, so SHA256SUMS cannot be checked"
    else
      SUMS_OUT=""
      SUMS_RC=0
      echo "checksum: verifying $(grep -c . "$SUMS_ROOT/SHA256SUMS" || true) published files (a cloud-synced folder may download them first)..."
      SUMS_OUT=$(cd "$SUMS_ROOT" && $SUMCMD -c --quiet SHA256SUMS 2>&1) || SUMS_RC=$?
      SUMS_TREE=$($SUMCMD "$SUMS_ROOT/SHA256SUMS" | cut -d' ' -f1)
      if [ "$SUMS_RC" -ne 0 ]; then
        echo "checksum: these files do not match the SHA256SUMS this tree was published with:"
        printf '%s\n' "$SUMS_OUT" | head -25 | sed 's/^/    /'
        checksum_problem "a file above is missing, changed, or not where it belongs"
      else
        SUMS_N=$(grep -c . "$SUMS_ROOT/SHA256SUMS" || true)
        echo "checksum: all $SUMS_N published files present, in place and unchanged (tree_sha256 $SUMS_TREE)"
      fi
      SUMS_EXTRA=$(cd "$SUMS_ROOT" && find . -type f ! -path './.git/*' ! -path '*/__pycache__/*' \
                     ! -name .DS_Store ! -name SHA256SUMS ! -name SOURCE.json | LC_ALL=C sort \
                   | LC_ALL=C comm -23 - <(sed 's/^[0-9a-f]*  //' SHA256SUMS | LC_ALL=C sort) || true)
      if [ -n "$SUMS_EXTRA" ]; then
        echo "checksum: $(printf '%s\n' "$SUMS_EXTRA" | grep -c .) file(s) here were not part of the publish (left in place):"
        printf '%s\n' "$SUMS_EXTRA" | head -10 | sed 's/^/    /'
      fi
    fi
  fi
}

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
    echo "  A public copy holds only the current release and the previous one. An older release is in git:"
    echo "    git log --oneline -- ${PLUGIN_DIRS[$i]}/$ver   # find the commit that had it"
    echo "    git checkout <commit> -- ${PLUGIN_DIRS[$i]}/$ver"
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

# Who owns the marketplace named golden-thread-plugin (0.22.0). Terminal route: this installer
# writes it, a directory marketplace with one entry per installed plugin (MARKET_MODE=local).
# Prompt route: the person ran `/plugin marketplace add <github repo>/golden-thread.git` (or a path), so
# Claude Code owns it -- a GitHub clone inside ~/.claude/plugins/marketplaces/ that this installer
# may even be running FROM. Writing our directory marketplace over it would replace the source
# they chose and scribble into a git clone, so there the installer touches only what is
# machine-level: the plugin cache, the registrations, settings, hooks, scripts, the vault
# (MARKET_MODE=external). Either way there is ONE marketplace of this name.
MARKET_MODE=$(modpy external-market "$KNOWN" "$MARKET_NAME" "$MARKETPLACE" "$SCRIPT_DIR" 2>/dev/null) || MARKET_MODE=local
[ "$MARKET_MODE" = external ] || MARKET_MODE=local
export GT_MARKET_MODE="$MARKET_MODE"

# ── --only NAME (0.21.4): one module, without the whole install ──────────────────────────────
#
# Owner, 2026-10-14: "the ability of installing a skill without going through the full install".
# Claude Code installs plugins (`claude plugin install gt-pane@golden-thread-plugin`), but it
# has no install hook, so it cannot do the machine half of a gt module: the hook-dir scripts,
# the hook wiring, the lockdown rules, the model policy, the recorded choice. A plugin installed
# that way alone is half a module, and the next full install removes it again when its default
# is off (nothing recorded it as chosen).
#
# --only runs THIS installer -- every machine step, the same code -- with three differences, so
# the result cannot drift from what a full install lays down:
#   * the plugin set is the machine as it is: gt at the release it already has (a different gt
#     in this tree is refused: that is an upgrade, the full install's job), every other plugin
#     at the version it already has (an older one this tree still carries is pinned; one it no
#     longer carries is refused), a module this machine never had is left out, and only NAME
#     moves to the newest release that admits that gt. NAME is recorded on (--with NAME).
#   * nothing is asked: stdin is closed, so every question keeps its current answer.
#   * the vault is not touched (no backup, no refresh, no upgrade), and the release gate is
#     replaced by a self-proof of NAME alone (modpy only-verify). A FAIL there exits 9, and
#     every non-zero exit after the first write is rolled back by the snapshot below.
# --check / --dry-run stop after the plan: exit 0 when NAME is installed, current and passes its
# self-proof; 3 when the install would change something; 1 when --only refuses.
ONLY_MODULE="" ONLY_PLUGIN="" ONLY_INDEX="" ONLY_FROM="" ONLY_TO=""
if [ -n "$ONLY" ]; then
  _oargs=()
  i=0
  while [ "$i" -lt "$PLUGIN_COUNT" ]; do
    _oargs+=("${PLUGIN_DIRS[$i]}" "${PLUGIN_VERS[$i]}" "${PLUGIN_NAMES[$i]}")
    i=$((i + 1))
  done
  _otarget=$(modpy only-target "$SCRIPT_DIR" "$ONLY" ${_oargs[@]+"${_oargs[@]}"}) || {
    printf '%s\n' "$_otarget"; exit 1; }
  IFS="$(printf '\t')" read -r ONLY_INDEX ONLY_MODULE ONLY_PLUGIN _ohow <<<"$_otarget"
  ONLY_TSV=$(modpy only-installed "$INSTALLED" "$CACHE_ROOT" "${PLUGIN_NAMES[@]}")
  only_have() {  # $1 = plugin name -> its installed version, or - ; $2 = field (2 version, 3 cached)
    printf '%s\n' "$ONLY_TSV" | awk -F'\t' -v n="$1" -v f="${2:-2}" '$1 == n {print $f}'
  }
  if [ "$(only_have gt)" = "-" ] || [ ! -f "$HOME/.claude/golden-thread/hooks/gt_paths.py" ]; then
    echo "✗ --only $ONLY: gt is not installed on this machine, so there is no gt to add a module to."
    echo "  Install gt first: $U_INSTALL  (then --only adds or updates one module). Nothing has been installed."
    exit 1
  fi
  if [ "$(only_have gt)" != "$VERSION" ]; then
    echo "✗ --only $ONLY: this machine has gt $(only_have gt), and this tree installs gt $VERSION."
    echo "  Moving gt is an upgrade: run the full install, $U_INSTALL. Nothing has been installed."
    exit 1
  fi
  if [ "$(only_have gt 3)" != yes ]; then
    echo "✗ --only $ONLY: gt $VERSION is registered here but its plugin files are missing; the full install"
    echo "  repairs that: $U_INSTALL. Nothing has been installed."
    exit 1
  fi
  echo "--only $ONLY: module $ONLY_MODULE (plugin $ONLY_PLUGIN, $_ohow); gt $VERSION and every other module stay as they are"
  i=1
  while [ "$i" -lt "$PLUGIN_COUNT" ]; do
    _oh=$(only_have "${PLUGIN_NAMES[$i]}")
    if [ "$i" = "$ONLY_INDEX" ]; then
      ONLY_FROM="$_oh"; ONLY_TO="${PLUGIN_VERS[$i]}"
    elif [ -n "$_oh" ] && [ "$_oh" != "-" ] && [ "$_oh" != "${PLUGIN_VERS[$i]}" ]; then
      if [ -f "$SCRIPT_DIR/${PLUGIN_DIRS[$i]}/$_oh/.claude-plugin/plugin.json" ]; then
        echo "  kept ${PLUGIN_NAMES[$i]} $_oh as installed (the full install would move it to ${PLUGIN_VERS[$i]})"
        PLUGIN_VERS[$i]="$_oh"
      else
        echo "✗ --only $ONLY: ${PLUGIN_NAMES[$i]} $_oh is installed here and this tree no longer carries that"
        echo "  release, so it cannot be kept as it is. Run the full install, $U_INSTALL. Nothing has been installed."
        exit 1
      fi
    fi
    i=$((i + 1))
  done
  MODULE_FLAGS=("with:$ONLY_MODULE")
  # No question is asked: with stdin closed every [ -t 0 ] below is false, so each question keeps
  # its current answer and no offer (status line, ast-grep, the guide) is made.
  exec </dev/null
fi

# ── The install questions (0.21.0, item #33) ──────────────────────────────────────────────────
# --questions prints them and installs nothing. Otherwise every answer is resolved NOW, before
# any file is written: flags, --answers FILE, the terminal, the current values, the defaults. Not
# at a terminal, a new install with a security question unanswered stops here with exit 5.
GT_QUESTIONS="$SRC/scripts/gt_install_questions.py"
if [ "$QUESTIONS" = yes ]; then
  if [ ! -f "$GT_QUESTIONS" ]; then
    echo "✗ this release ($VERSION) has no install questions list."
    exit 1
  fi
  _gt_qa=(list)
  if [ "$JSON_OUT" = yes ]; then _gt_qa+=(--json); fi
  _gt_rc=0
  python3 -B "$GT_QUESTIONS" "${_gt_qa[@]}" || _gt_rc=$?
  exit "$_gt_rc"
fi
# ── Claude Code 2.1.300 or newer (0.21.4) ──────────────────────────────────────────────────────
# Owner 2026-10-16: "check if it is 300 or higher. If not then we help them install the new
# version." Advice only: at a terminal it offers `claude update` (and runs it only on a yes); with
# no terminal or --accept-defaults it prints the command. It never fails or stops the install. A
# test sandbox skips it (hundreds of installs) unless GT_CC_VERSION_CHECK=1 asks for it.
if [ "$LIST_MODULES" != yes ] && [ "$QUESTIONS" != yes ] \
   && { [ -z "${GT_TEST_SANDBOX:-}" ] || [ "${GT_CC_VERSION_CHECK:-}" = 1 ]; }; then
  _gt_ccv=(check)
  if [ "$ACCEPT_DEFAULTS" = no ] && [ -t 0 ] && [ -t 1 ]; then _gt_ccv+=(--offer); fi
  python3 -B "$SRC/scripts/gt_cc_version.py" "${_gt_ccv[@]}" || true
fi
# ── Modules in one question (0.22.0) ───────────────────────────────────────────────────────────
# Before 0.22.0 a person had to turn modules on one at a time (--with NAME, or one /plugin install
# per module). --all-modules / --core-only are the one-flag answers, and a NEW install at a
# terminal that gave no module flag is asked once. An answer here is only ever the same
# --with/--without flags a person could have typed, so the choice is remembered like theirs.
if [ "$ALL_MODULES" = yes ] && [ "$CORE_ONLY" = yes ]; then
  echo "✗ --all-modules and --core-only contradict each other; give one"; exit 1
fi
# --starter is core only (phase 116): a module flag asks for more than it installs.
if [ "$STARTER" = yes ]; then
  if [ "$ALL_MODULES" = yes ] || [ "${#MODULE_FLAGS[@]}" -gt 0 ]; then
    echo "✗ --starter installs core only; --all-modules and --with/--without contradict it. Add a module later with --with NAME"
    exit 1
  fi
  CORE_ONLY=yes
  export GT_STARTER=1       # vault_init.py: no git hooks path in the vault
fi
if [ "$ALL_MODULES" = no ] && [ "$CORE_ONLY" = no ] && [ "${#MODULE_FLAGS[@]}" -eq 0 ] \
   && [ "$ACCEPT_DEFAULTS" = no ] && [ "$LIST_MODULES" != yes ] && [ "$QUESTIONS" != yes ] \
   && [ -t 0 ] && [ -t 1 ] \
   && [ ! -f "$HOME/.claude/golden-thread/install-choices.json" ] \
   && [ ! -d "$HOME/.claude/golden-thread/wired" ]; then
  _gt_every=$(modpy bundle "$SCRIPT_DIR" everything 2>/dev/null | tr '\n' ' ' | sed 's/ *$//; s/ /, /g')
  if [ -n "$_gt_every" ]; then
    question_break
    echo "Which Golden Thread modules do you want?"
    echo "  1) Everything (recommended): $_gt_every"
    echo "  2) Let me choose: the default set, and I will ask about the rest"
    echo "  3) Core only: gt on its own; add a module later with --with NAME"
    printf 'Choose 1, 2 or 3 [1]: '
    read -r _gt_mans </dev/tty || _gt_mans=""
    case "$_gt_mans" in
      ""|1|e|E|everything|Everything) ALL_MODULES=yes ;;
      3|c|C|core|Core) CORE_ONLY=yes ;;
      *) ;;
    esac
  fi
fi
if [ "$ALL_MODULES" = yes ]; then
  while IFS= read -r _gt_m; do
    [ -n "$_gt_m" ] || continue
    case " ${MODULE_FLAGS[*]:-} " in *" with:$_gt_m "*|*" without:$_gt_m "*) ;; *) MODULE_FLAGS+=("with:$_gt_m") ;; esac
  done < <(modpy bundle "$SCRIPT_DIR" everything)
elif [ "$CORE_ONLY" = yes ]; then
  while IFS= read -r _gt_m; do
    [ -n "$_gt_m" ] || continue
    case " ${MODULE_FLAGS[*]:-} " in *" with:$_gt_m "*|*" without:$_gt_m "*) ;; *) MODULE_FLAGS+=("without:$_gt_m") ;; esac
  done < <(modpy bundle "$SCRIPT_DIR" all)
fi

PLAN_SUMMARY=""
PLAN_REVIEW=""
# --list-modules asks nothing and installs nothing, so it needs no answers
if [ -f "$GT_QUESTIONS" ] && [ "$LIST_MODULES" != yes ]; then
  _gt_penv=$(mktemp "${TMPDIR:-/tmp}/gt-plan-env.XXXXXX")
  _gt_psum=$(mktemp "${TMPDIR:-/tmp}/gt-plan-sum.XXXXXX")
  _gt_prev=$(mktemp "${TMPDIR:-/tmp}/gt-plan-rev.XXXXXX")
  _gt_pa=(plan --env-out "$_gt_penv" --summary-out "$_gt_psum" --review-out "$_gt_prev")
  if [ "$ACCEPT_DEFAULTS" = yes ]; then _gt_pa+=(--accept-defaults); fi
  if [ "$STARTER" = yes ]; then _gt_pa+=(--starter); fi
  if [ -t 0 ] && [ -t 1 ]; then _gt_pa+=(--tty); fi
  if [ -n "$ANSWERS_FILE" ]; then _gt_pa+=(--answers "$ANSWERS_FILE"); fi
  if [ -n "$LOCKDOWN" ]; then _gt_pa+=(--set "lockdown=$LOCKDOWN"); fi
  if [ -n "$SANDBOX_MODE" ]; then _gt_pa+=(--set "sandbox_mode=$SANDBOX_MODE"); fi
  if [ -n "$PARANOID" ]; then _gt_pa+=(--set "paranoid=$PARANOID"); fi
  if [ -n "$MODEL_PROFILE" ]; then _gt_pa+=(--set "model_profile=$MODEL_PROFILE"); fi
  if [ "$PERSONA_SET" = yes ]; then _gt_pa+=(--set "persona=${PERSONA:-none}"); fi
  if [ -n "$EXPIRY_GUARD" ]; then _gt_pa+=(--set "expiry_guard=$EXPIRY_GUARD"); fi
  if [ -n "$TOKEN_PROFILE" ]; then _gt_pa+=(--set "token_profile=$TOKEN_PROFILE"); fi
  if [ -n "$PERSONA_ALIGNED" ]; then _gt_pa+=(--set "persona_aligned=$PERSONA_ALIGNED"); fi
  if [ -n "$REPLY_LENGTH" ]; then _gt_pa+=(--set "reply_length=$REPLY_LENGTH"); fi
  if [ -n "$PARALLEL_MAX" ]; then _gt_pa+=(--set "parallel_max=$PARALLEL_MAX"); fi
  if [ -n "$OPEN_GUIDE" ]; then _gt_pa+=(--set "open_guide=$OPEN_GUIDE"); fi
  if [ -n "$OBSIDIAN_PLUGINS" ]; then _gt_pa+=(--set "obsidian_plugins=$OBSIDIAN_PLUGINS"); fi
  if [ -n "$TELEMETRY" ]; then _gt_pa+=(--set "telemetry=$TELEMETRY"); fi
  if [ -n "$STATUSLINE" ]; then _gt_pa+=(--set "statusline=$STATUSLINE"); fi
  if [ -n "$AST_GREP" ]; then _gt_pa+=(--set "ast_grep=$AST_GREP"); fi
  # --with pane / --without pane answer the pane question (item #41)
  for _f in ${MODULE_FLAGS[@]+"${MODULE_FLAGS[@]}"}; do
    case "$_f" in
      with:pane) _gt_pa+=(--set "pane=yes") ;;
      without:pane) _gt_pa+=(--set "pane=no") ;;
    esac
  done
  _gt_rc=0
  python3 -B "$GT_QUESTIONS" "${_gt_pa[@]}" || _gt_rc=$?
  if [ "$_gt_rc" -ne 0 ]; then
    rm -f "$_gt_penv" "$_gt_psum" "$_gt_prev"
    exit "$_gt_rc"
  fi
  # shellcheck disable=SC1090
  . "$_gt_penv"
  PLAN_SUMMARY=$(cat "$_gt_psum")
  PLAN_REVIEW=$(cat "$_gt_prev")
  rm -f "$_gt_penv" "$_gt_psum" "$_gt_prev"
  # what the plan decided is what the steps below apply
  if [ "${PLAN_persona_SRC:-}" = choice ]; then
    PERSONA="$PLAN_persona"; PERSONA_SET=yes
  fi
  # The pane question decides whether the gt-pane module is installed (item #41). An explicit
  # --with/--without already is the answer; a kept answer changes nothing; a no that nobody
  # chose leaves the module at its default (off), recording nothing.
  case "${PLAN_pane_SRC:-}:${PLAN_pane:-}" in
    choice:yes|default:yes|accepted:yes)
      case " ${MODULE_FLAGS[*]:-} " in *" with:pane "*|*" without:pane "*) ;; *) MODULE_FLAGS+=("with:pane") ;; esac ;;
    choice:no)
      case " ${MODULE_FLAGS[*]:-} " in *" with:pane "*|*" without:pane "*) ;; *) MODULE_FLAGS+=("without:pane") ;; esac ;;
  esac
fi
plan_words() {  # $1 = a PLAN_<key>_SRC value -> how the answer came about, in words
  case "$1" in
    choice)   echo "your choice" ;;
    kept)     echo "kept: current" ;;
    accepted) echo "default, accepted with --accept-defaults" ;;
    *)        echo "default, chosen for you" ;;
  esac
}
# A bad --persona stops here, before anything is installed (0.21.0). The valid names are the
# packs this release ships, so the list cannot drift from them.
if [ "$PERSONA_SET" = yes ] && [ "$PERSONA" != none ] && [ -n "$PERSONA" ]; then
  _pv=$(python3 -B "$SRC/scripts/gt_persona.py" list 2>/dev/null | awk '{print $1}')
  for _p in $(echo "$PERSONA" | tr ',' ' '); do
    case " $(echo $_pv) " in
      *" $_p "*) ;;
      *) echo "✗ --persona: '$_p' is not an installed persona; valid: $(echo $_pv | tr ' ' ',')"; exit 1 ;;
    esac
  done
fi
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
# The exit handler also rolls a failed install back (0.20.1) -- see snapshot_take below,
# which arms it just before the first write.
GT_SNAP="" GT_SNAP_READY=""
gt_on_exit() {  # $1 = the exit status
  local rc="$1"
  trap - EXIT
  if [ "${ONLY_QUIET:-}" = yes ]; then          # --only, quiet: back to the terminal (0.21.4)
    exec 1>&3 2>&4
    ONLY_QUIET=""
    if [ "$rc" -ne 0 ]; then
      echo "✗ --only $ONLY stopped with exit $rc. The end of the installer's log:"
      tail -n 30 "$ONLY_LOG" | sed 's/^/  | /'
      echo "  The whole log: $ONLY_LOG"
    else
      rm -f "$ONLY_LOG"
    fi
  fi
  # Exit 9 under --force-manifest-mismatch is kept, not rolled back: the tree was installed on
  # purpose although it does not match its manifest, so the gate's components row fails by
  # construction (dev/check_wiring_coverage.py installs tampered trees exactly this way).
  if [ "$GT_SNAP_READY" = yes ] && [ "$rc" -ne 0 ] && [ "$rc" -ne 4 ] \
     && ! { [ "$rc" -eq 9 ] && [ "${FORCE_MANIFEST:-no}" = yes ]; }; then
    echo ""
    if ( snapshot_restore ); then
      GT_ROLLED_BACK=yes
      echo "ROLLED BACK — the install exited $rc, so the machine is as it was before it started:"
      echo "  settings.json, the plugin registrations, marketplace and cache, ~/.claude/golden-thread"
      echo "  (its backups/ kept) and vault-config.json are restored. The vault is not rolled back."
    else
      echo "⚠ ROLLBACK INCOMPLETE — the install exited $rc and restoring the previous state failed."
      echo "  The copy taken before the install is kept at: $GT_SNAP"
      GT_SNAP=""                                # keep it: it is the only copy
    fi
  fi
  # Installed-code integrity (0.21.0): this run unlocked gt's code (GT_ITOKEN), so it leaves it
  # locked again whatever happened -- after a rollback the files put back are the old ones, unlocked
  # by the snapshot; after exit 4 the install was complete but the lock step had not been reached.
  # The token is spent here either way.
  if [ -n "${GT_ITOKEN:-}" ]; then
    _gt_it="$HOME/.claude/golden-thread/hooks/gt_integrity.py"
    if [ -f "$_gt_it" ]; then
      if [ "${GT_ILOCKED:-}" != yes ] || [ "${GT_ROLLED_BACK:-}" = yes ]; then
        GT_INTEGRITY_TOKEN="$GT_ITOKEN" python3 -B "$_gt_it" lock >/dev/null 2>&1 \
          || echo "⚠ gt's code could not be locked again: run  python3 \"$_gt_it\" relock"
      fi
      python3 -B "$_gt_it" finish >/dev/null 2>&1 || true
    fi
  fi
  rm -rf "$GT_TMP"
  [ -n "$GT_SNAP" ] && rm -rf "$GT_SNAP"
  [ -n "${_GT_MODPY_FILE:-}" ] && rm -f "$_GT_MODPY_FILE"
  exit "$rc"
}
trap 'gt_on_exit $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
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
if [ -n "$ONLY" ]; then
  # The module itself must be installable beside this gt, here: refused, never skipped.
  _ost=$(awk -F'\t' -v n="$ONLY_MODULE" '$1 == n {print $2 "\t" $3}' "$GT_TMP/states.tsv")
  if [ "${_ost%%$(printf '\t')*}" != on ]; then
    case "${_ost#*$(printf '\t')}" in
      requires) _owhy="its release here (${ONLY_TO}) does not admit gt $VERSION (requires_gt)" ;;
      platform) _owhy="it does not run on this platform" ;;
      claude)   _owhy="it $(python3 "$SRC/scripts/gt_components.py" module-cc-gap "$ONLY_MODULE" 2>/dev/null || echo 'needs a newer Claude Code')" ;;
      invalid)  _owhy="its module.json is invalid ($U_INSTALL --list-modules says why)" ;;
      *)        _owhy="it resolves off (${_ost#*$(printf '\t')})" ;;
    esac
    echo "✗ --only $ONLY: module $ONLY_MODULE cannot be installed: $_owhy. Nothing has been installed."
    exit 1
  fi
  printf '%s\n' "$ONLY_TSV" > "$GT_TMP/only-installed.tsv"
  modpy only-freeze "$MODJSON" "$ONLY_MODULE" "$GT_TMP/only-installed.tsv" "$GT_TMP/states.tsv" || exit 1
  ONLY_MARKET="$MARKETPLACE"; [ "$MARKET_MODE" = external ] && ONLY_MARKET="-"
  only_verify() {
    modpy only-verify "$MODJSON" "$ONLY_MODULE" "$HOME" "$CACHE_ROOT" "$ONLY_MARKET" \
      "$INSTALLED" "$SETTINGS" "$SRC"
  }
  if [ "$ONLY_CHECK" = yes ]; then
    if [ "$ONLY_FROM" = "-" ]; then
      echo "Would install: $ONLY_PLUGIN $ONLY_TO (module $ONLY_MODULE is not on this machine)"
      echo "--check: nothing was changed. Run it without --check to install."
      exit 3
    fi
    if [ "$ONLY_FROM" != "$ONLY_TO" ]; then
      echo "Would update: $ONLY_PLUGIN $ONLY_FROM → $ONLY_TO"
      echo "--check: nothing was changed. Run it without --check to update."
      exit 3
    fi
    echo "$ONLY_PLUGIN $ONLY_TO is installed; checking it is complete:"
    _orc=0
    _oout=$(only_verify) || _orc=$?
    printf '%s\n' "$_oout" | sed 's/^/  /'
    if [ "$_orc" -ne 0 ]; then
      echo "Would repair: $ONLY_PLUGIN $ONLY_TO (a FAIL row above). --check: nothing was changed."
      exit 3
    fi
    echo "--check: $ONLY_PLUGIN $ONLY_TO is installed, current and complete; nothing to do."
    exit 0
  fi
  # Quiet by default (0.21.4, owner): the person sees the module's own lines and its self-proof;
  # the installer's whole log goes to a file, shown (its tail) only if the run fails. --verbose
  # prints all of it as a full install does.
  if [ "$VERBOSE" != yes ] && [ -n "${GT_INSTALL_RERUN:-}" ] && [ -n "${GT_ONLY_LOG:-}" ]; then
    ONLY_LOG="$GT_ONLY_LOG"; ONLY_QUIET=yes   # the migration re-run: fds 3/4 and the log carry over
  elif [ "$VERBOSE" != yes ]; then
    ONLY_LOG=$(mktemp "${TMPDIR:-/tmp}/gt-install-only.XXXXXX") || ONLY_LOG=""
    if [ -n "$ONLY_LOG" ]; then
      echo "Installing $ONLY_PLUGIN $ONLY_TO (the installer's log: $ONLY_LOG; --verbose shows it all)"
      exec 3>&1 4>&2 >"$ONLY_LOG" 2>&1
      ONLY_QUIET=yes
      export GT_ONLY_LOG="$ONLY_LOG"
    fi
  fi
fi

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
      [ "$_why" = invalid ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m: its module.json is invalid ($U_INSTALL --list-modules says why)
"
      [ "$_why" = requires ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m (${PLUGIN_NAMES[$i]} ${PLUGIN_VERS[$i]}): its requires_gt does not admit gt $VERSION — treated as off for this run, your recorded choice unchanged
"
      [ "$_why" = platform ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m: it is POSIX-only (a Unix-domain-socket gateway) and does not run on Windows — your recorded choice unchanged
"
      [ "$_why" = claude ] && SKIP_NOTES="${SKIP_NOTES}Skipping module $_m: it $(python3 "$SRC/scripts/gt_components.py" module-cc-gap "$_m" 2>/dev/null || echo 'needs a newer Claude Code') — your recorded choice unchanged; update Claude Code, then re-run the installer to add it
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
# The checksum runs HERE: after the first line (so the installer speaks at once -- 0.17.5 ran it
# silently before any output, and on a cloud-synced gt-src that hashing downloads every file
# first), and before step 0 below, the first thing that writes anything.
checksum_block
# every choice and how it was made, before anything is written (item #33)
if [ -n "$PLAN_REVIEW" ]; then printf '%s\n' "$PLAN_REVIEW"; fi
[ -n "$PICK_NOTES" ] && printf '%s' "$PICK_NOTES"
[ -n "$SKIP_NOTES" ] && printf '%s' "$SKIP_NOTES"

# ── Downgrade preflight (0.20.1): a HOOK POINT, before anything is written ───────────────────
#
# IS_DOWNGRADE=yes when this run installs a gt OLDER than the one this machine has. A release
# from before a security feature cannot undo that feature's settings (usability run M3: with the
# sandbox on, 0.19.2 has no gt_sandbox and no gt-vault MCP, so the deny rules stay and the vault
# is locked out). downgrade_preflight is where a refusal for such a state goes -- it runs before
# the snapshot and the first write, so a refusal (exit 1, nothing written) leaves the machine
# untouched. It is a no-op here; the sandbox/unlock check is added beside this comment.
IS_DOWNGRADE=no
if [ -n "${GT_PREVIOUS_RELEASE:-}" ] && [ "$GT_PREVIOUS_RELEASE" != "$VERSION" ] \
   && [ "$(printf '%s\n%s\n' "$GT_PREVIOUS_RELEASE" "$VERSION" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$VERSION" ]; then
  IS_DOWNGRADE=yes
fi
downgrade_preflight() {
  # ── A rollback must not strand gt sandbox mode or gt unlock (0.20.1, finding M3) ────────────
  # A release without gt_sandbox.py (before 0.20.1) cannot remove the deny rules sandbox mode
  # wrote into ~/.claude/settings.json, and one without gt_unlock.py cannot turn unlock off: the
  # rolled-back machine kept the vault locked from Claude with no undo. Checked HERE, before the
  # first write, by the NEWEST gt_sandbox.py in this tree (the one that knows both features);
  # it prints the exact commands that switch them off first.
  _gt_newest_sb="$SCRIPT_DIR/$CORE_DIR/$(latest_version "$SCRIPT_DIR/$CORE_DIR")/scripts/gt_sandbox.py"
  if [ -f "$_gt_newest_sb" ] && { [ ! -f "$SCRIPT_DIR/$CORE_DIR/$VERSION/scripts/gt_sandbox.py" ] \
       || [ ! -f "$SCRIPT_DIR/$CORE_DIR/$VERSION/scripts/gt_unlock.py" ]; }; then
    if ! python3 -B "$_gt_newest_sb" rollback-check --target "$SCRIPT_DIR/$CORE_DIR/$VERSION"; then
      exit 1
    fi
  fi
}
# the sandbox/unlock check keys on what the TARGET release lacks, so it runs on every install;
# IS_DOWNGRADE (set above from GT_PREVIOUS_RELEASE) is for checks that need the direction.
downgrade_preflight

# ── Installed-code integrity (0.21.0, the `paranoid` settings group) ────────────────────────────
# When gt's installed code is OS-locked and/or sealed (paranoid.os_lock, paranoid.signed_manifest),
# this install cannot write it: it must unlock first, and unlocking is the ONE presence-gated step
# (gate gt-code-write: Touch ID / Windows Hello / authenticator app with gt unlock, else a typed
# confirmation on a real terminal, whose caveat is printed). It runs HERE -- after every cheap
# refusal, before the snapshot and the first write -- so a refusal leaves the machine untouched.
# The presence check yields a token this shell variable holds (never exported, never on disk);
# `lock` at the end takes it, so one upgrade is one check (two when the manifest is signed).
# With the group off (the default) `locked` fails at once and none of this runs.
GT_ITOKEN="" GT_ILOCKED="" GT_ROLLED_BACK=""
GT_INTEGRITY_TOOL="$HOME/.claude/golden-thread/hooks/gt_integrity.py"
if [ -f "$GT_INTEGRITY_TOOL" ] && python3 -B "$GT_INTEGRITY_TOOL" locked >/dev/null 2>&1; then
  echo "gt's installed code is locked or sealed (paranoid): unlocking it for this install needs you."
  if ! GT_ITOKEN=$(python3 -B "$GT_INTEGRITY_TOOL" unlock --for-upgrade --emit-token) \
     || [ -z "$GT_ITOKEN" ]; then
    GT_ITOKEN=""
    echo "✗ gt's installed code stays locked, so nothing was installed (exit 10)."
    echo "  Unlock it from a terminal:  python3 \"$GT_INTEGRITY_TOOL\" unlock --for-upgrade"
    echo "  then run this installer again."
    exit 10
  fi
fi

# ── Rollback: a failed install leaves the machine as it was (0.20.1) ─────────────
#
# Until 0.19.2 a failure after the first write left a half-installed machine ("nothing is
# rolled back"): caches pruned, settings.json half-registered, hooks from two releases. Now
# everything this installer may write under the user's home is copied aside before step 0 --
# the first write -- and put back if the install then exits non-zero. The one exception is
# exit 4: the plugin installed completely and only the vault decision is missing, which is a
# question for the user, not a failure; and exit 9 under --force-manifest-mismatch, where the
# gate fails by construction on a tree the user chose to install anyway.
#
# Covered: ~/.claude/settings.json, plugins/installed_plugins.json, known_marketplaces.json,
# the golden-thread-plugin marketplace and cache, ~/.claude/golden-thread (hooks, recorded
# interpreter, module choices, Windows job files -- but not backups/, which only grows),
# vault-config.json, ~/.claude/CLAUDE.md, and the Windows python3 shim (~/bin/python3).
# NOT covered: the vault (it has its own pre-write backup, named when it is taken) and loaded
# launchd jobs.
#
# The snapshot dir is passed to the migration re-run (exec) below, so a failure there still
# restores the state from before the FIRST run.
GT_SNAP_PATHS=(.claude/settings.json .claude/plugins/installed_plugins.json
               .claude/plugins/known_marketplaces.json
               ".claude/plugins/marketplaces/$MARKET_NAME" ".claude/plugins/cache/$MARKET_NAME"
               .claude/vault-config.json .claude/CLAUDE.md bin/python3)
# Parent dirs the install may create; one that did not exist and is empty again goes too.
GT_SNAP_DIRS=(.claude/plugins/cache .claude/plugins/marketplaces .claude/plugins bin)
_gt_gtdir="$HOME/.claude/golden-thread"
snapshot_take() {
  local rel e
  GT_SNAP=$(mktemp -d "${TMPDIR:-/tmp}/gt-install-snapshot.XXXXXX")
  for rel in "${GT_SNAP_PATHS[@]}"; do
    if [ -e "$HOME/$rel" ] || [ -L "$HOME/$rel" ]; then
      mkdir -p "$GT_SNAP/home/$(dirname "$rel")"
      cp -Rp "$HOME/$rel" "$GT_SNAP/home/$rel" || return 1
    fi
  done
  for rel in "${GT_SNAP_DIRS[@]}"; do
    [ -d "$HOME/$rel" ] && echo "$rel" >> "$GT_SNAP/dirs.existed"
  done
  mkdir -p "$GT_SNAP/gt"
  if [ -d "$_gt_gtdir" ]; then
    : > "$GT_SNAP/gt.existed"
    for e in "$_gt_gtdir"/* "$_gt_gtdir"/.[!.]*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      [ "$(basename "$e")" = backups ] && continue
      cp -Rp "$e" "$GT_SNAP/gt/" || return 1
    done
  fi
  GT_SNAP_READY=yes
}
snapshot_restore() {
  local rel e
  for rel in "${GT_SNAP_PATHS[@]}"; do
    rm -rf "${HOME:?}/$rel" || return 1
    if [ -e "$GT_SNAP/home/$rel" ] || [ -L "$GT_SNAP/home/$rel" ]; then
      mkdir -p "$HOME/$(dirname "$rel")"
      cp -Rp "$GT_SNAP/home/$rel" "$HOME/$rel" || return 1
    fi
  done
  if [ -d "$_gt_gtdir" ]; then
    for e in "$_gt_gtdir"/* "$_gt_gtdir"/.[!.]*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      [ "$(basename "$e")" = backups ] && continue
      rm -rf "$e" || return 1
    done
  fi
  if [ -f "$GT_SNAP/gt.existed" ]; then
    mkdir -p "$_gt_gtdir" || return 1
    cp -Rp "$GT_SNAP/gt/." "$_gt_gtdir/" || return 1
  elif [ -d "$_gt_gtdir" ] && [ -z "$(ls -A "$_gt_gtdir/backups" 2>/dev/null)" ]; then
    rm -rf "$_gt_gtdir"                       # it did not exist, and holds no backup
  fi
  for rel in "${GT_SNAP_DIRS[@]}"; do
    grep -qxF "$rel" "$GT_SNAP/dirs.existed" 2>/dev/null || rmdir "$HOME/$rel" 2>/dev/null || true
  done
}
if [ -n "${GT_INSTALL_SNAPSHOT:-}" ] && [ -d "$GT_INSTALL_SNAPSHOT" ]; then
  GT_SNAP="$GT_INSTALL_SNAPSHOT" GT_SNAP_READY=yes          # the re-run: keep the first copy
elif ! snapshot_take; then
  echo "✗ Could not copy the current state aside before installing (for a rollback); nothing was written."
  GT_SNAP_READY=""
  exit 1
fi

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
  if [ ! -f "$SRC/MANIFEST.json" ]; then
    # ABSENT IS NOT AUTOMATICALLY INNOCENT. Until 0.16.2 a missing manifest returned 0 here
    # and was regenerated further down, so deleting MANIFEST.json produced a self-consistent
    # manifest describing whatever was in the tree -- a cheaper attack than tampering, since
    # tampering has to survive a comparison and deletion removes the comparison.
    #
    # But absence is LEGITIMATE for a zip install: package.sh deliberately does not ship the
    # manifest, precisely so a stale one cannot report drift that does not exist. So refusing
    # every absence would break the normal distribution path.
    #
    # The two cases are distinguishable. A git checkout TRACKS the manifest; a zip has no git
    # at all. A tree that tracks a manifest and does not have one has had it removed, and
    # regenerating there would bless exactly what the check exists to catch.
    if git -C "$SRC" ls-files --error-unmatch MANIFEST.json >/dev/null 2>&1; then
      echo ""
      echo "REFUSING TO INSTALL — MANIFEST.json is tracked in this tree but is not on disk"
      echo ""
      echo "This source is a git checkout, so the manifest is a committed file that something"
      echo "has removed. Regenerating it here would hash whatever is in the tree right now and"
      echo "call the result verified -- which is the one outcome the component check exists to"
      echo "prevent, reached by deleting the check rather than defeating it."
      echo ""
      echo "  Restore it:        git -C \"$SRC\" checkout -- MANIFEST.json"
      echo "  Meant to cut a new release?  python3 dev/plugins.py manifest \"$SRC\"   (development repository only: a published copy has no dev/)"
      exit 6
    fi
    # No git: a zip install, where regenerating is correct. Say so anyway -- the component
    # check is not running, and a check that could not run must never read as one that passed.
    echo "No MANIFEST.json in the source and no git to restore one from (a packaged install)."
    echo "  The component check CANNOT RUN against this tree; a manifest will be written from"
    echo "  the files laid down, and verification starts from the NEXT install, not this one."
    return 0
  fi
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
       echo "  Installing anyway?      $U_INSTALL --force-manifest-mismatch"
       exit 6 ;;
    5) # VERIFY_NO_MANIFEST from verify_source itself: the file EXISTS (the absent case
       # returned earlier) but could not be read or parsed. Truncated, corrupt, or not JSON.
       if [ "$FORCE_MANIFEST" = yes ]; then
         echo "⚠ MANIFEST.json is present but unreadable — overridden by --force-manifest-mismatch"
         printf '%s\n' "$out" | sed 's/^/  /'
         return 0
       fi
       echo ""
       echo "REFUSING TO INSTALL — MANIFEST.json is present but could not be read"
       printf '%s\n' "$out" | sed 's/^/  /'
       echo ""
       echo "A manifest that cannot be parsed compares against nothing, so every file would"
       echo "install unverified. That is the same outcome as having no manifest, reached by a"
       echo "different route, and until 0.16.3 it printed 'continuing' and did exactly that."
       echo ""
       echo "  Restore it:    git -C \"$SRC\" checkout -- MANIFEST.json"
       echo "  Regenerate:    python3 dev/plugins.py manifest \"$SRC\"   (development repository only: a published copy has no dev/)"
       echo "  Install anyway: $U_INSTALL --force-manifest-mismatch"
       exit 6 ;;
    *) # An exit code this installer does not know: gt_components.py crashed, python3 is
       # missing, or a future release added a verdict this one predates. Whatever it was,
       # THE CHECK DID NOT RUN -- which is not the same as the check passing, and saying
       # "continuing" made the two indistinguishable in the one place it matters most.
       if [ "$FORCE_MANIFEST" = yes ]; then
         echo "⚠ Manifest check could not run (exit $rc) — overridden by --force-manifest-mismatch"
         printf '%s\n' "$out" | sed 's/^/  /'
         return 0
       fi
       echo ""
       echo "REFUSING TO INSTALL — the manifest check could not run (exit $rc)"
       printf '%s\n' "$out" | sed 's/^/  /'
       echo ""
       echo "This installer understands exit 0, 3, 4 and 5 from verify-source. Anything else"
       echo "means the check itself failed rather than reaching a verdict, so nothing here has"
       echo "been verified. A check that could not run is not a check that passed."
       echo ""
       echo "  Install anyway: $U_INSTALL --force-manifest-mismatch"
       exit 6 ;;
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
modpy remove "$MODJSON" "$HOME" "$SRC" "$CACHE_ROOT" \
  "$( [ "$MARKET_MODE" = external ] && echo "$GT_TMP/no-local-marketplace" || echo "$MARKETPLACE" )" \
  "$INSTALLED" "$SETTINGS" \
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
#
# The top-level files copied below are the ones the INSTALLED gt release's own doctor lists
# (CACHE_FILES in its gt_doctor.py). A pinned rollback to a release older than 0.22.0 runs that
# older, frozen doctor, which calls retired.json and SPEC.md "left over" and fails the install
# (exit 9, found 2026-10-29 by test_install_quiet.RollbackIsQuiet). No doctor to read, or a list
# that does not parse: copy them all.
GT_DOCTOR_CACHE_FILES=$(sed -n 's/^CACHE_FILES = (\(.*\))$/\1/p' "$(plugin_src 0)/scripts/gt_doctor.py" 2>/dev/null | head -1 | tr -d '",')
[ -n "$GT_DOCTOR_CACHE_FILES" ] || GT_DOCTOR_CACHE_FILES="module.json MANIFEST.json retired.json SPEC.md"
i=0
while [ "$i" -lt "$PLUGIN_COUNT" ]; do
  p_src=$(plugin_src "$i"); p_cache=$(plugin_cache "$i")
  mkdir -p "$p_cache"
  # module.json and demo/ travel too: gt-demo reads module.json and the module's tour act
  # (demo/act.md) from the CACHE to assemble the tour for whatever is installed (0.14.0).
  # packs/ holds the pluggable definitions gt_registry.py resolves at runtime.
  # Without it the registry finds nothing on an installed machine and every
  # contributed definition is inert (found by security review, 2026-09-16).
  # agents/ and workflows/ (0.20.1): the stage agent definitions (gt:<stage>) and the pipeline
  # workflow Claude Code loads from a plugin; replaced like every other dir, so a rollback to a
  # release without them leaves none behind.
  # walkthroughs/, docs/, tests/ (0.22.0): a plugin carries them when it ships them (gt-demo's
  # walkthroughs, gt-lotr's docs, gt-pane's tests). The plugin manager copies the whole tree, so
  # a cache this loop fills must hold the same files or gt_doctor's plugin-files row fails.
  for dir in .claude-plugin skills scripts templates commands hooks demo packs agents workflows help walkthroughs docs tests; do
    rm -rf "${p_cache:?}/$dir"
    [ -d "$p_src/$dir" ] && cp -r "$p_src/$dir" "$p_cache/"
  done
  # MANIFEST.json travels for the same reason packs/ does, and it is load-bearing: it is the
  # ONLY runtime integrity control on an installed pack. Without it gt_registry.py cannot
  # verify a single pack, and since it now fails closed, every shipped pack would refuse to
  # load on an installed machine (both halves found by security review, 2026-09-16).
  # retired.json (the files a release retires, read by the installer) and SPEC.md (gt-lotr's
  # spec) travel too, for the same plugin-files reason as the dirs above (0.22.0).
  for f in module.json MANIFEST.json retired.json SPEC.md; do
    rm -f "${p_cache:?}/$f"
    case " $GT_DOCTOR_CACHE_FILES " in *" $f "*) ;; *) continue ;; esac
    [ -f "$p_src/$f" ] && cp "$p_src/$f" "$p_cache/$f"
  done
  set_modes "$p_cache"
  echo "Installed ${PLUGIN_NAMES[$i]} plugin files → $p_cache"
  i=$((i + 1))
done
strip_gt_demo "$(plugin_cache 0)"

# Windows (0.20.1): a `python3` the MODEL's shell finds. Skills and docs tell Claude to run
# `python3 <tool>.py ...`; in Claude Code for Windows the Bash tool is Git Bash, where `python3`
# is the Microsoft Store stub (python.org installs python.exe and py.exe, never python3.exe).
# The shim runs the interpreter the preflight resolved, in UTF-8 mode, with \r stripped from
# piped output; at a terminal it execs Python directly so the REPL still works. It is written
# twice, identically, and marked so gt only ever replaces its own:
#   * ~/.claude/golden-thread/bin/python3 -- gt's own bin dir. Claude Code is pointed at it by
#     gt_components.py's SessionStart hook, which on Windows appends
#     `export PATH="$HOME/.claude/golden-thread/bin:$PATH"` and PYTHONUTF8=1 to $CLAUDE_ENV_FILE
#     -- the documented way for a SessionStart hook to set the environment of every later Bash
#     command (code.claude.com/docs/en/hooks, "Persist environment variables").
#   * ~/bin/python3 -- Git for Windows' per-user bin, which its /etc/profile.d/env.sh puts
#     first on PATH in every login shell, so `python3` also works in a Git Bash terminal. A
#     ~/bin/python3 that is not gt's is left alone, and said so.
# Removed by deleting both files (INSTALL.md, Uninstall). macOS and Linux never get one.
GT_SHIM_MARK="gt-python3-shim"
write_python3_shim() {  # $1 = the file
  {
    echo '#!/usr/bin/env bash'
    echo "# $GT_SHIM_MARK -- written by gt's install.sh (0.20.1). Delete this file to remove it."
    echo "# Windows: \`python3\` is otherwise the Microsoft Store stub. This runs the Python gt resolved,"
    echo "# in UTF-8 mode, with \\r stripped from piped output."
    printf 'PY=%q\n' "$GT_PYTHON"
    echo 'export PYTHONUTF8=1'
    echo 'if [ -t 1 ]; then exec "$PY" "$@"; fi'
    echo '"$PY" "$@" | tr -d '"'"'\r'"'"''
    echo 'exit "${PIPESTATUS[0]}"'
  } > "$1"
  chmod 755 "$1"
}
install_python3_shim() {
  local t="$HOME/bin/python3"
  mkdir -p "$HOME/.claude/golden-thread/bin"
  write_python3_shim "$HOME/.claude/golden-thread/bin/python3"
  if { [ -e "$t" ] || [ -L "$t" ]; } && ! grep -q "$GT_SHIM_MARK" "$t" 2>/dev/null; then
    echo "python3 shim → ~/.claude/golden-thread/bin/python3 (~/bin/python3 is not gt's; left as it is)"
  else
    mkdir -p "$HOME/bin"
    write_python3_shim "$t"
    echo "python3 shim → ~/bin/python3 and ~/.claude/golden-thread/bin/python3 (runs $GT_PYTHON)"
  fi
}

# 1b. Install the Core-rule hooks to a STABLE location outside the vault.
# settings.json references these by absolute path, so the path must survive project
# renames, merges and vault moves. The scripts locate the rules at run time.
GT_HOOKS="$HOME/.claude/golden-thread/hooks"
GT_MAC_SHIM=""        # macOS: the interpreter skills' python3 is pointed at, when needed (M9)
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
  # packs/ and MANIFEST.json beside the hooks dir (0.21.0). gt_registry.py takes the release to
  # be the parent of its own directory, and for the hook copy that parent is
  # ~/.claude/golden-thread/ -- which had no packs/, so the pre-commit secrets scan resolved
  # zero patterns and refused to run. MANIFEST.json travels with them: it is the integrity
  # anchor gt_registry checks every shipped pack against (it fails closed without it).
  rm -rf "${HOME:?}/.claude/golden-thread/packs"
  [ -d "$SRC/packs" ] && cp -r "$SRC/packs" "$HOME/.claude/golden-thread/packs"
  [ -f "$SRC/MANIFEST.json" ] && cp "$SRC/MANIFEST.json" "$HOME/.claude/golden-thread/MANIFEST.json"
  # templates/shipped-hashes.json and templates/tools/ beside the hooks dir too (0.22.0). The
  # broker and gt_accept run a vault's gt_session.py / gt_task.py only when its bytes are in that
  # hash list, else gt's own tools/ copy; their hook copies look in ../templates, and a machine
  # with no gt in the plugin cache had neither. Only those two travel, REPLACED like packs/ so a
  # re-run or an upgrade converges and a rollback to a release without the list leaves none.
  rm -rf "${HOME:?}/.claude/golden-thread/templates"
  if [ -f "$SRC/templates/shipped-hashes.json" ]; then
    mkdir -p "$HOME/.claude/golden-thread/templates/tools"
    cp "$SRC/templates/shipped-hashes.json" "$HOME/.claude/golden-thread/templates/"
    for f in "$SRC/templates/tools"/*.py; do
      [ -f "$f" ] && cp "$f" "$HOME/.claude/golden-thread/templates/tools/"
    done
    set_modes "$HOME/.claude/golden-thread/templates"
  fi
  # gt unlock's Touch ID helper (0.20.1, macOS). Built HERE from the shipped Swift source
  # (scripts/gt-presence.swift) with the Xcode Command Line Tools and ad-hoc signed, unless an
  # identical build is already in place. A Developer ID signed release binary installed with a
  # `gt-presence.release` marker beside it is never rebuilt over.
  # Without swiftc nothing fails: gt unlock still offers TOTP and says Touch ID is unavailable.
  # 0.20.1 review ("helper swap"): an existing binary is KEPT only when it matches its install
  # record (gt-presence.install.json: the built binary's sha256, and for a release binary its
  # Developer ID requirement) -- a binary nobody recorded is rebuilt, never trusted, and every
  # use re-checks the record (gt_unlock_touchid._check_helper).
  if [ "$(uname -s)" = Darwin ] && [ -f "$GT_HOOKS/gt_unlock_touchid.py" ] \
     && [ -f "$GT_HOOKS/gt-presence.swift" ]; then
    _gtp_bin="$HOME/.claude/golden-thread/bin"
    _gtp_sum=$(shasum -a 256 "$GT_HOOKS/gt-presence.swift" 2>/dev/null | cut -d' ' -f1)
    _gtp_ok() { python3 -B "$GT_HOOKS/gt_unlock_touchid.py" verify --helper "$_gtp_bin/gt-presence" >/dev/null 2>&1; }
    if [ -x "$_gtp_bin/gt-presence" ] && [ -f "$_gtp_bin/gt-presence.release" ] \
       && { _gtp_ok || python3 -B "$GT_HOOKS/gt_unlock_touchid.py" record --release --helper "$_gtp_bin/gt-presence" >/dev/null 2>&1; }; then
      echo "Touch ID helper → $_gtp_bin/gt-presence (signed release binary, kept)"
    elif [ -x "$_gtp_bin/gt-presence" ] && [ ! -f "$_gtp_bin/gt-presence.release" ] \
       && [ "$(cat "$_gtp_bin/gt-presence.source-sha256" 2>/dev/null)" = "$_gtp_sum" ] && _gtp_ok; then
      echo "Touch ID helper → $_gtp_bin/gt-presence (unchanged)"
    elif rm -f "$_gtp_bin/gt-presence.release" \
       && python3 -B "$GT_HOOKS/gt_unlock_touchid.py" build --dest "$_gtp_bin" >/dev/null 2>&1; then
      printf '%s\n' "$_gtp_sum" > "$_gtp_bin/gt-presence.source-sha256"
      echo "Touch ID helper → $_gtp_bin/gt-presence (built here, ad-hoc signed)"
    else
      echo "Touch ID helper not built (needs the Xcode Command Line Tools: xcode-select --install);"
      echo "  gt unlock can still use TOTP. It is off by default either way."
    fi
  fi
  # Windows: the interpreter the preflight resolved, for the hook wrappers (hooks/gt_python.sh
  # reads it). Their `python3` is otherwise the Store stub and every hook fails open, silently.
  if [ -n "${GT_PYTHON:-}" ]; then
    printf '%s\n' "$GT_PYTHON" > "$GT_HOOKS/../python"
    install_python3_shim
  fi
  # One interpreter for every scheduled job (0.19.1), then rewrite any installed job on
  # another one. A macOS privacy grant is per interpreter, so jobs on two pythons meant a
  # grant that covered only some of them. 0.20.1: CHOSEN, not overwritten -- the recorded
  # interpreter is kept (0.19.1/0.19.2 replaced it with the python running the install, and
  # under launchd Homebrew's python could not write the vault: every install broke the jobs),
  # and with a job installed the choice is proven by a write probe run under launchd.
  #
  # Each verb is asked for only when the INSTALLED gt_schedule.py has it (0.20.1): this installer
  # also installs older releases (a rollback), and 0.19.x's has no choose-interpreter -- the
  # install printed argparse's raw usage dump (usability run M3). Older ones fall back to what
  # they do have: record-interpreter (0.19.1), else nothing.
  if [ -f "$GT_HOOKS/gt_schedule.py" ]; then
    _gt_py=$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null) || _gt_py=""
    _gt_sched="$GT_HOOKS/gt_schedule.py"
    if [ -n "$_gt_py" ]; then
      if grep -q '"choose-interpreter"' "$_gt_sched" 2>/dev/null; then
        python3 -B "$_gt_sched" choose-interpreter --candidate "$_gt_py" \
          ${VAULT_ARG:+--vault "$VAULT_ARG"} || true
      elif grep -q '"record-interpreter"' "$_gt_sched" 2>/dev/null \
           && [ ! -f "$GT_HOOKS/../interpreter.json" ]; then
        python3 -B "$_gt_sched" record-interpreter "$_gt_py" >/dev/null 2>&1 || true
      fi
      # Jobs still under the old com.markethaven.* label move to io.goldenthread.* (0.20.1).
      if grep -q '"migrate-labels"' "$_gt_sched" 2>/dev/null; then
        python3 -B "$_gt_sched" migrate-labels || true
      fi
      if grep -q '"reconcile"' "$_gt_sched" 2>/dev/null; then
        python3 -B "$_gt_sched" reconcile || true
      fi
    fi
  fi
  # macOS (0.20.1): the SAME interpreter for the hook wrappers and the settings.json hook
  # commands. `python3` is often Homebrew's, and macOS can refuse it writes (a file carrying
  # com.apple.provenance) that /usr/bin/python3 is allowed: the vault's log.md, 2026-10-03.
  # choose-interpreter picked one that passed the write probe; hooks/gt_python.sh reads it here.
  if [ "$(uname -s 2>/dev/null)" = Darwin ] && [ -f "$GT_HOOKS/../interpreter.json" ]; then
    _gt_rec=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["python"])' \
      "$GT_HOOKS/../interpreter.json" 2>/dev/null) || _gt_rec=""
    if [ -n "$_gt_rec" ] && [ -x "$_gt_rec" ]; then
      printf '%s\n' "$_gt_rec" > "$GT_HOOKS/../python"
      echo "  hooks and tools run $_gt_rec (it passed gt's write probe)"
    fi
    # 0.20.1 (usability run M9): skills and the gt-vault MCP run the BARE `python3` -- the one
    # on PATH, often Homebrew's. When that one FAILED the write probe (macOS refused it vault
    # writes), skills' writes failed too; only hooks and jobs were covered. So, only then, the
    # same remedy Windows has: a python3 shim in gt's bin dir that runs the chosen interpreter,
    # put first on PATH for Claude's Bash commands by the SessionStart hook (CLAUDE_ENV_FILE),
    # and the installed gt-vault MCP started by that interpreter's absolute path (step 2c). A
    # PATH python3 that passed keeps working exactly as before -- no shim, nothing overridden.
    # Only a release whose SessionStart hook exports the macOS shim (gt_components._is_gt_shim)
    # gets one: a rollback to an older gt removes it instead of leaving a file nothing reads.
    if [ -n "$_gt_rec" ] && [ -x "$_gt_rec" ] \
       && grep -q "def _is_gt_shim" "$SRC/scripts/gt_components.py" 2>/dev/null \
       && python3 - "$GT_HOOKS/../interpreter.json" <<'PYEOF'
import json, os, sys
try:
    refused = json.load(open(sys.argv[1], encoding="utf-8")).get("refused") or []
except (OSError, ValueError, AttributeError):
    refused = []
here = os.path.realpath(sys.executable)
sys.exit(0 if any(os.path.realpath(str(r)) == here for r in refused) else 1)
PYEOF
    then
      GT_MAC_SHIM="$_gt_rec"
      mkdir -p "$HOME/.claude/golden-thread/bin"
      {
        echo '#!/bin/sh'
        echo "# $GT_SHIM_MARK -- written by gt's install.sh (0.20.1). Delete this file to remove it."
        echo "# macOS: the python3 on PATH failed gt's write probe, so Claude's Bash commands run"
        echo "# the interpreter that passed it. Re-running install.sh rewrites or removes this."
        printf 'exec %q "$@"\n' "$_gt_rec"
      } > "$HOME/.claude/golden-thread/bin/python3"
      chmod 755 "$HOME/.claude/golden-thread/bin/python3"
      echo "  skills' python3 → $_gt_rec in Claude Code (shim: $HOME/.claude/golden-thread/bin/python3;"
      echo "    $(python3 -c 'import sys; print(sys.executable)') failed the write probe)"
    elif grep -q "$GT_SHIM_MARK" "$HOME/.claude/golden-thread/bin/python3" 2>/dev/null; then
      rm -f "$HOME/.claude/golden-thread/bin/python3"     # no longer needed: converge
    fi
  fi

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
        [sys.executable, os.path.join(src, "scripts", "gt_components.py"), "hookdir-scripts",
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
    echo "Release file list (MANIFEST.json) present — installed files are checked against it"
  elif git -C "$SRC" ls-files --error-unmatch MANIFEST.json >/dev/null 2>&1; then
    # Unreachable in a normal run -- verify_source_tree refuses this case before we get here.
    # Kept as a second, independent refusal rather than a comment: the generate step is what
    # would actually do the damage, and a guard that depends on an earlier guard still running
    # is one edit away from being no guard at all.
    echo "NOT writing a MANIFEST: it is tracked in this tree and missing from disk."
    echo "  Restore it with: git -C \"$SRC\" checkout -- MANIFEST.json"
    exit 6
  else
    python3 "$SRC/scripts/gt_components.py" manifest "$SRC" >/dev/null 2>&1 \
      && echo "Wrote component MANIFEST → $SRC/MANIFEST.json  (none was present, no git)"
  fi

fi

# 2. Create marketplace directory structure: one entry per plugin.
#
# Each entry's description is read from that plugin's own plugin.json. Until 0.14.0
# marketplace.json carried hand-typed descriptions for gt and gt-wiki, which had already
# drifted from the manifests -- and a third plugin would have had no entry at all.
if [ "$MARKET_MODE" = external ]; then
  echo "Marketplace $MARKET_NAME is managed by Claude Code (/plugin marketplace add): left as it is"
else
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
with open(out, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps({"name": market,
                         "owner": {"name": "Stacy Haven"},
                         "plugins": plugins}, indent=2) + "\n")
EOF
# Rewriting a file keeps its old mode, so marketplace.json needs set_modes like the plugin
# dirs below (an upgrade kept a 0600 copy where a fresh install wrote 0644).
set_modes "$MARKETPLACE/.claude-plugin"
chmod 755 "$MARKETPLACE" "$MARKETPLACE/plugins" 2>/dev/null || true
fi

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
while [ "$MARKET_MODE" = local ] && [ "$i" -lt "$PLUGIN_COUNT" ]; do
  p_src=$(plugin_src "$i"); p_market="$MARKETPLACE/plugins/${PLUGIN_NAMES[$i]}"
  cp "$p_src/.claude-plugin/plugin.json" "$p_market/.claude-plugin/plugin.json"
  for dir in skills scripts templates commands hooks demo packs agents workflows help walkthroughs docs tests; do
    rm -rf "$p_market/$dir"
    [ -d "$p_src/$dir" ] && cp -r "$p_src/$dir" "$p_market/$dir"
  done
  for f in module.json MANIFEST.json retired.json SPEC.md; do   # MANIFEST.json: see the cache loop above
    rm -f "$p_market/$f"
    [ -f "$p_src/$f" ] && cp "$p_src/$f" "$p_market/$f"
  done
  set_modes "$p_market"
  i=$((i + 1))
done
[ "$MARKET_MODE" = local ] && strip_gt_demo "$MARKETPLACE/plugins/gt"
[ "$MARKET_MODE" = local ] && echo "Populated marketplace plugin directories with skills/scripts/templates"

# 2c. macOS (0.20.1, M9): when the python3 on PATH failed the write probe, the gt-vault MCP --
# started by Claude Code with that bare `python3` -- could not write the vault either. Its
# INSTALLED manifests get the interpreter that passed, by absolute path (the same rewrite as
# Windows below; /usr/bin/python3 execs the real interpreter in place, so the server is still a
# direct child of claude). Re-copied from source on every install, so this converges both ways.
if [ -n "$GT_MAC_SHIM" ]; then
  _gt_mcp_files=()
  i=0
  while [ "$i" -lt "$PLUGIN_COUNT" ]; do
    for _f in "$(plugin_cache "$i")/.claude-plugin/plugin.json" \
              "$MARKETPLACE/plugins/${PLUGIN_NAMES[$i]}/.claude-plugin/plugin.json"; do
      [ -f "$_f" ] && _gt_mcp_files+=("$_f")
    done
    i=$((i + 1))
  done
  python3 -B "$SRC/scripts/gt_components.py" localize-mcp --python "$GT_MAC_SHIM" \
    ${_gt_mcp_files[@]+"${_gt_mcp_files[@]}"} >/dev/null \
    || echo "  ⚠ could not point the plugins' MCP servers at $GT_MAC_SHIM; they run the PATH python3"
fi

# 2c. Windows: an MCP server a plugin starts with `python3` (gt-lotr) would run the Microsoft
# Store stub and never start. Its INSTALLED manifests (cache and marketplace) get the
# interpreter the preflight resolved, as an absolute Windows path, so Claude Code starts it
# directly -- still a direct child of claude, which gt unlock's register_shim requires.
# gt_components.localize_mcp says why at length. macOS and Linux: untouched.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    if [ -n "${GT_PYTHON:-}" ]; then
      _gt_pyw=$(cygpath -m "$GT_PYTHON" 2>/dev/null || printf '%s' "$GT_PYTHON")
      _gt_mcp_files=()
      i=0
      while [ "$i" -lt "$PLUGIN_COUNT" ]; do
        for _f in "$(plugin_cache "$i")/.claude-plugin/plugin.json" \
                  "$MARKETPLACE/plugins/${PLUGIN_NAMES[$i]}/.claude-plugin/plugin.json"; do
          [ -f "$_f" ] && _gt_mcp_files+=("$_f")
        done
        i=$((i + 1))
      done
      python3 -B "$SRC/scripts/gt_components.py" localize-mcp --python "$_gt_pyw" \
        ${_gt_mcp_files[@]+"${_gt_mcp_files[@]}"} \
        || echo "  ⚠ could not point the plugins' MCP servers at $_gt_pyw; gt-lotr's will not start"
    fi
    ;;
esac

[ "$MARKET_MODE" = local ] && echo "Created marketplace entries → $MARKETPLACE"

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
    # newline="\n" (0.20.1): Windows Python writes CRLF in text mode, and settings.json
    # flipped between LF and CRLF from one install to the next (usability run, Windows).
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(d, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

if os.environ.get("GT_MARKET_MODE") == "external":
    print("known_marketplaces.json: %s is Claude Code's own entry, left as it is" % market_name)
else:
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
# The gt-all bundle (0.22.0): `/plugin install gt-all` installs it, with every module as a
# dependency, and a dependent forces its dependencies on -- so it would undo --without NAME.
# It stays only while the Claude-managed marketplace can load it AND every plugin it names is
# among the plugins being installed; otherwise it goes (entry, enabled flag, cache copy), and
# the person's modules are whatever they chose. Nothing else about it is touched.
bundle_key = "gt-all@" + market_name
drop_bundle = False
if bundle_key in d.get("plugins", {}):
    keep = False
    if os.environ.get("GT_MARKET_MODE") == "external":
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(src)))     # the plugin root
            pj = os.path.join(root, "bundles", "gt-all", ".claude-plugin", "plugin.json")
            deps = json.load(open(pj, encoding="utf-8")).get("dependencies") or []
            have = {k.split("@")[0] for k, _p, _v in PLUGINS}
            keep = all(x in have for x in deps)
        except (OSError, ValueError, AttributeError):
            keep = False
    if not keep:
        gone = d["plugins"].pop(bundle_key)
        import shutil
        for e in gone if isinstance(gone, list) else []:
            ip = e.get("installPath") if isinstance(e, dict) else None
            if ip and os.path.basename(os.path.dirname(ip)) == "gt-all" \
                    and os.path.basename(os.path.dirname(os.path.dirname(ip))) == market_name:
                shutil.rmtree(os.path.dirname(ip), ignore_errors=True)
        print("Removed the gt-all bundle registration (%s)"
              % ("not every module it names is installed" if os.environ.get("GT_MARKET_MODE") == "external"
                 else "this marketplace does not carry it"))
        drop_bundle = True
order_plugin_keys(d.get("plugins"), ORDER, market_name)
save(installed, d)
print("Registered in installed_plugins.json")

d = load(settings, {})
for key, _path, _ver in PLUGINS:
    d.setdefault("enabledPlugins", {})[key] = True
if drop_bundle:
    d.get("enabledPlugins", {}).pop(bundle_key, None)
order_plugin_keys(d.get("enabledPlugins"), ORDER, market_name)
save(settings, d)
print("Registered in settings.json  (backups in %s)" % BACKUPS)
EOF


# 5b. Model and effort per skill (0.19.1, request model-and-effort-per-plugin). The profile is,
# in order: --model-profile this run, the recorded choice, `average` on a NEW install, a one-time
# question on an interactive upgrade (average preselected), else `inherit` -- a scripted upgrade
# changes nothing unasked and does not use up the question. Written into the installed copies
# only; the release source is never edited.
MP="$SRC/scripts/gt_model_policy.py"
if [ -f "$MP" ]; then
  _mp_vault=()
  if [ -n "${VAULT_ARG:-}" ]; then _mp_vault=(--vault "$VAULT_ARG"); fi
  _prof="$MODEL_PROFILE"
  # the install questions (item #33) decided: an answer given or asked is used as it is; a
  # kept/defaulted one falls through to the recorded choice and the rules below
  if [ "${PLAN_model_profile_SRC:-}" = choice ] || [ "${PLAN_model_profile_SRC:-}" = accepted ]; then _prof="$PLAN_model_profile"; fi
  # a defaulted model_profile is the token profile's value for a machine that never recorded one
  # (fix B2, 0.21.4): the profile writes it below anyway, so say the same thing here
  if [ "${PLAN_model_profile_SRC:-}" = default ] && [ -n "${PLAN_model_profile:-}" ]; then _prof="$PLAN_model_profile"; fi
  if [ -z "$_prof" ]; then _prof=$(python3 -B "$MP" current --home "$HOME" 2>/dev/null || true); fi
  if [ -z "$_prof" ]; then
    if [ -z "${GT_PREVIOUS_RELEASE:-}" ]; then
      _prof=average
    elif [ -t 0 ] && [ -t 1 ]; then
      question_break
      echo "gt can now run each skill at a model and effort that fits it:"
      for _p in average very-high inherit; do python3 -B "$MP" table --profile "$_p" ${_mp_vault[@]+"${_mp_vault[@]}"} | sed 's/^/  /'; done
      printf 'Model profile [A = average / v = very-high / i = inherit (as before)]: '
      read -r _ans </dev/tty || _ans=""
      case "$_ans" in v|V|very-high) _prof=very-high ;; i|I|inherit) _prof=inherit ;; *) _prof=average ;; esac
    else
      _prof=inherit
      echo "Model profile: inherit (an upgrade with no terminal changes nothing; choose with --model-profile)"
      _MP_NO_RECORD=yes
    fi
  fi
  if [ "${_MP_NO_RECORD:-}" != yes ]; then
    python3 -B "$MP" choose "$_prof" --home "$HOME" >/dev/null || { echo "✗ --model-profile: $_prof is not average, very-high or inherit"; exit 1; }
  fi
  python3 -B "$MP" table --profile "$_prof" ${_mp_vault[@]+"${_mp_vault[@]}"} || true
  python3 -B "$MP" apply --profile "$_prof" --home "$HOME" ${_mp_vault[@]+"${_mp_vault[@]}"} || \
    echo "  ⚠ some skills were left at the session's model (a refused combination is named above)"
fi

# 5c. Lockdown level (0.20.5): how much Claude may run without asking. The level is, in order:
# --lockdown this run, else a question at a terminal (Return keeps the current level, which is
# strict on a new install), else -- no terminal -- the current level, unchanged and named.
# gt_lockdown.py writes only gt's own allow/deny rules into ~/.claude/settings.json, records
# them, and backs the file up first; strict on a machine gt never loosened writes nothing.
LD="$SRC/scripts/gt_lockdown.py"
if [ -f "$LD" ]; then
  _ld="$LOCKDOWN"
  _ld_cur=$(python3 -B "$LD" current 2>/dev/null || echo strict)
  # asked or given through the install questions (item #33); a kept level is left alone
  if [ "${PLAN_lockdown_SRC:-}" = choice ] || [ "${PLAN_lockdown_SRC:-}" = accepted ] || [ "${PLAN_lockdown_SRC:-}" = default ]; then _ld="$PLAN_lockdown"; fi
  if [ -z "$_ld" ] && [ -z "${PLAN_lockdown_SRC:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    question_break
    echo "How much may Claude run without asking? (the lockdown level; these levels reduce prompts,"
    echo "they are not a security boundary -- change it any time: gt_settings.py set lockdown <level>)"
    python3 -B "$LD" table | sed 's/^/  /'
    printf 'Lockdown level [1 strict / 2 trust local / 3 trust network / 4 insecure] (Return keeps %s): ' "$(echo "$_ld_cur" | tr '-' ' ')"
    read -r _ans </dev/tty || _ans=""
    case "$_ans" in
      1|very*)    _ld=strict ;;
      2|mostly*)  _ld=trust-local ;;
      3|partly*)  _ld=trust-network ;;
      4|insecure) _ld=insecure ;;
      *)          _ld="$_ld_cur" ;;
    esac
  fi
  # paranoid (0.21.0) is strict plus the paranoid switch: the level part is applied here, the
  # switch with the other settings below (PLAN_paranoid), where a refusal is said out loud
  if [ "$_ld" = paranoid ]; then _ld=strict; fi
  if [ -n "$_ld" ]; then
    python3 -B "$LD" apply "$_ld" || { echo "✗ lockdown: could not apply $_ld (settings.json left as it was)"; exit 1; }
  else
    # a kept level is re-merged all the same: a module rule names its script by the installed
    # path ({scripts}), which holds the release, so last release's rules would name a folder
    # this install just replaced
    # (an older release's gt_lockdown.py may not know `refresh`: say so, never fail the install)
    python3 -B "$LD" refresh >/dev/null 2>&1 || echo "! lockdown: rules not re-merged for $_ld_cur -- run gt_settings.py set lockdown $_ld_cur"
  fi
  echo "Lockdown: $(python3 -B "$LD" current 2>/dev/null | tr '-' ' ')  (choose with --lockdown <level>, or gt_settings.py set lockdown <level>)"
  if [ -n "${PLAN_lockdown_SRC:-}" ]; then echo "  lockdown: ${PLAN_lockdown} ($(plan_words "$PLAN_lockdown_SRC"))"; fi
fi

# 5c-persona (0.21.0). Who is using gt: the persona shapes a new project's extra files, never
# enforcement. Asked beside the lockdown level: --persona a,b this run (none clears), else a
# question at a terminal (Return keeps the current choice), else -- no terminal -- unchanged.
# The choice is recorded after the vault step below, because the setting lives in vault-config.json.
PS="$SRC/scripts/gt_persona.py"
if [ -f "$PS" ]; then
  _pr_cur=$(python3 -B "$SRC/scripts/gt_settings.py" get persona 2>/dev/null || true)
  if [ "$PERSONA_SET" = no ] && [ -z "${PLAN_persona_SRC:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    question_break
    echo "Who is using Golden Thread? A persona adds files to a new project (it never changes what gt enforces)."
    python3 -B "$PS" list | sed 's/^/  /'
    printf 'Persona, comma-separated, or none (Return keeps %s): ' "${_pr_cur:-none}"
    read -r _ans </dev/tty || _ans=""
    if [ -n "$_ans" ]; then PERSONA="$_ans"; PERSONA_SET=yes; fi
  fi
  [ "$PERSONA" = none ] && PERSONA=""
  echo "Persona: ${PERSONA:-${_pr_cur:-none}}  (choose with --persona a,b, or gt_settings.py set persona a,b)"
  if [ -n "${PLAN_persona_SRC:-}" ]; then echo "  persona: ${PLAN_persona} ($(plan_words "$PLAN_persona_SRC"))"; fi
fi

# 5c. The status line (0.21.0). gt-usage's allowance meter shows in Claude Code's status line
# only with a `statusLine` entry in settings.json -- the user's own display, so no installer
# wrote it, and people never found the one line in the skill that says how. Offered here, at a
# terminal only, defaulting to no; never when a status line already exists (it is theirs), and
# never in an unattended run (one line says how instead).
offer_statusline() {
  local u="$HOME/.claude/golden-thread/hooks/gt_usage.py" on
  [ -f "$u" ] || return 0
  on=$(python3 -c 'import json,sys
try: print(any(m.get("name") == "usage" and m.get("state") == "on" for m in json.load(open(sys.argv[1]))))
except Exception: print(False)' "$MODJSON" 2>/dev/null || echo False)
  [ "$on" = True ] || return 0
  if python3 -c 'import json,os,sys
p = os.path.expanduser("~/.claude/settings.json")
try: d = json.load(open(p))
except Exception: d = {}
sys.exit(0 if isinstance(d, dict) and d.get("statusLine") else 1)' 2>/dev/null; then
    return 0
  fi
  if [ -n "${PLAN_statusline_SRC:-}" ] && [ "${PLAN_statusline_SRC}" = choice ]; then
    case "$PLAN_statusline" in
      yes) python3 "$u" --setup-statusline || echo "  ⚠ the status line could not be added"
           echo "  statusline: yes ($(plan_words "$PLAN_statusline_SRC"))" ;;
      *)   echo "  Status line not set. Later: python3 \"$u\" --setup-statusline"
           echo "  Without it gt cannot read the context size, so the context nudge stays silent."
           echo "  statusline: no ($(plan_words "$PLAN_statusline_SRC"))" ;;
    esac
  elif [ -z "${PLAN_statusline_SRC:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    question_break
    printf 'Show the plan-allowance meter in Claude Code'"'"'s status line? [y/N]: '
    local ans=""; read -r ans </dev/tty || ans=""
    case "$ans" in
      y|Y|yes|YES) python3 "$u" --setup-statusline || echo "  ⚠ the status line could not be added" ;;
      *) echo "  Status line not set. Later: python3 \"$u\" --setup-statusline"
         echo "  Without it gt cannot read the context size, so the context nudge stays silent." ;;
    esac
  else
    echo "Status line: not set (no terminal to ask). To show the allowance meter: python3 \"$u\" --setup-statusline"
    echo "  Without a status line gt cannot read the context size, so the context nudge stays silent."
  fi
}

# 6. Register the SessionStart / PreCompact / SessionEnd hooks.
#
# SessionStart  -> gt_components.py check   : is what is INSTALLED still what is
#                  CHECKED IN? Found live on 2026-08-29 that guard_session_claims.sh
#                  was installed but absent from the plugin source entirely, and
#                  validate_response.sh had drifted -- two of three enforcement
#                  mechanisms existing on one machine only.
# PreCompact / SessionEnd -> gt_report_card.py: since 0.15.0 declared by the report-card
#                  module's module.json and wired here exactly like gt's own entries.
#
# TWO ROUTES since 0.21.0 (gt_components.py "plugin-shipped hooks" has the why):
#   plugin    the gt plugin and each module plugin ship hooks/hooks.json, whose entries source
#             a per-machine command from ~/.claude/golden-thread/wired/. This step writes
#             those files and REMOVES gt's own entries from settings.json (a user's hooks are
#             kept); settings.json is written only when there is something of gt's to remove.
#   settings  settings.json is wired exactly as 0.20.0 did, and the wired dir is removed so
#             the plugins' entries stay no-ops (nothing runs twice).
# The route: --hook-mode / GT_HOOK_MODE, else plugin from Claude Code 2.1.136, else settings.
# Exported so vault_init (step 7) wires the enforcement hooks on the same route.
GT_HOOK_MODE=$(GT_HOOK_MODE="${GT_HOOK_MODE:-}" python3 -B "$SRC/scripts/gt_components.py" \
                 hook-mode --choose 2>/dev/null || true)
case "$GT_HOOK_MODE" in plugin|settings) ;; *) GT_HOOK_MODE=settings ;; esac
export GT_HOOK_MODE
echo "Hook route: $GT_HOOK_MODE  ($(python3 -B "$SRC/scripts/gt_components.py" hook-mode 2>/dev/null | sed 's/^route to wire: //' || true))"
python3 - "$SRC" "$SCRIPT_DIR" "$MODJSON" <<'EOF'
import json, os, subprocess, sys, tempfile
src, script_dir, modjson = sys.argv[1:4]
route = os.environ.get('GT_HOOK_MODE') or 'settings'
p = os.path.expanduser('~/.claude/settings.json')
d = json.load(open(p)) if os.path.exists(p) else {}
before = json.dumps(d, sort_keys=True)
had_hooks = 'hooks' in d
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
    [sys.executable, os.path.join(src, 'scripts', 'gt_components.py'),
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
want = [(r['event'], r['script'], r['command'], r.get('matcher'), r.get('timeout'))
        for r in regs if r.get('owner') == 'install.sh']

changed = []
for event, script, cmd, matcher, timeout in (want if route == 'settings' else []):
    arr = hooks.setdefault(event, [])
    # Replace the entry for THIS script only, not every golden-thread entry for the
    # event -- SessionStart has four, and wiping by event would delete the
    # siblings registered moments earlier in this same loop.
    arr[:] = [e for e in arr
              if not any(script in (h.get('command') or '')
                         for h in e.get('hooks', []))]
    # `timeout` sits on the HOOK, `matcher` on the BLOCK -- that is Claude Code's shape,
    # not a choice made here. Both are omitted when unset so an entry that wants neither
    # is byte-identical to what every release before 0.16.2 wrote; an upgrade that changes
    # nothing should show as changing nothing.
    entry = {'type': 'command', 'command': cmd}
    if timeout is not None:
        entry['timeout'] = timeout
    block = {'hooks': [entry]}
    if matcher is not None:
        block['matcher'] = matcher
    arr.append(block)
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
# On the plugin route no (event, script) of gt's belongs in settings.json: every entry that
# runs a known gt hooks-dir script is removed below, the enforcement ones included (step 7
# wires those as files). Unknown hooks-dir entries and the user's own hooks stay.
pairs = {(r['event'], r['script']) for r in regs} if route == 'settings' else set()
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

if route == 'plugin' and not had_hooks and not hooks:
    del d['hooks']             # never ADD an empty "hooks" key on the plugin route
# The plugin route writes settings.json only to take gt's entries OUT of it. The settings
# route writes it every time, as every release before 0.21.0 did.
if route == 'settings' or json.dumps(d, sort_keys=True) != before:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix='.gt-')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps(d, indent=2) + '\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)

# The wired files (plugin route): install.sh's own entries now; the enforcement entries are
# vault_init's (step 7), written only against a vault. A wired file no registration names any
# more (a retired hook, a module switched off) is removed. The settings route removes the
# whole dir, so a plugin's hooks.json entries do nothing there.
if route == 'plugin':
    _all = [dict(r) for r in regs]
    _gc.write_wired([r for r in _all if r.get('owner') == 'install.sh'])
    for g in _gc.prune_wired({_gc.wired_name(r['event'], r['script']) for r in _all}):
        print('Removed wired hook %s (no longer registered)' % g)
else:
    # Inline, not _gc: a rollback installs a gt whose gt_components predates the plugin
    # route, and its wired files would only sit there inert (its plugin ships no hooks.json).
    import shutil
    _wd = os.path.expanduser('~/.claude/golden-thread/wired')
    try:
        os.unlink(os.path.expanduser('~/.claude/golden-thread/wired-manifest.json'))
    except OSError:
        pass
    if os.path.isdir(_wd):
        shutil.rmtree(_wd, ignore_errors=True)
        print('Hook route is settings: removed ~/.claude/golden-thread/wired/')

# Say what was wired, by event and by count. The old message printed a bare list
# after the "Restart Claude Code" line, where it read as trailing noise past the
# end of the output -- so an install that wired nothing looked no different from
# one that wired everything.
by_event = {}
for c in changed:
    ev, sc = c.split('/', 1)
    by_event.setdefault(ev, []).append(sc)
if route == 'settings':
    # Both numbers (0.20.1): "Registered 18" here and "all 24 declared hooks are wired" from the
    # gate read as six going missing. The rest are the enforcement hooks, wired against the vault.
    others = len([r for r in regs if r.get('owner') != 'install.sh'])
    print('Registered %d hooks in ~/.claude/settings.json%s:'
          % (len(changed), ' (%d more, the Core-rule enforcement hooks, are wired against the '
             'vault: %d in all)' % (others, len(changed) + others) if others else ''))

else:
    for r in regs:
        if r.get('owner') == 'install.sh':
            by_event.setdefault(r['event'], []).append(r['script'])
    print('Wired %d hooks for the plugins\' hooks.json (~/.claude/golden-thread/wired/; '
          'settings.json not used):' % sum(len(v) for v in by_event.values()))
for ev in sorted(by_event):
    print('  %-16s %s' % (ev, ', '.join(sorted(by_event[ev]))))
if removed and route == 'plugin':
    print('Moved %d gt hook entr%s out of ~/.claude/settings.json -- the plugins\' hooks.json '
          'runs them now; your own hooks are untouched (backup: %s):'
          % (len(removed), 'y' if len(removed) == 1 else 'ies', bak))
elif removed:
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

offer_statusline || true

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
    echo "INSTALL INCOMPLETE — machine migration failed; the install is rolled back; fix and re-run install.sh"
    echo "════════════════════════════════════════════════════════════════════════"
    echo "The migrator exited $rc ($( [ "$rc" -eq 2 ] && echo "could not evaluate" || echo "a migration failed" ))."
    echo "The vault step did not run; what this install wrote is put back below."
    echo "  Status:  $U_PY \"$(upath "$mig")\" status --release \"$(upath "$SRC")\""
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
    [ -n "${_GT_MODPY_FILE:-}" ] && rm -f "$_GT_MODPY_FILE"    # exec runs no EXIT trap
    export GT_INSTALL_RERUN=1
    export GT_INSTALL_SNAPSHOT="$GT_SNAP"      # a failure in the re-run restores THIS run's start
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
# ~/GoldenThread since 0.21.4 (was ~/Documents/GoldenThread): macOS and Windows guard Documents,
# and gt writes into its vault constantly. gt_vault_location.py says why, for any path chosen.
DEFAULT_VAULT="$HOME/GoldenThread"
if [ -n "${USERPROFILE:-}" ] && command -v cygpath >/dev/null 2>&1; then
  DEFAULT_VAULT="$(cygpath -u "$USERPROFILE")/GoldenThread"       # %USERPROFILE%\GoldenThread
fi
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
  echo "Backing up the vault's gt-managed files before changing them..."
  if with_heartbeat "backing up the vault" python3 "$SRC/scripts/vault_refresh.py" backup \
       --vault "$real" --out "$VAULT_PREWRITE_BACKUP" >/dev/null 2>&1; then
    echo "  backed up (${HEARTBEAT_ELAPSED}s)"
  else
    echo "⚠ could not back up the vault's files before installing — continuing"; VAULT_PREWRITE_BACKUP=""
  fi
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

hook_dest() {  # where step 7 wires the enforcement hooks, by route (0.21.0)
  if [ "${GT_HOOK_MODE:-settings}" = plugin ]; then
    echo "~/.claude/golden-thread/wired/ (run by the gt plugin's hooks.json)"
  else
    echo "~/.claude/settings.json"
  fi
}

vault_init_reason() {  # $1 = vault_init.py's output: say WHY it refused (0.21.4), not just that it did
  VI_OUT="$1" python3 -c '
import json, os, sys
text = os.environ.get("VI_OUT", "")
rows = []
try:
    rows = [r for r in json.loads(text) if isinstance(r, dict) and r.get("action") in ("conflict", "error")]
except ValueError:
    pass
if rows:
    for r in rows[:5]:
        print("    reason: %s %s -- %s" % (r.get("action"), r.get("path", ""), r.get("note", "")))
else:
    tail = [l for l in text.splitlines() if l.strip()][-3:]
    for l in tail or ["(vault_init.py printed nothing; run it by hand to see why)"]:
        print("    reason: " + l)
' 2>/dev/null || echo "    reason: (not available)"
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
    local vi_out
    vi_out=$(GT_VAULT_LOCATION_WARNED=1 python3 "$SRC/scripts/vault_init.py" fresh --vault "$target" --domain "Personal" 2>&1) \
      || { echo "⚠ could not create the vault at $target"
           vault_init_reason "$vi_out"; return 1; }
    VAULT_CREATED_NOW=$(real_path "$target")
    VAULT_GIT_PREEXISTED="$git_before"
  else
    echo "Connecting the existing vault at $target"
    local vi_out
    vi_out=$(GT_VAULT_LOCATION_WARNED=1 python3 "$SRC/scripts/vault_init.py" connect --vault "$target" 2>&1) \
      || { echo "⚠ could not connect $target"
           vault_init_reason "$vi_out"; return 1; }
  fi
  if out=$(python3 "$SRC/scripts/vault_init.py" install-core-rules --vault "$target" 2>/dev/null); then
    echo "Wired enforcement hooks → $(hook_dest)"
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

# Vault location (0.21.4): a vault in Documents or on a synced or shared drive (iCloud, Dropbox,
# OneDrive, Google Drive, Box, a network share) is WARNED about, in plain words with the reason,
# and ~/GoldenThread recommended -- never refused. At a terminal a NEW vault's path is confirmed
# (keep it, take the recommended one, or type another); an existing vault is only warned about,
# and without a terminal the warning goes to stderr and the install carries on. The detection is
# gt_vault_location.py, the one helper /gt:gt-setup, /gt:gt-init and gt_doctor use too.
# -> sets VL_CHOSEN to the path to use.
vault_location_check() {  # $1 = the vault path chosen
  local path="$1" out rc ans tries=0
  VL_CHOSEN="$path"
  [ -f "$SRC/scripts/gt_vault_location.py" ] || return 0      # an older gt (a rollback)
  while :; do
    rc=0
    out=$(python3 "$SRC/scripts/gt_vault_location.py" check "$VL_CHOSEN" 2>/dev/null) || rc=$?
    [ "$rc" -eq 1 ] || return 0
    if [ -t 0 ] && [ -t 1 ] && [ ! -d "$VL_CHOSEN/Projects" ] && [ ! -f "$VL_CHOSEN/index.md" ] \
       && [ "$tries" -lt 3 ]; then
      question_break
      printf '%s\n' "$out"
      printf 'Keep it? [y = keep this path / n = use %s / or type another path]: ' "$DEFAULT_VAULT"
      read -r ans </dev/tty || ans="y"
      case "$ans" in
        ""|y|Y|yes|YES) return 0 ;;
        n|N|no|NO)      VL_CHOSEN="$DEFAULT_VAULT" ;;
        *)              VL_CHOSEN="$ans" ;;
      esac
      tries=$((tries + 1))
    else
      printf '%s\n' "$out" >&2
      return 0
    fi
  done
}

# --vault (or $GT_VAULT) was given: do the whole job in one command. Runs before the
# config is read, so the path the user named is the one that gets set up and wired.
if [ -n "$VAULT_ARG" ] && [ "$NO_VAULT" != yes ]; then
  vault_location_check "$VAULT_ARG"
  setup_vault "$VL_CHOSEN" || true
fi
VAULT_PATH=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null)
# --only (0.21.4) never touches the vault: no backup, no hook seeding, no refresh, no upgrade.
if [ -n "$ONLY" ]; then VAULT_PATH=""; fi
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
    if grep -q '"action": "updated"' <<<"$out"; then
      echo "Wired enforcement hooks → $(hook_dest)"
    else
      echo "Enforcement hooks already wired"
    fi
  else
    echo "⚠ Could not wire the enforcement hooks. Run this, then restart:"
    echo "  $U_PY \"$(upath "$SRC/scripts/vault_init.py")\" install-core-rules --vault \"$(upath "$vault")\""
  fi
}

if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then
  snapshot_vault_state "$VAULT_PATH"
  backup_vault_before_writes "$VAULT_PATH"
  wire_enforcement_hooks "$VAULT_PATH"
fi

# gt-farm (0.22.0): local routing needs <vault>/farm-ladder.tsv, and nothing used to say so. When the farm module
# is on and the vault is known, place the template once; an existing file is never touched.
if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ] && [ -z "$ONLY" ]; then
  _farm_send=$(ls "$SCRIPT_DIR"/golden-thread-farm/*/scripts/gt_farm_send.py 2>/dev/null | sort | tail -1) || _farm_send=""
  if [ -n "$_farm_send" ] && python3 -c "import json,os,sys;p=os.path.expanduser('~/.claude/golden-thread/install-choices.json');sys.exit(0 if json.load(open(p)).get('choices',{}).get('farm')=='on' else 1)" 2>/dev/null; then
    GT_CORE_SCRIPTS="$SRC/scripts" python3 "$_farm_send" setup --apply-template 2>&1 | sed 's/^/  /' || true
  fi
fi

# The vault_refresh.py to run (0.20.1). Normally the release's own. On a ROLLBACK the older
# release's copy knows the tool and hook texts gt shipped up to ITSELF only, so every file a
# newer release put in the vault matched "no version gt has shipped" and was reported MODIFIED
# LOCALLY -- 13 false warnings on a rollback to 0.19.2 (usability run M3). So a rollback runs
# that same script from a scratch copy of the release whose shipped-hashes.json is the union
# with the newest release's: a newer release's text is then gt's own, and is put back to the
# rolled-back release's copy, as it should be. The release tree itself is never modified.
vault_refresh_script() {
  local newest m
  newest="$SCRIPT_DIR/$CORE_DIR/$(latest_version "$SCRIPT_DIR/$CORE_DIR")"
  if [ "$newest" = "$SRC" ] || [ ! -f "$newest/templates/shipped-hashes.json" ] \
     || [ ! -f "$SRC/templates/shipped-hashes.json" ]; then
    printf '%s\n' "$SRC/scripts/vault_refresh.py"; return 0
  fi
  m="$GT_TMP/refresh-release"
  if [ ! -f "$m/scripts/vault_refresh.py" ]; then
    mkdir -p "$m" && cp -R "$SRC/scripts" "$SRC/templates" "$SRC/.claude-plugin" "$m/" \
      && python3 - "$SRC/templates/shipped-hashes.json" "$newest/templates/shipped-hashes.json" \
           "$m/templates/shipped-hashes.json" <<'PYEOF' \
      || { printf '%s\n' "$SRC/scripts/vault_refresh.py"; return 0; }
import json, sys
old, new, out = sys.argv[1:4]
a = json.load(open(old, encoding="utf-8"))
b = json.load(open(new, encoding="utf-8"))
for kind, names in b.items():
    if isinstance(names, dict):
        mine = a.setdefault(kind, {})
        for n, hashes in names.items():
            mine[n] = sorted(set(mine.get(n, [])) | set(hashes))
with open(out, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(a, fh, indent=1, sort_keys=True)
PYEOF
  fi
  printf '%s\n' "$m/scripts/vault_refresh.py"
}

# Ask git, not the filesystem: .git is a FILE for a worktree, a submodule or a
# --separate-git-dir checkout, and `-d .git` skipped all of those silently.
if [ -n "$VAULT_PATH" ] && [ "$STARTER" != yes ] && git -C "$VAULT_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  # .githooks/, the vault tools and core.hooksPath, refreshed by CONTENT (0.15.0): a copy
  # gt shipped is replaced, the owner's own is kept (or, for a committed hook edit, backed
  # up first). The rules and why: scripts/vault_refresh.py. Until 0.15.0 this block copied
  # the hooks over unconditionally, replaced any tool older ON DISK than the template, and
  # set core.hooksPath whatever it held -- three ways to lose an owner's edit.
  if [ -f "$SRC/scripts/vault_refresh.py" ]; then
    echo "Refreshing the vault's git hooks and tools..."
    with_heartbeat "refreshing the vault's tools" python3 "$(vault_refresh_script)" \
        refresh --vault "$VAULT_PATH" 2>&1 \
      || echo "⚠ the vault's git hooks and tools could not be refreshed — run install.sh again to retry"
  fi
fi

if [ -z "$ONLY" ] && { [ -z "$VAULT_PATH" ] || [ ! -d "$VAULT_PATH" ]; }; then
  # No vault. The four enforcement hooks are wired AGAINST a vault, so an install that
  # stops here leaves them inert — which is the state that produced a false "broken
  # install" report on 2026-09-12. Three ways out, and which one applies depends on
  # who is running this.
  if [ "$NO_VAULT" = yes ]; then
    echo ""
    echo "Installed without a vault (--no-vault). The Core-rule enforcement hooks are"
    echo "present but NOT wired; they are wired against a vault. Run /gt:gt-init, or"
    echo "  $U_INSTALL --vault <path>"
    echo "when you have one."
  elif [ -t 0 ] && [ -t 1 ]; then
    # A person is watching. Ask; never invent a directory unasked.
    question_break
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
      vault_location_check "$CHOSEN"
      setup_vault "$VL_CHOSEN" || true
    else
      echo "Skipped. Run /gt:gt-init in Claude Code when you are ready; that wires them."
    fi
  else
    # NOT a terminal: an agent, a pipe or CI (0.21.4, owner: "if they do a headless setup it
    # installs into <user>/GoldenThread by default"). Until 0.21.4 this stopped with exit 4 and
    # asked for a decision; now the vault is set up at the recommended <home>/GoldenThread, and
    # the path -- with any location warning -- goes to stderr, so the caller can tell the person.
    # --no-vault still means no vault. Exit 4 is left only for a default that could not be set up.
    echo "No vault given and no terminal to ask: setting one up at the default, $(upath "$DEFAULT_VAULT")" >&2
    echo "  (another place: $U_INSTALL --vault <path>; none at all: --no-vault)" >&2
    vault_location_check "$DEFAULT_VAULT"
    if ! setup_vault "$VL_CHOSEN"; then
      echo ""
      echo "════════════════════════════════════════════════════════════════════════"
      echo "INSTALL INCOMPLETE — the default vault could not be set up"
      echo "════════════════════════════════════════════════════════════════════════"
      echo "The plugin is installed, but the Core-rule enforcement hooks are NOT wired:"
      echo "they are wired against a vault, and $(upath "$VL_CHOSEN") could not be used (above)."
      echo ""
      echo "  Already have one?   $U_INSTALL --vault <path-to-existing-vault>"
      echo "  Another new one?    $U_INSTALL --vault <path>"
      echo "  Deliberately none?  $U_INSTALL --no-vault"
      echo ""
      echo "ASK THE USER which they want before re-running. Exit 4 means exactly this."
      echo "════════════════════════════════════════════════════════════════════════"
      exit 4
    fi
  fi
fi


# Stale socket directories (0.20.1). gt_ipc falls back to /tmp/gt-<uid>-<hash>/ when a home's
# socket path is too long for AF_UNIX, and nothing removed one once its server was gone: the
# usability Mac had 36. Removed here only when it is this user's, private (0700), untouched for
# an hour, and holds nothing but sockets none of which accepts a connection. Quiet otherwise.
if [ "$(uname -s 2>/dev/null)" = Darwin ] || [ "$(uname -s 2>/dev/null)" = Linux ]; then
  python3 - <<'PYEOF' || true
import errno, glob, os, shutil, socket, stat, time
gone = 0
for d in glob.glob("/tmp/gt-%d-*" % os.getuid()):
    try:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 \
                or time.time() - st.st_mtime < 3600:
            continue
        names = os.listdir(d)
        if any(not stat.S_ISSOCK(os.lstat(os.path.join(d, n)).st_mode) for n in names):
            continue
        live = False
        for n in names:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            try:
                s.connect(os.path.join(d, n))
                live = True
            except socket.timeout:
                live = True          # a busy server (full backlog) is still a server
            except OSError as exc:
                # Only "nobody is listening" is dead; EAGAIN on a full backlog is not.
                if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK, errno.EPROTOTYPE):
                    live = True
            finally:
                s.close()
        if not live:
            shutil.rmtree(d)
            gone += 1
    except OSError:
        continue
if gone:
    print("Removed %d stale gt socket dir(s) under /tmp (no server listening)" % gone)
PYEOF
fi

# ── Summary ────────────────────────────────────────────────────────────────
#
# LAST, deliberately. This block used to sit before steps 6 and 7, so hook
# registration and vault-tool seeding printed BELOW "Restart Claude Code to load
# the plugins." -- past what reads as the end of the output. The line that tells
# you whether the hooks were wired is the line most worth seeing, and it was the
# one printed after the goodbye.
echo ""
if [ -n "$ONLY" ]; then
  if [ "$ONLY_FROM" = "-" ]; then
    ONLY_HEADER="Module $ONLY_MODULE installed: $ONLY_PLUGIN $ONLY_TO, beside gt $VERSION (--only; nothing else changed)."
  elif [ "$ONLY_FROM" != "$ONLY_TO" ]; then
    ONLY_HEADER="Module $ONLY_MODULE updated: $ONLY_PLUGIN $ONLY_FROM → $ONLY_TO, beside gt $VERSION (--only; nothing else changed)."
  else
    ONLY_HEADER="Module $ONLY_MODULE reinstalled: $ONLY_PLUGIN $ONLY_TO, beside gt $VERSION (--only; nothing else changed)."
  fi
  echo "$ONLY_HEADER"
else
  echo "Golden Thread $VERSION installed."
fi
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
  if [ -n "$ONLY" ] && [ "${PLUGIN_NAMES[$i]}" != "$ONLY_PLUGIN" ]; then i=$((i + 1)); continue; fi
  if [ -n "$ONLY" ]; then
    ONLY_SKILLS=$(list_skills "$(plugin_cache "$i")/skills" "/${PLUGIN_NAMES[$i]}:" 2>/dev/null || true)
  fi
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
  echo "  (change with $U_INSTALL --with NAME / --without NAME; see --list-modules)"
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
if [ "$PERSONA_SET" = yes ]; then
  if [ -f "$HOME/.claude/vault-config.json" ]; then
    python3 "$SRC/scripts/gt_settings.py" set persona "${PERSONA:-none}" | sed 's/^/  /' || true
  else
    echo "  (persona not recorded: there is no vault yet; after /gt:gt-init run: gt_settings.py set persona ${PERSONA:-none})"
  fi
fi
# The install questions that are settings (item #33): sandbox_mode, paranoid and expiry_guard,
# applied when the person chose them (or accepted defaults on a new machine). A kept answer is
# left alone. A refused one is said out loud and never hidden: the install does not fail on it,
# because gt is installed and working, but the summary names it.
PLAN_NOT_APPLIED=""
apply_plan_setting() {  # $1 = key
  local key="$1" val src cur
  eval "val=\${PLAN_${key}:-}; src=\${PLAN_${key}_SRC:-}"
  [ -n "$src" ] || return 0
  [ "$src" != kept ] || return 0
  cur=$(python3 -B "$SRC/scripts/gt_settings.py" get "$key" 2>/dev/null || true)
  # token_profile is compared with the STORED choice, not `get`: `get` answers the registry default
  # (standard) when nothing is stored, so a fresh machine looked as if the default were in force
  # and the seven settings it writes were never written (fix B1, 0.21.4). The other keys hold one
  # value each, so their effective value is the one that counts.
  if [ "$key" = token_profile ]; then
    cur=$(python3 -B -c 'import json,os,sys
try:
    d = json.load(open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8"))
except Exception:
    d = {}
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print(v if isinstance(v, str) else "")' "$key" 2>/dev/null || true)
  fi
  if [ "$cur" = "$val" ]; then
    echo "  $key: $val ($(plan_words "$src"))"
    return 0
  fi
  if [ ! -f "$HOME/.claude/vault-config.json" ]; then
    echo "  ⚠ $key: $val ($(plan_words "$src")) NOT APPLIED: there is no vault yet; after /gt:gt-init run: gt_settings.py set $key $val"
    PLAN_NOT_APPLIED="${PLAN_NOT_APPLIED}  $key=$val (no vault yet)
"
    return 0
  fi
  local rc=0 out
  out=$(python3 -B "$SRC/scripts/gt_settings.py" set "$key" "$val" 2>&1) || rc=$?
  if [ -n "$out" ]; then printf '%s\n' "$out" | sed 's/^/    /'; fi
  if [ "$rc" -eq 0 ]; then
    echo "  $key: $val ($(plan_words "$src"))"
  else
    echo "  ⚠ $key: $val NOT APPLIED (the reason is above); set it yourself later: gt_settings.py set $key $val"
    PLAN_NOT_APPLIED="${PLAN_NOT_APPLIED}  $key=$val (refused)
"
  fi
}
apply_plan_setting sandbox_mode
apply_plan_setting paranoid
# --starter records itself (phase 116), so `gt_settings.py show` and the doctor can say which install this is
if [ "$STARTER" = yes ] && [ -z "$ONLY" ] && [ -f "$HOME/.claude/vault-config.json" ]; then
  python3 -B "$SRC/scripts/gt_settings.py" set profile starter >/dev/null 2>&1 \
    || echo "  ⚠ profile: starter NOT RECORDED; run: gt_settings.py set profile starter"
  python3 -B "$SRC/scripts/gt_settings.py" set rules_set lite >/dev/null 2>&1 \
    || echo "  ⚠ rules_set: lite NOT APPLIED; run: gt_settings.py set rules_set lite"
fi
# the token profile first (it sets seven settings), then the two it shares an answer with: an answer
# the person gave for the expiry guard or the reply length is applied after it and so wins
# A token profile nobody chose (defaulted or accepted) must not undo a setting the plan KEPT -- a
# recorded reply_length or expiry_guard, or the model profile a scripted upgrade leaves as it is:
# it writes the others and leaves those (fix B2, 0.21.4). A profile the person chose sets all seven.
GT_TOKEN_PROFILE_KEEP=""
case "${PLAN_token_profile_SRC:-}" in
  default|accepted)
    for _k in model_profile expiry_guard reply_length; do
      eval "_ks=\${PLAN_${_k}_SRC:-}"
      if [ "$_ks" = kept ]; then GT_TOKEN_PROFILE_KEEP="${GT_TOKEN_PROFILE_KEEP:+$GT_TOKEN_PROFILE_KEEP,}$_k"; fi
    done ;;
esac
export GT_TOKEN_PROFILE_KEEP
apply_plan_setting token_profile
unset GT_TOKEN_PROFILE_KEEP
apply_plan_setting expiry_guard
apply_plan_setting reply_length
apply_plan_setting parallel_max
apply_plan_setting persona_aligned
apply_plan_setting telemetry
if [ -f "$HOME/.claude/vault-config.json" ]; then
  # Said in words (0.20.1): the raw line named the internal key (parallel_profile, cpu_max).
  python3 "$SRC/scripts/gt_settings.py" detect-machine --write 2>/dev/null \
    | sed -E 's/^parallel_profile (updated|unchanged): ([0-9]+) core\(s\), ([0-9.]+) GB -> cpu_max ([0-9]+), io_max ([0-9]+)$/This machine: \2 cores, \3 GB — gt runs up to \4 jobs at once (\5 for disk-bound work)/' \
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

  echo "Checking the vault for upgrades this release needs..."
  # Matches below read here-strings, never `printf "$out" | grep -q`: under pipefail, grep -q
  # exiting on its first match can SIGPIPE the printf, the pipeline then "fails", and a present
  # line reads as absent -- the install intermittently reported "unrecognised output" (0.18.1).
  out=$(python3 "$up" status --vault "$vault" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "⚠ Vault upgrades: the check could not run (exit $rc) — run /gt:gt-upgrade to look"
    echo ""; return 0
  fi
  attn=$(printf '%s\n' "$out" | upgrade_attention_list)
  if grep -q 'nothing pending' <<<"$out"; then
    echo "Vault upgrades: none pending"
    [ -n "$attn" ] && printf '%s\n' "$attn"
    echo ""; return 0
  fi
  if ! grep -q 'pending step(s):' <<<"$out"; then
    echo "⚠ Vault upgrades: the check could not run (unrecognised output) — run /gt:gt-upgrade to look"
    echo ""; return 0
  fi

  if ! git -C "$vault" rev-parse --git-dir >/dev/null 2>&1; then
    echo "⚠ Vault upgrades pending, not applied — the vault is not a git repository, so an"
    echo "  applied upgrade could not be reviewed or undone with git."
    printf '%s\n' "$out" | upgrade_pending_list
    [ -n "$attn" ] && printf '%s\n' "$attn"
    echo "  To apply (it backs the vault up first): /gt:gt-upgrade"
    echo "    or: $U_PY \"$(upath "$up")\" --vault \"$(upath "$vault")\" run"
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
      echo "    or: $U_PY \"$(upath "$up")\" --vault \"$(upath "$vault")\" run"
      echo ""; return 0
    fi
  fi

  echo "Vault upgrades — applying to $vault:"
  if [ "$ours" = yes ]; then
    echo "  (the vault was clean before this install; its uncommitted files are this install's own refreshes:)"
    printf '%s\n' "$porcelain" | sed 's/^/    /'
  fi
  # STREAMED, not captured (2026-09-29): the longest step of an upgrade from an old release
  # used to print nothing until it was all done. gt_upgrade prints "[k/N]" per step with its
  # time and a heartbeat; the lines reach the screen as they come, a copy feeds the summary.
  # The attention section is printed once, from the follow-up status below.
  local run_log
  run_log=$(mktemp)
  python3 -u "$up" --vault "$vault" run ${allow[@]+"${allow[@]}"} > "$run_log" 2>&1 &
  local up_pid=$!
  local shown=0 total
  while kill -0 "$up_pid" 2>/dev/null; do
    sleep 1
    total=$(wc -l < "$run_log" | tr -d ' ')
    if [ "$total" -gt "$shown" ]; then
      sed -n "$((shown + 1)),${total}p" "$run_log" | sed '/^needs a person (/,$d' \
        | sed '/^[[:space:]]*$/d' | sed 's/^/  /' || true
      shown=$total
    fi
  done
  wait "$up_pid" || run_rc=$?
  total=$(wc -l < "$run_log" | tr -d ' ')
  if [ "$total" -gt "$shown" ]; then
    sed -n "$((shown + 1)),${total}p" "$run_log" | sed '/^needs a person (/,$d' \
      | sed '/^[[:space:]]*$/d' | sed 's/^/  /' || true
  fi
  run_out=$(cat "$run_log"); rm -f "$run_log"
  backup=$(printf '%s\n' "$run_out" | sed -n 's/^backup: //p' | head -1)
  if [ "$run_rc" -eq 1 ] && grep -q 'need a person:' <<<"$run_out"; then
    echo "  Left for you: the step(s) above that need a person. Run /gt:gt-upgrade to finish"
  elif [ "$run_rc" -eq 2 ] && grep -q 'REFUSED' <<<"$run_out"; then
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
  elif grep -q 'nothing pending' <<<"$out"; then
    echo "Vault upgrades: none pending"
  elif grep -q 'pending step(s):' <<<"$out"; then
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
if [ -z "$ONLY" ]; then apply_vault_upgrades || true; fi

# The pre-write backup stays only when this run changed a file it holds (0.15.0).
if [ -n "$VAULT_PREWRITE_BACKUP" ] && [ -f "$VAULT_PREWRITE_BACKUP" ]; then
  python3 "$SRC/scripts/vault_refresh.py" prune --vault "$VAULT_PREWRITE_PATH" \
    --backup "$VAULT_PREWRITE_BACKUP" 2>/dev/null || true
fi

# ── The structural matcher: optional in the code, default-on in this path ──────
#
# gt NEVER REQUIRES ast-grep. A rule that needs it is reported SKIPPED, counted in the
# affirmative line, and placed in SARIF notifications -- the scanner's contract is that a check
# which did not run says so. That must stay true: gt is stdlib-default, and a rule format whose
# meaning depended on an optional native dependency would be undefined without it.
#
# But "optional" and "off by default" are different decisions. Without the binary, structural
# rules never run and the person who installed gt has no reason to know what they are missing.
# So this OFFERS it, with yes as the default, and names both routes.
#
# IT NEVER INSTALLS WITHOUT A PERSON PRESENT: a non-interactive run skips and says so.
#
# EVERYTHING HERE IS `set -e`-SAFE, and that is not decoration. install.sh runs under
# `set -euo pipefail`, and the first version of this block used bare `[ -n "$X" ] && echo ...`
# statements plus `astgrep_present; AG_STATE=$?`. Both abort the script when the left side is
# false -- so the installer FAILED on any machine without brew or npm, after having installed
# everything, without printing its final line. Caught by the install suite, which is the only
# reason it is not in a release. No bare `a && b` statements below; every conditional is an
# `if`, and every function result is captured with `|| rc=$?`.
AG_MIN="0.45.3"

ag_version_of() { "$1" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true; }

ag_ge_min() {   # is $1 >= AG_MIN ?
  if [ -z "${1:-}" ]; then return 1; fi
  if [ "$(printf '%s\n%s\n' "$AG_MIN" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$AG_MIN" ]; then
    return 0
  fi
  return 1
}

AG_FOUND=""; AG_FOUND_VER=""
# 0 = usable, 1 = absent, 2 = present but too old.
astgrep_state() {
  for b in ast-grep sg; do
    if ! command -v "$b" >/dev/null 2>&1; then continue; fi
    # `sg` is util-linux's setgid tool on many systems, so the NAME proves nothing.
    if ! "$b" --version 2>&1 | grep -qi "ast-grep"; then continue; fi
    AG_FOUND="$(command -v "$b")"
    AG_FOUND_VER="$(ag_version_of "$b")"
    if ag_ge_min "$AG_FOUND_VER"; then return 0; fi
    return 2
  done
  return 1
}

AG_STATE=0
astgrep_state || AG_STATE=$?

if [ -n "$ONLY" ]; then
  :   # --only (0.21.4): ast-grep is a machine tool, not part of a module; nothing to offer here
elif [ "$AG_STATE" -eq 0 ]; then
  echo ""
  echo "ast-grep $AG_FOUND_VER found ($AG_FOUND) — structural code rules are available."
elif [ "$AG_STATE" -eq 2 ]; then
  echo ""
  echo "ast-grep $AG_FOUND_VER is installed at $AG_FOUND, but gt is tested against $AG_MIN."
  echo "Older builds support fewer languages, so a rule targeting one they lack never matches"
  echo "and nothing says the language was missing rather than the code clean. gt will report"
  echo "structural rules as SKIPPED until it is upgraded:"
  if command -v brew >/dev/null 2>&1; then echo "  brew upgrade ast-grep"; fi
  if command -v npm  >/dev/null 2>&1; then echo "  npm install -g @ast-grep/cli"; fi
else
  AG_BREW=""; AG_NPM=""
  if command -v brew >/dev/null 2>&1; then AG_BREW="brew install ast-grep"; fi
  if command -v npm  >/dev/null 2>&1; then AG_NPM="npm install -g @ast-grep/cli"; fi
  if [ -z "${PLAN_ast_grep_SRC:-}" ] && [ -t 0 ] && [ -t 1 ] && [ -n "$AG_BREW$AG_NPM" ]; then question_break; else echo ""; fi
  echo "ast-grep is not installed. It is OPTIONAL: gt works without it, and any rule that"
  echo "needs it is reported as SKIPPED rather than quietly passing. With it, gt can check"
  echo "code structurally in 29 languages (bash, markdown, go, rust and more) instead of by"
  echo "line matching. It is MIT-licensed and gt only ever runs it on your own files."
  if [ -z "$AG_BREW" ] && [ -z "$AG_NPM" ]; then
    echo "Neither brew nor npm is on PATH here, so gt cannot offer to install it."
    echo "See https://ast-grep.github.io/ for other routes."
  elif [ "${PLAN_ast_grep_SRC:-}" = choice ] && [ "$PLAN_ast_grep" = no ]; then
    echo "ast-grep: not installed (no) (your choice). Structural rules will be reported as SKIPPED until it is:"
    echo "  ${AG_BREW:-$AG_NPM}"
  elif [ "${PLAN_ast_grep_SRC:-}" = choice ]; then
    AG_CMD="${AG_BREW:-$AG_NPM}"
    echo "ast-grep: yes (your choice)"
    echo "Running: $AG_CMD"
    AG_RC=0
    $AG_CMD || AG_RC=$?
    AG_AFTER=0
    astgrep_state || AG_AFTER=$?
    if [ "$AG_RC" -eq 0 ] && [ "$AG_AFTER" -eq 0 ]; then
      echo "ast-grep $AG_FOUND_VER installed — structural rules are now available."
    else
      echo "That did not complete. gt is installed and fine; structural rules will be"
      echo "SKIPPED until ast-grep is present. Try it yourself later: $AG_CMD"
    fi
  elif [ -z "${PLAN_ast_grep_SRC:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    echo ""
    if [ -n "$AG_BREW" ]; then echo "  1) $AG_BREW"; fi
    if [ -n "$AG_NPM" ];  then echo "  2) $AG_NPM"; fi
    AG_DEFAULT="${AG_BREW:-$AG_NPM}"
    printf 'Install it now with "%s"? [Y/n or 1/2]: ' "$AG_DEFAULT"
    AG_ANSWER=""
    read -r AG_ANSWER </dev/tty || AG_ANSWER=""
    AG_CMD=""
    case "$AG_ANSWER" in
      ""|y|Y|yes|YES) AG_CMD="$AG_DEFAULT" ;;
      1)              AG_CMD="$AG_BREW" ;;
      2)              AG_CMD="$AG_NPM" ;;
      *)              AG_CMD="" ;;
    esac
    if [ -n "$AG_CMD" ]; then
      echo "Running: $AG_CMD"
      AG_RC=0
      $AG_CMD || AG_RC=$?
      AG_AFTER=0
      astgrep_state || AG_AFTER=$?
      if [ "$AG_RC" -eq 0 ] && [ "$AG_AFTER" -eq 0 ]; then
        echo "ast-grep $AG_FOUND_VER installed — structural rules are now available."
      else
        # NEVER fails the install: gt is installed and working by this point, and a
        # third-party package manager is not a reason to report gt as broken.
        echo "That did not complete. gt is installed and fine; structural rules will be"
        echo "SKIPPED until ast-grep is present. Try it yourself later: $AG_CMD"
      fi
    else
      echo "Skipped. Structural rules will be reported as SKIPPED until it is installed:"
      echo "  ${AG_BREW:-$AG_NPM}"
    fi
  else
    echo "Not a terminal, so nothing was installed. To enable structural rules later:"
    if [ -n "$AG_BREW" ]; then echo "  $AG_BREW"; fi
    if [ -n "$AG_NPM" ];  then echo "  $AG_NPM"; fi
  fi
fi

# ── Post-install validation: the release gate, run by default (0.17.11) ─────────
#
# Owner requirement: the gate that proves THIS machine is correct for THIS release runs on
# every install, not only when someone remembers it. It is the INSTALLED doctor that runs --
# what the machine will actually execute -- pinned to the release just installed (--release)
# so a rollback is judged against what it installed, not the newest dir in the tree.
#
# --stage install: what cannot be true until /gt:gt-upgrade has run (the vault's rule-1
# copy, its migrations) is PENDING with the next step, never PASS and never FAIL. A real FAIL
# stops the install with exit 9 -- distinct from 4 (no vault), 6 (manifest), 7 (machine
# migration), 8 (checksum). Nothing is rolled back: the files are the release's.
#
# Skipped, and said so, when no vault is configured (the gate checks the vault) or when the
# installed doctor predates the profile (a rollback to a release without it). The SessionStart
# check (gt_version_check.py) runs the same gate once per installed version.
post_install_gate() {
  local doc="$HOME/.claude/golden-thread/hooks/gt_doctor.py" vault out rc=0
  vault=$(python3 -c "import json,os;p=os.path.expanduser('~/.claude/vault-config.json');print(json.load(open(p)).get('vault_path','')) if os.path.exists(p) else print('')" 2>/dev/null) || vault=""
  echo ""
  if [ -z "$vault" ] || [ ! -d "$vault" ]; then
    echo "Post-install validation: skipped — no vault configured (it validates against one)."
    return 0
  fi
  if [ ! -f "$doc" ] || ! grep -q "def post_install_main" "$doc" 2>/dev/null; then
    echo "Post-install validation: skipped — the installed gt_doctor.py has no post-install profile."
    return 0
  fi
  echo "Post-install validation (gt_doctor.py post-install --stage install):"
  out=$(python3 "$doc" post-install --vault "$vault" --release "$SRC" \
        --plugin-root "$SCRIPT_DIR" --stage install 2>&1) || rc=$?
  printf '%s\n' "$out" | sed 's/^/  /' || true
  # Exit 9 only on rc 1: the gate RAN and a row proved the install broken. Vault-state rows are
  # PENDING at this stage (a failed vault upgrade never fails the install), and a gate that
  # could not run at all is said out loud here, not passed off as a broken install.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
    echo "  ⚠ the post-install validation could not run (exit $rc) — run it after restarting:"
    echo "    $U_PY \"$(upath "$doc")\" post-install --vault \"$(upath "$vault")\""
    return 0
  fi
  if [ "$rc" -eq 1 ]; then
    echo ""
    echo "════════════════════════════════════════════════════════════════════════"
    echo "INSTALL FAILED VALIDATION — the FAIL row(s) above are real; the install is rolled back."
    echo "Fix each (its fix: line), then re-run install.sh. Exit 9 means exactly this."
    echo "════════════════════════════════════════════════════════════════════════"
    return 9
  fi
  # Only when something IS pending (0.20.1: the line was printed after a clean gate too).
  if grep -q '^PENDING' <<<"$out"; then
    echo "  After /gt:gt-upgrade and a Claude Code restart, run the full gate (a PENDING row"
    echo "  then has to PASS):"
  else
    echo "  To re-run this check later:"
  fi
  echo "    $U_PY \"$(upath "$doc")\" post-install --vault \"$(upath "$vault")\""
  return 0
}
# Installed-code integrity (0.21.0): compile, seal and lock what was just installed, with the token
# the presence check at the top gave -- or, on a first install, with no check at all (nothing is
# protected yet that a first seal could bypass). `lock` refuses code that differs from the release
# manifest installed beside it. With the group off it converges to "nothing locked" and says nothing
# unless it had something to undo. It runs BEFORE the post-install gate so the gate's integrity row
# validates the locked machine; if the gate fails the install is rolled back and re-locked above.
if [ -f "$HOME/.claude/golden-thread/hooks/gt_integrity.py" ]; then
  if GT_INTEGRITY_TOKEN="$GT_ITOKEN" python3 -B "$HOME/.claude/golden-thread/hooks/gt_integrity.py" lock --from-install; then
    GT_ILOCKED=yes
  else
    echo "✗ gt's installed code could not be sealed and locked (above); the install is rolled back."
    exit 11
  fi
elif [ -f "$HOME/.claude/golden-thread/integrity.json" ]; then
  # an OLDER gt (a rollback) has no integrity tools: nothing there reads the record, so drop it
  rm -f "$HOME/.claude/golden-thread/integrity.json" "$HOME/.claude/golden-thread/integrity-stamps.json"
  echo "Removed the installed-code integrity record (this release has no integrity tools)."
fi

# gt unlock's authority (0.20.1, M12): a running gt_unlockd.py keeps the code it started with,
# so the same pid used to survive a reinstall, an upgrade and a rollback. Stop it when the code
# it runs has changed -- an orderly stop that needs no factor, ONLY because the code changed,
# and that never grants: every grant is dropped (the command says so) -- and start it again
# from the new code when unlock is on. Silent when no authority is running.
if [ -f "$HOME/.claude/golden-thread/hooks/gt_unlock.py" ]; then
  python3 -B "$HOME/.claude/golden-thread/hooks/gt_unlock.py" daemon restart-if-stale 2>/dev/null \
    | sed 's/^/  /' || true
fi

PI_RC=0
if [ -n "$ONLY" ]; then
  # --only (0.21.4): the release gate validates gt against a vault, which --only does not touch.
  # The proof here is of the one module installed, and a FAIL is rolled back like any failure.
  echo ""
  echo "Self-proof of module $ONLY_MODULE ($ONLY_PLUGIN $ONLY_TO):"
  _oout=$(only_verify) || PI_RC=$?
  printf '%s\n' "$_oout" | sed 's/^/  /'
  if [ "$PI_RC" -ne 0 ]; then
    echo ""
    echo "✗ --only $ONLY FAILED its self-proof (the FAIL row above); the install is rolled back. Exit 9."
    PI_RC=9
  fi
else
  post_install_gate || PI_RC=$?
fi
if [ "$PI_RC" -ne 0 ]; then
  exit "$PI_RC"
fi
if [ "${ONLY_QUIET:-}" = yes ]; then           # --only, quiet: the module's own lines (0.21.4)
  {
    echo "$ONLY_HEADER"
    if [ -n "${ONLY_SKILLS:-}" ]; then echo "$ONLY_PLUGIN skills:"; printf '%s\n' "$ONLY_SKILLS"; fi
    echo "Self-proof of module $ONLY_MODULE ($ONLY_PLUGIN $ONLY_TO):"
    printf '%s\n' "$_oout" | sed 's/^/  /'
    echo "Restart Claude Code to load it."
  } >&3
fi

# gt unlock (0.20.1) ships OFF. Say so once, here -- what it is and how to turn it on -- and
# never again per session (no nagging). When it is on, the same command reports its level.
if [ -f "$HOME/.claude/golden-thread/hooks/gt_unlock.py" ]; then
  echo ""
  python3 -B "$HOME/.claude/golden-thread/hooks/gt_unlock.py" status 2>/dev/null \
    | sed 's/^/  /' || true
fi

# What the person chose and what was chosen for them (item #33), once, at the end.
if [ -n "${PLAN_SUMMARY:-}" ]; then
  echo ""
  printf '%s\n' "$PLAN_SUMMARY"
fi
if [ -n "${PLAN_NOT_APPLIED:-}" ]; then
  echo ""
  echo "Chosen but NOT applied (the install itself is complete):"
  printf '%s' "$PLAN_NOT_APPLIED"
fi

# "Download Obsidian's plugins?" (phase 89b): only a yes runs the pinned installer, one plugin at a time; each
# is all-or-nothing against its sha256 pin, and a failure here never fails the install. It writes into the
# vault named by ~/.claude/vault-config.json; with no vault it says so and fetches nothing.
if [ -z "$ONLY" ] && [ "${PLAN_obsidian_plugins:-}" = yes ]; then
  echo ""
  for _pl in terminal realclaudian; do
    python3 -B "$SRC/scripts/gt_prereqs.py" plugin "$_pl" --yes \
      || echo "  (the $_pl plugin was not installed: run python3 $SRC/scripts/gt_prereqs.py check)"
  done
fi

# "Open the guide?" (item #36): a yes opens the Getting started page of the offline help for the chosen
# persona (the first, when there are several); anything else says how to open it later.
if [ -z "$ONLY" ] && [ "${PLAN_open_guide:-}" = yes ]; then
  _gp="${PLAN_persona:-}"
  case "$_gp" in none) _gp="" ;; esac
  _gp="${_gp%%,*}"
  echo ""
  if [ -n "$_gp" ]; then
    python3 -B "$SRC/scripts/gt_help.py" start --persona "$_gp" || echo "  (the guide could not be opened: run /gt:gt-help start)"
  else
    python3 -B "$SRC/scripts/gt_help.py" start || echo "  (the guide could not be opened: run /gt:gt-help start)"
  fi
else
  echo ""
  echo "Getting started: say \"show me around Golden Thread\" to Claude, or run /gt:gt-help start"
fi

# Persona hand-off (phase 95e): after an install with a persona that has one (project manager, product manager,
# executive, leadership), name the three core commands, the tour and the offline walk-through; then, only if
# something an Obsidian-first setup needs is missing, one line pointing at `gt_prereqs.py plan`. Nothing is
# installed or asked here; a persona with no hand-off, and --only, print nothing.
if [ -z "$ONLY" ]; then
  _hp="${PLAN_persona:-}"
  case "$_hp" in none) _hp="" ;; esac
  if [ -n "$_hp" ]; then
    _hout=$(python3 -B "$SRC/scripts/gt_persona.py" handoff --personas "$_hp" 2>/dev/null) || _hout=""
    if [ -n "$_hout" ]; then
      echo ""
      printf '%s\n' "$_hout"
      _pout=$(python3 -B "$SRC/scripts/gt_prereqs.py" check --brief 2>/dev/null) || _pout=""
      if [ -n "$_pout" ]; then printf '%s\n' "$_pout"; fi
    fi
  fi
fi

if [ "$STARTER" = yes ] && [ -z "$ONLY" ]; then
  echo "Starter install: core only, nothing scheduled, no sandbox or credential helper. To remove it: $U_INSTALL --uninstall"
fi
echo "Restart Claude Code to load the plugins."
