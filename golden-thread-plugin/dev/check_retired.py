#!/usr/bin/env python3
"""check_retired.py — a release that stops installing something must say so in retired.json.

    dev/check_retired.py <new-version-dir> <previous-version-dir>

## Why

install.sh runs from gt-src, which carries only the newest release and no git history. So
when a release stops installing a hook-dir file or stops registering a hook, the ONLY way
an upgrade can remove what the older release left behind is if the new release carries
that knowledge itself, in `retired.json`. Owner requirement, 2026-09-14: someone on 0.12.8
who installs 0.14 directly must end identical to a fresh install, with no legacy items
stuck. A removal that is not recorded is a legacy item stuck forever on every machine that
skipped the release.

## What it compares

What each release INSTALLS into ~/.claude/golden-thread/hooks/ (the names of hooks/* plus
HOOK_DIR_SCRIPTS) and which scripts it REGISTERS (HOOK_REGISTRATIONS). Anything the
previous release installed or registered that the new one does not must appear in the new
release's retired.json (`hook_files[].installed_as` / `hook_registrations[]`).

Exit 0 clean, 1 unrecorded removals, 2 could not check (a check that could not look is
not a clean result).
"""
import importlib.util
import json
import sys
from pathlib import Path


def load_components(vdir):
    spec = importlib.util.spec_from_file_location(
        "gt_components_retired_%s" % vdir.name.replace(".", "_"), str(vdir / "scripts" / "gt_components.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def installs(vdir):
    comp = load_components(vdir)
    names = {p.name for p in (vdir / "hooks").iterdir()
             if p.is_file() and p.suffix in (".py", ".sh") and not p.name.startswith(".")}
    names |= set(getattr(comp, "HOOK_DIR_SCRIPTS", ()))
    regs = {r["script"] for r in getattr(comp, "HOOK_REGISTRATIONS", ())}
    return names, regs


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    new, old = Path(argv[1]), Path(argv[2])
    try:
        new_files, new_regs = installs(new)
        old_files, old_regs = installs(old)
        retired = json.loads((new / "retired.json").read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001 -- any failure is "could not check"
        print("UNCHECKED: %s" % e)
        return 2
    recorded_files = {e.get("installed_as") for e in retired.get("hook_files", [])}
    recorded_regs = set(retired.get("hook_registrations", []))
    problems = []
    for name in sorted(old_files - new_files):
        if name not in recorded_files:
            problems.append("%s no longer installs %s into the hooks dir, and retired.json does not "
                            "list it — an upgrade from %s would leave it behind" % (new.name, name, old.name))
    for script in sorted(old_regs - new_regs):
        if script not in recorded_regs:
            problems.append("%s no longer registers %s, and retired.json does not list it in "
                            "hook_registrations" % (new.name, script))
    for p in problems:
        print("  " + p)
    if problems:
        return 1
    print("retired.json records everything %s stopped installing since %s" % (new.name, old.name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
