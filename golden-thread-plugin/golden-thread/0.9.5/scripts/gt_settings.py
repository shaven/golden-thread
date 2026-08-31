#!/usr/bin/env python3
"""Golden Thread settings: one registry, one file, full user control.

Every behaviour this system performs on its own -- checking component drift at
session start, printing a report card at compact, and whatever is added later --
must be something the user can see and switch off. A system that acts
automatically and cannot be inspected or disabled is not trustworthy, however
good its intentions, and the whole point of Golden Thread is that a mechanism
which cannot be verified is not enforcement.

Settings live in `~/.claude/vault-config.json` beside `vault_path`. That file
already exists on every install and is already read by the hooks, so no new
location is introduced.

## Adding a setting

Append one entry to SETTINGS. Nothing else needs to change: `/gt:gt-settings`
renders whatever is registered, validates against `values`, and the reader
helper below gives any script the value with its default applied. A setting that
is not in this registry is not a setting -- it is an undocumented behaviour, and
that is the thing this file exists to prevent.
"""
import json
import os
import sys

CONFIG = os.path.expanduser("~/.claude/vault-config.json")

SETTINGS = {
    "component_updates": {
        "default": "report",
        "values": ["off", "report", "confirm", "auto"],
        "summary": "What to do when INSTALLED hooks/scripts differ from what is checked in.",
        "detail": (
            "off     nothing is checked\n"
            "report  print the drift, change nothing  (default)\n"
            "confirm print the drift and the exact command to apply it\n"
            "auto    apply stale/missing silently\n"
            "\n"
            "`auto` never overwrites a file where the INSTALLED copy is newer than the\n"
            "source, and never deletes one absent from the source. On 2026-08-29 the real\n"
            "drift ran that direction: a naive updater would have reverted the timestamp\n"
            "validator and deleted the claim guard. Note these files EXECUTE on every\n"
            "prompt and the plugin source sits in a cloud-synced folder, so `auto` means a\n"
            "sync from another machine can change what runs here."),
    },
    "version_check": {
        "default": "report",
        "values": ["off", "report"],
        "summary": "Check at session start whether a newer plugin version is checked in.",
        "detail": (
            "off     no checking\n"
            "report  name the newer version and how to install it  (default)\n"
            "\n"
            "`component_updates` asks whether the installed FILES match a given version.\n"
            "This asks whether that version is still the newest one available -- the axis\n"
            "the component check is blind along. On 2026-08-30 gt 0.6.0 was found\n"
            "installed with 0.9.4 checked in beside it since the day before, four hooks\n"
            "registered in settings.json pointing at files that had never been copied. A\n"
            "component check aimed at 0.6.0 reported clean the whole time.\n"
            "\n"
            "There is deliberately no `auto`. Installing a version rewrites hook\n"
            "registrations and prunes caches; doing that mid-session leaves the running\n"
            "session executing hooks that no longer match the ones on disk. An upgrade is\n"
            "a decision with a restart attached."),
    },
    "orphan_check": {
        "default": "report",
        "values": ["off", "report", "reap"],
        "summary": "Look for abandoned Claude WORKERS (background shells) at session start.",
        "detail": (
            "off     no checking\n"
            "report  list stalled workers and how to reap them  (default)\n"
            "reap    terminate stalled workers automatically\n"
            "\n"
            "A worker is judged by CPU consumed across its whole process tree, not by\n"
            "age and not by whether someone wrote a note about it. On 2026-08-29 ten\n"
            "orphans were found alive across three sessions, the oldest at 10 days 23\n"
            "hours, every one having burned under 0.05 seconds of CPU. All ten would\n"
            "have passed a documentation check.\n"
            "\n"
            "A DECLARED worker that is stalled is reported more urgently, not less --\n"
            "someone was told work was happening and it is not. `reap` only ever kills\n"
            "stalled workers; one consuming CPU is never touched."),
    },
    "report_card": {
        "default": "minimal",
        "values": ["off", "minimal", "full"],
        "summary": "Session report card at /compact, auto-compact and session end.",
        "detail": (
            "off     nothing\n"
            "minimal hygiene only -- what went wrong in THIS session  (default)\n"
            "full    hygiene, plus vault features available and unused\n"
            "\n"
            "Fires on PreCompact so it is produced while there is still context to write\n"
            "it in, rather than competing for the last of it at session end."),
    },
}


def _load():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(d):
    with open(CONFIG, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")


def get(name):
    """Value with the registered default applied. For use by hooks and scripts."""
    spec = SETTINGS.get(name)
    if not spec:
        return None
    v = (_load().get(name) or "").strip().lower()
    return v if v in spec["values"] else spec["default"]


def show():
    cfg = _load()
    print("Golden Thread settings   (%s)" % CONFIG)
    print()
    for name, spec in SETTINGS.items():
        raw = cfg.get(name)
        cur = get(name)
        mark = "" if raw else "   (default — not set in the file)"
        print("  %-20s %s%s" % (name, cur, mark))
        print("  %-20s %s" % ("", spec["summary"]))
        print("  %-20s options: %s" % ("", " | ".join(spec["values"])))
        print()
    print("change with:  python3 gt_settings.py set <name> <value>")
    print("explain with: python3 gt_settings.py explain <name>")
    return 0


def explain(name):
    spec = SETTINGS.get(name)
    if not spec:
        print("unknown setting: %s" % name)
        print("known: %s" % ", ".join(SETTINGS))
        return 2
    print("%s  (current: %s, default: %s)" % (name, get(name), spec["default"]))
    print()
    print(spec["summary"])
    print()
    print(spec["detail"])
    return 0


def set_value(name, value):
    spec = SETTINGS.get(name)
    if not spec:
        print("unknown setting: %s" % name)
        print("known: %s" % ", ".join(SETTINGS))
        return 2
    value = (value or "").strip().lower()
    if value not in spec["values"]:
        print("invalid value %r for %s" % (value, name))
        print("valid: %s" % " | ".join(spec["values"]))
        return 2
    d = _load()
    if not d.get("vault_path"):
        # Refuse to create a config that would leave the hooks unable to find the
        # vault -- writing a partial file here would break enforcement, not extend it.
        print("refusing to write %s: it has no vault_path. Run /gt:gt-init first."
              % CONFIG)
        return 2
    was = get(name)
    d[name] = value
    _save(d)
    print("%s: %s -> %s" % (name, was, value))
    return 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("show", "list"):
        return show()
    if a[0] == "explain" and len(a) > 1:
        return explain(a[1])
    if a[0] == "set" and len(a) > 2:
        return set_value(a[1], a[2])
    if a[0] == "get" and len(a) > 1:
        print(get(a[1]) or "")
        return 0
    print("usage: gt_settings.py [show | get <name> | set <name> <value> | explain <name>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
