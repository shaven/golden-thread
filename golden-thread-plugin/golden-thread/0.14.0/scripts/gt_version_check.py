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

CHOICES = os.path.expanduser("~/.claude/golden-thread/install-choices.json")

# name in installed_plugins.json  ->  directory under the plugin source root, label, and
# the module name users type in install.sh --with/--without (None: gt core, never optional)
PLUGINS = (
    ("gt@golden-thread-plugin", "golden-thread", "gt", None),
    ("gt-wiki@golden-thread-plugin", "golden-thread-wiki", "gt-wiki", "wiki"),
)


def install_choices():
    """The `choices` map of install-choices.json, or {} if it is absent or unreadable.

    Read-only. Since 0.14.0 a module can be switched off on purpose (install.sh
    --without NAME); an off module is not installed, and saying nothing about it would
    look exactly like one that went missing. Absent file -> {} -> behaviour as before.
    """
    try:
        with open(CHOICES) as fh:
            choices = json.load(fh).get("choices")
    except Exception:
        return {}
    return choices if isinstance(choices, dict) else {}


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


def install_record():
    """The `plugins` map of installed_plugins.json, or None if it cannot be read."""
    try:
        with open(INSTALLED) as fh:
            plugins = json.load(fh).get("plugins")
    except Exception:
        return None
    return plugins if isinstance(plugins, dict) else None


def installed_version(key, record=None):
    record = install_record() if record is None else record
    try:
        entries = (record or {}).get(key) or []
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

    record = install_record()
    if record is None:
        # Without the install record every plugin would be skipped below and the
        # empty result read as "current" -- the false all-clear this check exists
        # to prevent.
        print("GOLDEN THREAD version: unknown — cannot read %s, so cannot tell "
              "whether the newest release is installed." % INSTALLED)
        return 1

    lines, notes, upgradable, compared = [], [], False, 0
    choices = install_choices()
    for key, subdir, label, module in PLUGINS:
        have = installed_version(key, record)
        if module and choices.get(module) == "off":
            # Off by choice is not stale and not missing: report it, never compare it,
            # never recommend installing it.
            if have:
                notes.append("  %-8s %s installed, but install-choices.json has %s off "
                             "(the next install.sh run removes it)" % (label, have, module))
            else:
                notes.append("  %-8s not installed by choice (install-choices.json: %s off; "
                             "bash install.sh --with %s to add it)" % (label, module, module))
            continue
        vers = available(os.path.join(src_root, subdir))
        if not have or not vers:
            continue
        newest = vers[-1]
        hp, np_ = parse(have), parse(newest)
        if hp is None or np_ is None:
            continue
        compared += 1
        if np_ > hp:
            lines.append("  %-8s %s installed, %s available" % (label, have, newest))
            upgradable = True
        elif hp > np_:
            lines.append("  %-8s %s installed, but the source tree has nothing newer "
                         "than %s (installed is AHEAD — capture it into the plugin "
                         "source rather than reinstalling over it)" % (label, have, newest))

    if not lines and not compared:
        print("GOLDEN THREAD version: unknown — no installed plugin could be compared "
              "with a release in %s." % src_root)
        if notes:
            print("\n".join(notes))
        return 1

    if not lines:
        # Said out loud rather than exiting silently: at SessionStart a mute check
        # cannot be told apart from one that never ran. Same reasoning as
        # gt_components and gt_workers.
        print("GOLDEN THREAD version: current — newest release installed.")
        if notes:
            print("\n".join(notes))
        return 0

    print("GOLDEN THREAD version:")
    print("\n".join(lines + notes))
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

    Degrades to a plain print, never to silence: if gt_settings cannot be imported
    OR is an older copy without capture/emit (a stale file in the hooks dir looked
    exactly like this on 2026-09-05 and would have crashed all four SessionStart
    checks while the component check reported clean), the report is printed
    directly. fn runs exactly once on every path."""
    try:
        import gt_settings
        capture, emit = gt_settings.capture, gt_settings.emit
    except Exception:
        return fn(*a, **kw)
    r, text = capture(fn, *a, **kw)
    try:
        emit(text)
    except Exception:
        print(text)
    return r

def main():
    args = [x for x in sys.argv[1:] if x != "--hook"]   # see gt_settings.hook_args
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
