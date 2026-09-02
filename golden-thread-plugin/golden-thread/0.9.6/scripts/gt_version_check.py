#!/usr/bin/env python3
"""Golden Thread version check: is the INSTALLED release still the newest one
CHECKED IN? Notify only.

## The gap this closes

`gt_components.py` answers "do the installed files match version X?" -- where X is
whatever version directory it was handed on the command line. That is a real check
and it catches real drift, but it is blind along one axis: it never asks whether X
is still the newest version available.

On 2026-08-30, gt 0.6.0 was found installed while 0.9.4 had been checked in
alongside it since 2026-08-29 -- a fortnight of sessions running a release two
minor versions behind, including four hooks referenced from settings.json whose
files had never been copied to disk. A component check pointed at 0.6.0 would have
reported CLEAN throughout. Nothing was drifting; the wrong thing was simply the
thing being verified.

So this check compares VERSIONS, not file contents, and the two are complementary:

  gt_components  installed files  vs  one version's source   (contents)
  gt_version     installed version vs  newest version present (selection)

## Why notify-only, with no `auto`

`component_updates` offers `auto` because applying it copies individual files.
Installing a whole new VERSION is a different act: it rewrites settings.json hook
registrations, prunes superseded caches, and repopulates the marketplace. Doing
that mid-session leaves hooks on disk that do not match the ones already loaded in
memory -- the session keeps executing the old copies while every check reports the
new ones, which is precisely the "verified but not applied" state this system
exists to make impossible.

An upgrade is a decision with a restart attached. This check tells; the user runs
install.sh.

## Direction is inferred, never assumed

Following gt_components: if the installed version is NEWER than anything in the
source tree, that is reported as `ahead` and never treated as an error. It means a
release was installed from somewhere else, or the source tree has not synced --
and the right response is to capture it, not to "fix" it by downgrading.
"""
import json
import os
import re
import sys

INSTALLED = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# name in installed_plugins.json  ->  directory under the plugin source root
PLUGINS = (
    ("gt@golden-thread-plugin", "golden-thread", "gt"),
    ("gt-wiki@golden-thread-plugin", "golden-thread-wiki", "gt-wiki"),
)


def _registry_get(name, fallback):
    """Prefer the shared settings registry so defaults live in ONE place.

    Same reasoning as gt_components._registry_get: a second copy of a default is a
    default that drifts.
    """
    try:
        import gt_settings
        v = gt_settings.get(name)
        if v:
            return v
    except Exception:
        pass
    return fallback


def policy():
    return _registry_get("version_check", "report")


def parse(v):
    m = SEMVER.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def available(root):
    """Every installable version directory under `root`, newest last.

    Mirrors install.sh's latest_version(): an N.N.N directory that actually holds a
    manifest. A version dir without one is an archive or a work in progress, and
    install.sh would refuse it -- so reporting it as available would advertise an
    upgrade that cannot be installed.

    Sorted on the parsed integer tuple, never on the string: lexically "0.9.4"
    sorts above "0.10.0", which would start recommending a DOWNGRADE the first time
    a minor version reached double digits.
    """
    out = []
    try:
        names = os.listdir(root)
    except Exception:
        return out
    for name in names:
        p = parse(name)
        if not p:
            continue
        if not os.path.isfile(os.path.join(root, name, ".claude-plugin", "plugin.json")):
            continue
        out.append((p, name))
    return [n for _, n in sorted(out)]


def installed_version(key):
    try:
        with open(INSTALLED) as fh:
            entries = json.load(fh).get("plugins", {}).get(key) or []
        return (entries[0].get("version") or "").strip() or None
    except Exception:
        return None


def report(src_root, pol=None):
    pol = pol or policy()
    if pol == "off":
        return 0

    if not os.path.isdir(src_root):
        # A cloud-synced source folder that is not present is worth one line, not
        # silence: it is indistinguishable from "up to date" otherwise, and it is
        # the state in which an upgrade would be invisible.
        print("GOLDEN THREAD version: plugin source not readable at %s — "
              "cannot tell whether a newer release exists." % src_root)
        return 1

    lines, upgradable = [], False
    for key, subdir, label in PLUGINS:
        have = installed_version(key)
        vers = available(os.path.join(src_root, subdir))
        if not have or not vers:
            continue
        newest = vers[-1]
        hp, np_ = parse(have), parse(newest)
        if hp is None or np_ is None:
            continue
        if np_ > hp:
            lines.append("  %-8s %s installed, %s available" % (label, have, newest))
            upgradable = True
        elif hp > np_:
            lines.append("  %-8s %s installed, but the source tree has nothing newer "
                         "than %s (installed is AHEAD — capture it into the plugin "
                         "source rather than reinstalling over it)" % (label, have, newest))

    if not lines:
        # Said out loud rather than exiting silently: at SessionStart a mute check
        # cannot be told apart from one that never ran. Same reasoning as
        # gt_components and gt_workers.
        print("GOLDEN THREAD version: current — newest release installed.")
        return 0

    print("GOLDEN THREAD version:")
    print("\n".join(lines))
    # Only offered when something is actually STALE. On an `ahead`-only report this
    # line would recommend the one action that destroys the thing being reported:
    # install.sh copies the source down, so running it would silently downgrade the
    # newer installed release. Same principle as gt_components never auto-applying
    # `ahead` -- losing work is worse than being out of date.
    if upgradable:
        print('  install with: bash "%s/install.sh"   (then restart Claude Code)'
              % src_root)
    return len(lines)



def _emit(fn, *a, **kw):
    """Run a reporting function and deliver its output to the user as well as the
    model. See gt_settings.emit -- as a SessionStart hook, plain stdout reaches the
    model only, so a healthy check was invisible to the person it was reassuring.
    Falls back to printing if the settings module cannot be imported, because a
    degraded delivery path must never swallow the report entirely."""
    try:
        import gt_settings
    except Exception:
        return fn(*a, **kw)
    r, text = gt_settings.capture(fn, *a, **kw)
    gt_settings.emit(text)
    return r

def main():
    args = sys.argv[1:]
    if not args or args[0] != "check":
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_version_check.py check <plugin-source-root>")
        return 2
    root = args[1] if len(args) > 1 else None
    if not root:
        print("need the plugin source root (the directory holding install.sh)")
        return 2
    _emit(report, root)
    return 0      # never a failing exit: advisory only, like gt_components check


if __name__ == "__main__":
    raise SystemExit(main())
