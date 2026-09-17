#!/usr/bin/env python3
"""plugins.py — the ONE rule for "which plugins does this repo ship, at which version".

    python3 dev/plugins.py list [PLUGIN_ROOT]            # "<dir> <version> <name>" per plugin
    python3 dev/plugins.py newest DIR                     # newest version under DIR, or exit 1
    python3 dev/plugins.py manifest-check VERSION_DIR     # 0 ok, 1 stale, 2 missing, 3 cannot check
    python3 dev/plugins.py manifest VERSION_DIR           # write MANIFEST.json (non-core plugins)
    python3 dev/plugins.py modules [PLUGIN_ROOT]          # "<name> <plugin> <version> <default>"
    python3 dev/plugins.py module-check VERSION_DIR [--gt V]  # 0 valid, 1 invalid, 2 not a module

The rule: every top-level PLUGIN_ROOT/<dir>/<N.N.N>/.claude-plugin/plugin.json is a
plugin release; the newest per <dir>, compared numerically per field, is the one that
ships. Output is sorted by directory name.

Until 0.13.0 release-check.sh, package.sh, sync-gt-src.sh, check_wiring_coverage.py and
feature_requests.py each carried their own copy of this rule and each named gt and
gt-wiki by hand, so a third plugin would have been silently skipped by whichever copy
nobody remembered. The shell scripts call `list`; the Python ones import `discover`.
One implementation means shell and Python cannot disagree about what ships.

`modules` and `module-check` (0.14.0) read module.json, the file that makes a plugin dir a
gt MODULE. The validator is duplicated, on purpose, in the release's
scripts/gt_components.py -- that file ships and cannot import dev/ -- and
tests/test_module_format.py runs both on the same fixtures and requires identical
verdicts. `--gt V` also requires the module's requires_gt to admit gt V (the gate's
modules step passes the gt being released).

`manifest-check` decides the gate's manifest step for one plugin. Every plugin ships a
MANIFEST.json in the `files` shape gt_components.build_manifest produces; a plugin
without one FAILS (exit 2), so a new module cannot ship without hash trust. A plugin
that carries its own scripts/gt_components.py (gt) is checked with it, hooks included;
any other plugin is checked with the core gt release's build_manifest, files only.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"\d+\.\d+\.\d+")
CORE = "golden-thread"          # the plugin whose gt_components.py hashes the others
MANIFEST = "MANIFEST.json"
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _key(name):
    return tuple(int(x) for x in name.split("."))


def newest(plugin_dir):
    """-> the newest installable version directory under plugin_dir, or None."""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return None
    cands = [d for d in plugin_dir.iterdir()
             if d.is_dir() and SEMVER.fullmatch(d.name)
             and (d / ".claude-plugin" / "plugin.json").is_file()]
    return max(cands, key=lambda d: _key(d.name)) if cands else None


def releases(plugin_dir, keep=2):
    """-> up to `keep` installable version directories under plugin_dir, newest first.

    gt-src carries the newest release AND the one before it (owner decision, 2026-09-14):
    a machine that installs from gt-src must be able to roll back with
    `install.sh <previous>`, and with only the newest copied it had nothing to roll back to.
    """
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return []
    cands = [d for d in plugin_dir.iterdir()
             if d.is_dir() and SEMVER.fullmatch(d.name)
             and (d / ".claude-plugin" / "plugin.json").is_file()]
    return sorted(cands, key=lambda d: _key(d.name), reverse=True)[:keep]


def plugin_name(version_dir):
    try:
        name = json.loads((Path(version_dir) / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return name or Path(version_dir).parent.name


def discover(root=DEFAULT_ROOT):
    """-> [(dir_name, version_dir Path, plugin name)] for every plugin, sorted by dir."""
    root = Path(root)
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        v = newest(d)
        if v is not None:
            out.append((d.name, v, plugin_name(v)))
    return out


# BEGIN module validator (keep identical in dev/plugins.py and scripts/gt_components.py)
MODULE_FILE = "module.json"
MODULE_SCHEMA = 1
MODULE_KEYS = ("schema", "name", "plugin", "version", "requires_gt", "summary", "default",
               "skills", "scripts", "templates", "hooks", "hookdir_scripts", "settings",
               "requires_modules", "replaces_core", "demo")
MODULE_REQUIRED = ("schema", "name", "plugin", "version", "requires_gt", "summary", "default")
MODULE_LISTS = ("skills", "scripts", "templates", "hooks", "hookdir_scripts", "settings",
                "requires_modules", "replaces_core")
MODULE_HOOK_KINDS = ("guard", "reporter")
# Core-rule enforcement is never module-owned (owner decision 2026-09-14): a module the
# user can switch off must not be able to take a Core rule's mechanism with it.
CORE_ENFORCEMENT_HOOKS = ("inject_core_rules.sh", "validate_response.sh",
                          "guard_session_claims.sh", "guard_test_before_commit.sh",
                          "guard_vault_writes.sh", "guard_protected_paths.sh")
_MODULE_NAME_RE = r"^[a-z][a-z0-9-]*$"
_EVENT_RE = r"^[A-Z][A-Za-z]*$"
_SETTING_KEY_RE = r"^[a-z][a-z0-9_]*$"
_CLAUSE_RE = r"^(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)$"


def parse_requires_gt(spec):
    """'>=0.14.0,<0.15.0' -> [(op, (0,14,0)), ...]. Comma = AND. ValueError if malformed."""
    import re
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("requires_gt must be a non-empty string")
    out = []
    for clause in spec.split(","):
        m = re.match(_CLAUSE_RE, clause.strip())
        if not m:
            raise ValueError("requires_gt clause %r is not <op><N.N.N> with op one of "
                             ">=,<=,==,>,<" % clause.strip())
        out.append((m.group(1), tuple(int(x) for x in m.group(2).split("."))))
    return out


def _vcmp(a, b):
    n = max(len(a), len(b))
    a, b = tuple(a) + (0,) * (n - len(a)), tuple(b) + (0,) * (n - len(b))
    return (a > b) - (a < b)


def requires_gt_admits(spec, version):
    """Does gt `version` ('0.14.0') satisfy `spec`? Numeric per field. ValueError if malformed."""
    v = tuple(int(x) for x in str(version).strip().split("."))
    ops = {">=": lambda c: c >= 0, "<=": lambda c: c <= 0, "==": lambda c: c == 0,
           ">": lambda c: c > 0, "<": lambda c: c < 0}
    return all(ops[op](_vcmp(v, want)) for op, want in parse_requires_gt(spec))


def _plain_entry(s):
    return (isinstance(s, str) and s not in ("", ".", "..") and "/" not in s
            and "\\" not in s)


def _rel_entry(s):
    return (isinstance(s, str) and s != "" and not s.startswith("/") and "\\" not in s
            and ".." not in s.split("/"))


def validate_module(version_dir):
    """-> (data, reasons). (None, None) when version_dir has no module.json (not a module).

    `reasons` is a list of human-readable strings, empty when the module is valid.
    """
    import os
    import re
    version_dir = os.path.abspath(str(version_dir))
    path = os.path.join(version_dir, MODULE_FILE)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return {}, ["module.json is not readable JSON (%s)" % e.__class__.__name__]
    if not isinstance(data, dict):
        return {}, ["module.json is not a JSON object"]
    reasons = []
    for k in sorted(set(data) - set(MODULE_KEYS)):
        reasons.append("unknown key %r" % k)
    for k in MODULE_REQUIRED:
        if k not in data:
            reasons.append("missing required key %r" % k)
    if "schema" in data and data["schema"] != MODULE_SCHEMA:
        reasons.append("schema is %r, this reader knows %d" % (data["schema"], MODULE_SCHEMA))
    name = data.get("name")
    if "name" in data and not (isinstance(name, str) and re.match(_MODULE_NAME_RE, name)):
        reasons.append("name %r does not match %s" % (name, _MODULE_NAME_RE))
    elif name == "gt":
        reasons.append("name 'gt' is reserved for the core")
    try:
        with open(os.path.join(version_dir, ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as fh:
            pj = json.load(fh)
        if not isinstance(pj, dict):
            raise ValueError("plugin.json is not an object")
    except (OSError, ValueError):
        pj = None
        reasons.append(".claude-plugin/plugin.json is missing or unreadable")
    dirname = os.path.basename(version_dir)
    if "plugin" in data:
        if not isinstance(data["plugin"], str) or not data["plugin"]:
            reasons.append("plugin must be a non-empty string")
        elif pj is not None and pj.get("name") != data["plugin"]:
            reasons.append("plugin %r differs from plugin.json name %r"
                           % (data["plugin"], pj.get("name")))
    if "version" in data:
        if data["version"] != dirname:
            reasons.append("version %r differs from the directory name %r"
                           % (data["version"], dirname))
        if pj is not None and pj.get("version") != data["version"]:
            reasons.append("version %r differs from plugin.json version %r"
                           % (data["version"], pj.get("version")))
    if "requires_gt" in data:
        try:
            parse_requires_gt(data["requires_gt"])
        except ValueError as e:
            reasons.append(str(e))
    if "summary" in data and not (isinstance(data["summary"], str) and data["summary"].strip()):
        reasons.append("summary must be a non-empty string")
    if "default" in data and data["default"] not in ("on", "off"):
        reasons.append("default %r is not 'on' or 'off'" % (data["default"],))
    for k in MODULE_LISTS:
        if k in data and not isinstance(data[k], list):
            reasons.append("%s must be a list" % k)

    def lst(k):
        v = data.get(k, [])
        return v if isinstance(v, list) else []

    for kind, sub, want in (("skills", "skills", "dir"), ("scripts", "scripts", "file"),
                            ("templates", "templates", "any"),
                            ("hookdir_scripts", "scripts", "file")):
        for e in lst(kind):
            if not _plain_entry(e):
                reasons.append("%s entry %r is not a plain name" % (kind, e))
                continue
            p = os.path.join(version_dir, sub, e)
            ok = {"dir": os.path.isdir, "file": os.path.isfile, "any": os.path.exists}[want](p)
            if not ok:
                reasons.append("%s entry %r does not exist under %s/" % (kind, e, sub))
    for i, h in enumerate(lst("hooks")):
        if not isinstance(h, dict):
            reasons.append("hooks[%d] is not an object" % i)
            continue
        for k in sorted(set(h) - {"event", "script", "args", "kind", "timeout"}):
            reasons.append("hooks[%d] unknown key %r" % (i, k))
        for k in ("event", "script", "kind"):
            if k not in h:
                reasons.append("hooks[%d] missing %r" % (i, k))
        if "event" in h and not (isinstance(h["event"], str) and re.match(_EVENT_RE, h["event"])):
            reasons.append("hooks[%d] event %r is not a hook event name" % (i, h["event"]))
        if "kind" in h and h["kind"] not in MODULE_HOOK_KINDS:
            reasons.append("hooks[%d] kind %r is not guard or reporter" % (i, h["kind"]))
        # `timeout` exists because SOME EVENTS HAVE A BUDGET AND CANCEL SILENTLY. SessionEnd
        # hooks share 1.5s across all of them; a hook that runs over is cancelled and its
        # output DISCARDED, with nothing reported to anyone. Declaring a timeout raises the
        # budget to match (up to 60s). Until 0.16.2 this key did not exist, so no gt hook
        # could ask for one -- the report card measured 0.61s of the 1.5s and would have
        # started truncating, invisibly, the moment a vault got slower.
        if "timeout" in h and not (isinstance(h["timeout"], int)
                                   and not isinstance(h["timeout"], bool)
                                   and 1 <= h["timeout"] <= 600):
            reasons.append("hooks[%d] timeout %r is not a whole number of seconds in 1..600"
                           % (i, h["timeout"]))
        if "args" in h and not (isinstance(h["args"], list)
                                and all(isinstance(a, str) for a in h["args"])):
            reasons.append("hooks[%d] args must be a list of strings" % i)
        s = h.get("script")
        if "script" in h:
            if not _plain_entry(s):
                reasons.append("hooks[%d] script %r is not a plain name" % (i, s))
            elif s in CORE_ENFORCEMENT_HOOKS:
                reasons.append("hooks[%d] script %r is a Core-rule enforcement hook; "
                               "those are never module-owned" % (i, s))
            elif not (os.path.isfile(os.path.join(version_dir, "hooks", s))
                      or os.path.isfile(os.path.join(version_dir, "scripts", s))):
                reasons.append("hooks[%d] script %r exists under neither hooks/ nor scripts/"
                               % (i, s))
    for i, s in enumerate(lst("settings")):
        if not isinstance(s, dict):
            reasons.append("settings[%d] is not an object" % i)
            continue
        for k in sorted(set(s) - {"key", "default", "values", "summary", "detail"}):
            reasons.append("settings[%d] unknown key %r" % (i, k))
        for k in ("key", "default", "values", "summary"):
            if k not in s:
                reasons.append("settings[%d] missing %r" % (i, k))
        if "key" in s and not (isinstance(s["key"], str)
                               and re.match(_SETTING_KEY_RE, s["key"])):
            reasons.append("settings[%d] key %r does not match %s"
                           % (i, s["key"], _SETTING_KEY_RE))
        vals = s.get("values")
        if "values" in s and not (isinstance(vals, list) and vals
                                  and all(isinstance(v, str) for v in vals)):
            reasons.append("settings[%d] values must be a non-empty list of strings" % i)
        elif "default" in s and "values" in s and s["default"] not in vals:
            reasons.append("settings[%d] default %r is not one of its values"
                           % (i, s["default"]))
        if "summary" in s and not isinstance(s["summary"], str):
            reasons.append("settings[%d] summary must be a string" % i)
        # Optional long explanation for `gt_settings.py explain` (0.15.0): a setting that
        # moved out of gt must not lose the text that says why it exists.
        if "detail" in s and not isinstance(s["detail"], str):
            reasons.append("settings[%d] detail must be a string" % i)
    for i, r in enumerate(lst("requires_modules")):
        if not isinstance(r, dict):
            reasons.append("requires_modules[%d] is not an object" % i)
            continue
        for k in sorted(set(r) - {"name", "soft"}):
            reasons.append("requires_modules[%d] unknown key %r" % (i, k))
        if not (isinstance(r.get("name"), str) and re.match(_MODULE_NAME_RE, r["name"])):
            reasons.append("requires_modules[%d] name %r is not a module name"
                           % (i, r.get("name")))
        if "soft" in r and not isinstance(r["soft"], bool):
            reasons.append("requires_modules[%d] soft must be true or false" % i)
    for e in lst("replaces_core"):
        if not _rel_entry(e):
            reasons.append("replaces_core entry %r is not a relative path" % (e,))
    # Optional: a tour act /gt-demo:gt-demo includes when this module is installed.
    if "demo" in data:
        dm = data["demo"]
        if not _rel_entry(dm):
            reasons.append("demo %r is not a relative path inside the module" % (dm,))
        elif not os.path.isfile(os.path.join(version_dir, dm)):
            reasons.append("demo %r does not exist in the module" % (dm,))
    return data, reasons
# END module validator


def _load_components(path):
    spec = importlib.util.spec_from_file_location("gt_components_for_manifest", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _builder(version_dir):
    """-> (module, own) — the gt_components that hashes this plugin, and whether it is its own."""
    version_dir = Path(version_dir)
    own = version_dir / "scripts" / "gt_components.py"
    if own.is_file():
        return _load_components(own), True
    core = newest(version_dir.parent.parent / CORE)
    if core is None or not (core / "scripts" / "gt_components.py").is_file():
        return None, False
    return _load_components(core / "scripts" / "gt_components.py"), False


def expected_manifest(version_dir):
    mod, own = _builder(version_dir)
    if mod is None:
        return None, own
    man = mod.build_manifest(str(version_dir))
    if not own:
        man.pop("hooks", None)      # the core's hook registrations are not this plugin's
    return man, own


def manifest_check(version_dir):
    version_dir = Path(version_dir)
    path = version_dir / MANIFEST
    fix = "python3 dev/plugins.py manifest %s" % version_dir
    if not path.is_file():
        print("%s has no %s — every plugin ships one; run: %s" % (version_dir, MANIFEST, fix))
        return 2
    want, own = expected_manifest(version_dir)
    if want is None:
        print("cannot check %s: no %s/<version>/scripts/gt_components.py beside it"
              % (version_dir, CORE))
        return 3
    try:
        have = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        print("%s is not JSON (%s); run: %s" % (path, e, fix))
        return 1
    same = have.get("files") == want["files"] and (not own or have.get("hooks") == want["hooks"])
    if not same:
        print("%s is stale; run: %s" % (path, fix if not own else
              "python3 %s/scripts/gt_components.py manifest %s" % (version_dir, version_dir)))
        return 1
    print("%s matches the tree (%d files)" % (path, len(want["files"])))
    return 0


def write_manifest(version_dir):
    version_dir = Path(version_dir)
    want, own = expected_manifest(version_dir)
    if want is None:
        print("cannot build a manifest for %s: no core gt_components.py" % version_dir)
        return 3
    with open(version_dir / MANIFEST, "w") as fh:
        json.dump(want, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("wrote %s (%d files)" % (version_dir / MANIFEST, len(want["files"])))
    return 0


def discover_modules(root=DEFAULT_ROOT):
    """-> [(dir_name, version_dir, data, reasons)] for every plugin whose newest release
    carries a module.json, sorted by module name."""
    out = []
    for d, v, _name in discover(root):
        data, reasons = validate_module(v)
        if data is None:
            continue
        name = data.get("name") if isinstance(data.get("name"), str) else d
        out.append((name, d, v, data, reasons))
    return [(d, v, data, reasons) for name, d, v, data, reasons in sorted(out, key=lambda x: x[0])]


def module_check(version_dir, gt=None):
    data, reasons = validate_module(version_dir)
    if data is None:
        print("%s is not a module (no %s)" % (version_dir, MODULE_FILE))
        return 2
    if not reasons and gt:
        try:
            if not requires_gt_admits(data.get("requires_gt"), gt):
                reasons = ["requires_gt %s does not admit gt %s" % (data.get("requires_gt"), gt)]
        except ValueError as e:
            reasons = [str(e)]
    for r in reasons:
        print("  %s" % r)
    print("%s: %s" % (version_dir, "invalid" if reasons else "valid module %s %s"
                      % (data.get("name"), data.get("version"))))
    return 1 if reasons else 0


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "modules" and len(rest) <= 1:
        root = Path(rest[0]) if rest else DEFAULT_ROOT
        if not root.is_dir():
            print("no such plugin root: %s" % root, file=sys.stderr)
            return 2
        for _d, _v, data, _reasons in discover_modules(root):
            print("%s %s %s %s" % (data.get("name"), data.get("plugin"), data.get("version"),
                                   data.get("default")))
        return 0
    if cmd == "module-check" and rest:
        gt = None
        if "--gt" in rest:
            i = rest.index("--gt")
            if i + 1 >= len(rest):
                print("--gt needs a version", file=sys.stderr)
                return 2
            gt = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
        if len(rest) == 1:
            return module_check(rest[0], gt)
    if cmd == "list" and len(rest) <= 1:
        found = discover(rest[0] if rest else DEFAULT_ROOT)
        for d, v, name in found:
            print("%s %s %s" % (d, v.name, name))
        return 0 if found else 1
    if cmd == "newest" and len(rest) == 1:
        v = newest(rest[0])
        if v is None:
            return 1
        print(v.name)
        return 0
    if cmd == "releases" and len(rest) in (1, 2):
        keep = int(rest[1]) if len(rest) == 2 else 2
        found = releases(rest[0], keep)
        for v in found:
            print(v.name)
        return 0 if found else 1
    if cmd == "manifest-check" and len(rest) == 1:
        return manifest_check(rest[0])
    if cmd == "manifest" and len(rest) == 1:
        return write_manifest(rest[0])
    print("usage: plugins.py list [ROOT] | newest DIR | releases DIR [KEEP] | manifest-check VERSION_DIR"
          " | manifest VERSION_DIR | modules [ROOT] | module-check VERSION_DIR [--gt V]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
