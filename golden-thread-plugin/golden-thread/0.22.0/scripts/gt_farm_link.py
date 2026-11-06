#!/usr/bin/env python3
"""gt_farm_link.py -- how gt core finds the gt-farm module's scripts (0.21.4).

gt-farm is an optional module: it may be off, or not installed. Core never imports it at top level and
never fails because it is missing; it asks this file where its scripts are and gets None when they are
not there. The places, in order: $GT_FARM_SCRIPTS; the module beside this release in a source tree
(`golden-thread-farm/<newest>/scripts`); the plugin cache next to gt (`gt-farm/<newest>/scripts`); the
folder installed_plugins.json names for gt-farm; the stable hooks dir.

    from gt_farm_link import farm_scripts, farm_module, farm_script
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "gt_farm_calibration.py"


def _versions(d):
    try:
        names = [v for v in os.listdir(d) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    except OSError:
        return []
    return sorted(names, key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)


def farm_scripts(marker=MARKER):
    """-> the gt-farm scripts folder holding `marker`, or None."""
    cands = [os.environ.get("GT_FARM_SCRIPTS")]
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    for name in ("golden-thread-farm", "gt-farm"):
        cands += [os.path.join(root, name, v, "scripts") for v in _versions(os.path.join(root, name))]
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json"),
                  encoding="utf-8") as fh:
            ent = (json.load(fh).get("plugins", {}).get("gt-farm@golden-thread-plugin") or [])
        cands += [os.path.join(str(e.get("installPath") or ""), "scripts") for e in ent]
    except (OSError, ValueError, AttributeError):
        pass
    cands.append(os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks"))
    return next((c for c in cands if c and os.path.isfile(os.path.join(c, marker))), None)


def farm_script(name):
    """-> the full path of one gt-farm script, or None."""
    d = farm_scripts(name)
    return os.path.join(d, name) if d else None


def farm_module(name="gt_farm_calibration"):
    """-> the imported gt-farm module, or None when gt-farm is not installed or does not import."""
    d = farm_scripts(name + ".py")
    if not d:
        return None
    if d not in sys.path:
        sys.path.append(d)
    try:
        return __import__(name)
    except Exception:                                  # noqa: BLE001 - a missing module is a state, not a crash
        return None
