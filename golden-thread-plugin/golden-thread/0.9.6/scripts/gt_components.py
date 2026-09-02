#!/usr/bin/env python3
"""Golden Thread component versioning: detect drift between what is INSTALLED and
what is CHECKED IN, and repair it under a policy the user chooses.

## The problem this exists for

The plugin's own CLAUDE.md warns that installed caches keep serving old content
unless the version is bumped and `install.sh` re-run. That is a convention, and
conventions fail the same way every other un-enforced rule in this system fails.

On 2026-08-29 the failure was found in the wild, in the enforcement layer itself:

  * `guard_session_claims.sh` / `.py` -- the hook enforcing Core rule 1 -- were
    INSTALLED but absent from the plugin source entirely.
  * `validate_response.sh` -- the timestamp validator -- was installed at 8,754
    bytes against the source's 8,408.

Two of the three enforcement mechanisms existed only on one machine. A fresh
install, or a second machine, would have had the Core rules as documents with
nothing re-asserting them.

## Why this does not simply overwrite

That drift ran INSTALLED-NEWER. A naive updater ("source is truth, copy it down")
would have reverted the timestamp validator to an older build and deleted the
claim guard outright -- destroying the very enforcement it was meant to keep
current, silently, on every session start.

So direction is inferred, never assumed:

  * `stale`   installed older than source        -> safe to update
  * `ahead`   installed NEWER than source        -> the SOURCE needs capturing;
                                                    never auto-overwritten
  * `missing` in source, absent installed        -> safe to install
  * `extra`   installed, absent from source      -> never deleted; reported

`ahead` and `extra` are reported and left alone under EVERY policy including
`auto`. Losing work is worse than being out of date.

## Policy

`~/.claude/vault-config.json` -> `"component_updates"`:

  | value     | behaviour                                                    |
  |-----------|--------------------------------------------------------------|
  | `off`     | no checking at all                                            |
  | `report`  | print drift, change nothing  (**default**)                    |
  | `confirm` | print drift and the exact command to apply it                 |
  | `auto`    | apply `stale`/`missing` silently; still only reports the rest  |

Default is `report`, not `auto`, deliberately. These files EXECUTE on every
prompt, and the plugin source lives in a cloud-synced folder -- so `auto` means a
sync from another machine, or a conflicted copy, can change what runs here without
anyone looking. That is a reasonable trade to opt into, not one to impose.
"""
import hashlib
import json
import os
import sys
import time

MANIFEST_NAME = "MANIFEST.json"
CONFIG = os.path.expanduser("~/.claude/vault-config.json")
INSTALLED_HOOKS = os.path.expanduser("~/.claude/golden-thread/hooks")
DEFAULT_POLICY = "report"
VALID_POLICIES = ("off", "report", "confirm", "auto")


def sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except Exception:
        return None
    return h.hexdigest()



