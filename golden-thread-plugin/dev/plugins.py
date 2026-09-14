#!/usr/bin/env python3
"""plugins.py — the ONE rule for "which plugins does this repo ship, at which version".

    python3 dev/plugins.py list [PLUGIN_ROOT]            # "<dir> <version> <name>" per plugin
    python3 dev/plugins.py newest DIR                     # newest version under DIR, or exit 1
    python3 dev/plugins.py manifest-check VERSION_DIR     # 0 ok, 1 stale, 2 missing, 3 cannot check
    python3 dev/plugins.py manifest VERSION_DIR           # write MANIFEST.json (non-core plugins)

The rule: every top-level PLUGIN_ROOT/<dir>/<N.N.N>/.claude-plugin/plugin.json is a
plugin release; the newest per <dir>, compared numerically per field, is the one that
ships. Output is sorted by directory name.

Until 0.13.0 release-check.sh, package.sh, sync-gt-src.sh, check_wiring_coverage.py and
feature_requests.py each carried their own copy of this rule and each named gt and
gt-wiki by hand, so a third plugin would have been silently skipped by whichever copy
nobody remembered. The shell scripts call `list`; the Python ones import `discover`.
One implementation means shell and Python cannot disagree about what ships.

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


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
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
          " | manifest VERSION_DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