def _registry_get(name, fallback):
    """Prefer the shared settings registry so defaults live in ONE place.

    gt_settings.py is the single source of truth for what a setting means and what
    it defaults to; reading the config key directly here would create a second
    default that drifts from it -- the same duplication the plugin forbids for rule
    text in hooks. Falls back to reading the file only if the registry is absent,
    so a partial install still behaves.
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
    def _raw():
        try:
            with open(CONFIG) as fh:
                v = (json.load(fh).get("component_updates") or "").strip().lower()
            if v in VALID_POLICIES:
                return v
        except Exception:
            pass
        return DEFAULT_POLICY
    return _registry_get("component_updates", _raw())


def build_manifest(version_dir, groups=("hooks", "scripts", "templates")):
    """Hash every shipped file. Written to <version_dir>/MANIFEST.json."""
    files = {}
    for g in groups:
        base = os.path.join(version_dir, g)
        if not os.path.isdir(base):
            continue
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith(".pyc") or n.startswith("."):
                    continue
                p = os.path.join(root, n)
                rel = os.path.relpath(p, version_dir)
                files[rel] = {"sha256": sha(p), "bytes": os.path.getsize(p)}
    return {"version": os.path.basename(version_dir.rstrip("/")),
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": files}


def compare(version_dir, installed_map):
    """-> list of {rel, state, src, dst}.

    `installed_map` maps a manifest-relative path to where it actually lives on
    this machine, because the plugin's layout and the install layout differ:
    hooks/x.sh ships under hooks/ but installs to ~/.claude/golden-thread/hooks/.
    """
    man_path = os.path.join(version_dir, MANIFEST_NAME)
    try:
        with open(man_path) as fh:
            man = json.load(fh)
    except Exception:
        return [{"rel": MANIFEST_NAME, "state": "no-manifest", "src": man_path, "dst": None}]

    rows = []
    for rel, meta in sorted(man.get("files", {}).items()):
        dst = installed_map(rel)
        if dst is None:
            continue                     # not something this machine installs
        src = os.path.join(version_dir, rel)
        if not os.path.exists(dst):
            rows.append({"rel": rel, "state": "missing", "src": src, "dst": dst})
            continue
        if sha(dst) == meta.get("sha256"):
            continue
        # Differs. Direction decides whether it is safe to touch.
        try:
            newer_installed = os.path.getmtime(dst) > os.path.getmtime(src)
        except Exception:
            newer_installed = False
        rows.append({"rel": rel, "state": "ahead" if newer_installed else "stale",
                     "src": src, "dst": dst})
    return rows


def hooks_installed_map(rel):
    """hooks/<f> -> ~/.claude/golden-thread/hooks/<f>; everything else uninstalled."""
    parts = rel.split(os.sep)
    if len(parts) == 2 and parts[0] == "hooks":
        return os.path.join(INSTALLED_HOOKS, parts[1])
    return None


def extras(version_dir):
    """Installed hook files with no counterpart in the source. Never deleted."""
    man_path = os.path.join(version_dir, MANIFEST_NAME)
    try:
        # Match on BASENAME across the WHOLE manifest, not just hooks/. The install
        # layout deliberately differs from the ship layout: gt_components.py and
        # gt_report_card.py ship under scripts/ but install into the hooks dir so
        # settings.json can address them by a stable absolute path. Scoping this to
        # hooks/ made the checker report its own two files as unknown extras on
        # every session start -- a check crying wolf about itself.
        with open(man_path) as fh:
            known = {os.path.basename(k) for k in json.load(fh).get("files", {})}
    except Exception:
        return []
    out = []
    if os.path.isdir(INSTALLED_HOOKS):
        for n in sorted(os.listdir(INSTALLED_HOOKS)):
            p = os.path.join(INSTALLED_HOOKS, n)
            if not os.path.isfile(p) or n.endswith(".pyc"):
                continue
            if n not in known:
                out.append(p)
    return out


def apply(rows):
    """Copy source over installed for `stale` and `missing` ONLY."""
    import shutil
    done = []
    for r in rows:
        if r["state"] not in ("stale", "missing"):
            continue
        try:
            os.makedirs(os.path.dirname(r["dst"]), exist_ok=True)
            shutil.copy2(r["src"], r["dst"])
            os.chmod(r["dst"], 0o755)
            done.append(r["rel"])
        except Exception:
            continue
    return done


def report(version_dir, pol=None):
    pol = pol or policy()
    if pol == "off":
        return 0
    rows = compare(version_dir, hooks_installed_map)
    ex = extras(version_dir)
    actionable = [r for r in rows if r["state"] in ("stale", "missing")]
    blocked = [r for r in rows if r["state"] in ("ahead", "no-manifest")]
    if not rows and not ex:
        # Say so out loud rather than exiting silently -- at SessionStart a mute
        # check cannot be told apart from one that never ran. See gt_workers.
        print("GOLDEN THREAD components: clean — installed matches %s."
              % os.path.basename(version_dir))
        return 0

    lines = []
    for r in actionable:
        lines.append("  %-9s %s" % (r["state"], r["rel"]))
    for r in blocked:
        if r["state"] == "ahead":
            lines.append("  %-9s %s  (installed is NEWER — the plugin source needs "
                         "updating from it, not the other way round)" % ("ahead", r["rel"]))
        else:
            lines.append("  %-9s %s" % (r["state"], r["rel"]))
    for p in ex:
        lines.append("  %-9s %s  (installed, absent from plugin source)"
                     % ("extra", os.path.basename(p)))

    if pol == "auto" and actionable:
        done = apply(actionable)
        lines.append("  applied automatically: %s" % (", ".join(done) or "nothing"))
    print("GOLDEN THREAD components drifted from %s:" % os.path.basename(version_dir))
    print("\n".join(lines))
    if pol == "confirm" and actionable:
        print("  apply with: python3 %s apply %s" % (os.path.abspath(__file__), version_dir))
    if blocked or ex:
        print("  `ahead`/`extra` are never auto-applied — that would revert or delete "
              "work that exists only here. Capture them into the plugin instead.")
    return len(rows) + len(ex)



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
    if not args:
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_components.py [manifest|check|apply] <version-dir>")
        return 2
    cmd = args[0]
    vdir = args[1] if len(args) > 1 else None
    # every command below reads the version dir. check and apply used to pass
    # None straight into os.path.join and die on a raw TypeError, so a typo at
    # the command line looked like a broken install rather than a missing arg.
    if cmd in ("manifest", "check", "apply") and not vdir:
        print("need a version dir")
        print("usage: gt_components.py [manifest|check|apply] <version-dir>")
        return 2
    if cmd == "manifest":
        man = build_manifest(vdir)
        with open(os.path.join(vdir, MANIFEST_NAME), "w") as fh:
            json.dump(man, fh, indent=1, sort_keys=True)
        print("wrote %s with %d file(s)" % (MANIFEST_NAME, len(man["files"])))
    elif cmd == "check":
        _emit(report, vdir)
        return 0                                 # never a failing exit: advisory only
    elif cmd == "apply":
        rows = compare(vdir, hooks_installed_map)
        done = apply([r for r in rows if r["state"] in ("stale", "missing")])
        print("applied: %s" % (", ".join(done) or "nothing"))
    else:
        print("usage: gt_components.py [manifest|check|apply] <version-dir>")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
