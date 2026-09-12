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

## The second axis: WIRED, not merely PRESENT  (0.9.13)

Everything above answers one question -- *are the right files here, unmodified?*
It cannot answer the question that actually matters: *is any of it connected to
anything?*

Found on 2026-09-10 on a second machine. Every file installed correctly, the
manifest verified clean, and not one SessionStart check ever ran, because
`settings.json` had no SessionStart entries at all. The manifest could not have
caught it: it held `files`, `generated` and `version` and nothing else, so
"components clean" truthfully meant "the files are present" while the honest
answer was "none of this executes."

That is bootstrap blindness, not an oversight. The only components that could
report "SessionStart is not wired" ARE the SessionStart hooks; unwired, they never
run to say so. The check therefore has to live in something that runs for another
reason -- this file, wired to the very event it verifies -- plus `selftest.sh` for
the cold case where nothing is wired at all and nothing can self-report.

`HOOK_REGISTRATIONS` below declares every event/script pair the plugin expects to
find in `settings.json`. It is written into `MANIFEST.json` at build time and
compared against live settings at check time, and it is also the list `install.sh`
registers FROM -- one declaration read by both, so the installer and the checker
cannot disagree about what "wired" means.
"""
import hashlib
import json
import os
import shlex
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
    # `hooks` is what makes "components clean" mean WIRED as well as PRESENT. It
    # records the declaration only -- event, script, owner -- never a resolved
    # command, which would be machine-specific and wrong everywhere else.
    return {"version": os.path.basename(version_dir.rstrip("/")),
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hooks": [{"event": r["event"], "script": r["script"],
                       "args": list(r["args"]), "owner": r["owner"]}
                      for r in HOOK_REGISTRATIONS],
            "files": files}


# --- verify-source: does this SOURCE TREE match its own committed manifest? ----------
#
# compare() answers "does what is INSTALLED match the manifest". This answers the other
# question, at install time: "do the files I am about to install match the manifest that
# ships beside them". Since 0.12.6 install.sh no longer regenerates the manifest, so the
# two CAN now disagree, and until 0.12.7 nothing at install time noticed.
#
# Why it matters on a machine that is not this one: dev/release-check.sh catches a stale
# manifest, but only where the gate runs. A second machine installing from gt-src would
# install files the manifest does not describe, be told nothing, and then report component
# drift at every session start for a mismatch the installer could have caught once. That
# happened here on 2026-09-12: 0.12.4 was committed with a stale manifest and only the
# gate saw it.
#
# Requested 2026-09-11 (2026-09-11-install-refuse-stale-manifest), decided by the owner
# 2026-09-12: refuse for files that EXECUTE, warn for files that are merely COPIED, and
# treat a developer's uncommitted edit as the warning case rather than the refusal case.
# That last clause is what keeps the check honest -- the author edits a script and installs
# to test it many times a day, and a gate that fires on the normal development loop gets
# overridden by reflex and then ignored.

EXECUTABLE_PREFIXES = ("hooks/", "scripts/")

VERIFY_CLEAN = 0        # manifest describes the tree (or only uncommitted edits differ)
VERIFY_EXECUTABLE = 3   # a committed file that RUNS disagrees -> refuse
VERIFY_INERT = 4        # only copied files disagree -> warn
VERIFY_NO_MANIFEST = 5  # nothing to compare against


def _uncommitted(version_dir, rels):
    """-> the subset of `rels` with uncommitted changes, per git.

    A developer mid-edit is the one case where a mismatch is EXPECTED, and git already
    knows. Not a git tree, no git, or any error -> empty set, so the caller treats the
    mismatch as real. Failing toward "this is genuine" is right here: the cost is a
    message a developer can override, not a silent wrong install.
    """
    import subprocess
    root = os.path.abspath(version_dir)
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return set()
        toplevel = top.stdout.strip()
        # --untracked-files=all, because a WHOLE NEW version directory is untracked and
        # porcelain would otherwise report it as one "?? golden-thread/0.12.7/" line and
        # name none of the files inside it. An untracked file is a local edit in progress
        # by definition -- it has never been committed -- so it belongs in the same
        # category as a modified tracked file.
        out = subprocess.run(["git", "-C", root, "status", "--porcelain",
                              "--untracked-files=all", "--", root],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return set()
    except Exception:
        return set()
    dirty = set()
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        full = os.path.realpath(os.path.join(toplevel, path))
        for rel in rels:
            target = os.path.realpath(os.path.join(root, rel))
            # Equal, or the reported path is a DIRECTORY containing it: git collapses an
            # untracked tree to its top directory even with -uall in some versions, and a
            # renamed parent reports the parent.
            if target == full or target.startswith(full.rstrip("/") + os.sep):
                dirty.add(rel)
    return dirty


def verify_source(version_dir):
    """-> (code, mismatching rels, uncommitted rels). Prints a report."""
    man_path = os.path.join(version_dir, MANIFEST_NAME)
    try:
        with open(man_path) as fh:
            have = json.load(fh)
    except Exception:
        print("no %s in %s" % (MANIFEST_NAME, version_dir))
        print("  fix: python3 %s/scripts/gt_components.py manifest %s"
              % (version_dir, version_dir))
        return VERIFY_NO_MANIFEST, [], set()

    want = build_manifest(version_dir)          # ONE hashing path, shared with the gate
    a, b = have.get("files") or {}, want["files"]
    bad = sorted(set(a) ^ set(b)) + sorted(r for r in set(a) & set(b) if a[r] != b[r])
    bad = sorted(set(bad))
    if not bad:
        print("source matches %s (%d files)" % (MANIFEST_NAME, len(b)))
        return VERIFY_CLEAN, [], set()

    dirty = _uncommitted(version_dir, bad)
    committed = [r for r in bad if r not in dirty]
    executable = [r for r in committed if r.startswith(EXECUTABLE_PREFIXES)]

    for rel in bad:
        why = "uncommitted local edit" if rel in dirty else (
            "EXECUTES" if rel.startswith(EXECUTABLE_PREFIXES) else "copied, not run")
        print("  %-58s %s" % (rel, why))
    print("  fix: python3 %s/scripts/gt_components.py manifest %s"
          % (version_dir, version_dir))

    if executable:
        return VERIFY_EXECUTABLE, bad, dirty
    if committed:
        return VERIFY_INERT, bad, dirty
    return VERIFY_CLEAN, bad, dirty            # every mismatch is a local edit in progress


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
        installed_sha = sha(dst)
        if installed_sha == meta.get("sha256"):
            continue
        # Differs. Direction decides whether it is safe to touch -- `stale` gets applied,
        # `ahead` never does, because reverting work that exists only here is the one
        # mistake this mechanism must not make.
        #
        # mtime ALONE CANNOT DECIDE IT, and used to. install.sh copies with plain `cp`, so
        # every installed file carries the install-time mtime and is therefore always
        # newer than the source it came from. Any content mismatch at all was reported as
        # `ahead` -- "installed is NEWER, the plugin source needs updating from it" --
        # which is never auto-applied, so the warning was permanent and the advice was
        # backwards. Seen on another machine 2026-09-12: gt_paths.py reported `ahead`
        # against 0.12.6 at every session start with nothing a user could do about it.
        #
        # So ask IDENTITY first: does the installed file match some OTHER release sitting
        # on this machine? Then it is a leftover from that install -- stale, and safe to
        # update. Only a file matching no release we can see is treated as possibly local
        # work, which keeps the protective default without applying it to everything.
        if _came_from_an_install(version_dir, rel, installed_sha):
            state = "stale"                  # identified as a copy an install deployed
        else:
            try:
                older = os.path.getmtime(dst) < os.path.getmtime(src)
            except Exception:
                older = False
            # An installed file OLDER than the source is unambiguously behind it. A
            # NEWER one proves nothing -- `cp` stamps every installed file with the
            # install time -- so the honest verdict is that the direction is unknown.
            # `differs` is treated exactly like `ahead`: never applied automatically.
            state = "stale" if older else "differs"
        rows.append({"rel": rel, "state": state, "src": src, "dst": dst})
    return rows


def _came_from_an_install(version_dir, rel, installed_sha):
    """Is `installed_sha` a copy this plugin itself deployed at some point?

    Two witnesses, both local, either of which settles the direction:

    1. **A sibling release's manifest.** The releases install.sh keeps beside this one
       (current + previous by policy). A hit means the installed file is a leftover from
       that release.

    2. **The plugin CACHE**, ~/.claude/plugins/cache/golden-thread-plugin/gt/<ver>/<rel>.
       This is the witness that works when there is only ONE release on disk, which is
       exactly the shape of a machine installing from gt-src -- gt-src holds the newest
       version and nothing else, so witness 1 has nothing to compare against. install.sh
       writes the cache and the hooks dir from the same source in the same run, so a
       hooks-dir copy that matches no cached copy while the cache matches the manifest is
       a leftover from an earlier install.

    A miss proves nothing either way; the caller falls back to mtime. Reported by another
    machine 2026-09-12: gt_paths.py called `ahead` against 0.12.6 at every session start,
    on a source tree with a single version directory.
    """
    if not installed_sha:
        return False
    parent = os.path.dirname(os.path.abspath(version_dir))
    here = os.path.basename(os.path.abspath(version_dir))

    def manifest_hash(man_path):
        try:
            with open(man_path) as fh:
                return ((json.load(fh).get("files") or {}).get(rel) or {}).get("sha256")
        except Exception:
            return None

    try:
        for name in os.listdir(parent):
            if name == here:
                continue
            if manifest_hash(os.path.join(parent, name, MANIFEST_NAME)) == installed_sha:
                return True
    except OSError:
        pass

    cache = os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt")
    try:
        versions = os.listdir(cache)
    except OSError:
        return False
    for ver in versions:
        cached = os.path.join(cache, ver, rel)
        if os.path.isfile(cached) and sha(cached) == installed_sha:
            return True
    return False


# Scripts that ship under scripts/ but INSTALL into the hooks dir, because
# settings.json addresses them by a stable absolute path. This tuple is the single
# source of that list: install.sh asks for it (`gt_components.py hookdir-scripts`)
# rather than carrying its own copy, and the drift check maps every name here so a
# stale copy in the hooks dir is reported and repaired like any hook. Before 0.9.7
# only hooks/* were mapped, so an old gt_settings.py beside four scripts that
# needed the new one crashed every SessionStart check while this reported clean.
HOOK_DIR_SCRIPTS = ("gt_paths.py", "gt_components.py", "gt_report_card.py",
                    "gt_settings.py", "gt_workers.py", "gt_version_check.py",
                    "gt_push_check.py",
                    "gt_watch.py",         # upstream repo watch: cron fetch + SessionStart report (0.10.0)
                    "gt_lint.py",          # the vault linter lives in the hooks dir too (used by gt_lint_weekly.py and /gt:gt-lint)
                    "gt_lint_weekly.py",   # weekly vault + wiki lint under launchd (0.9.11)
                    "gt_doctor.py",        # /gt:gt-doctor runs the other probes from here (0.12.0)
                    "gt_test_receipt.py")  # test-run receipts, read by the commit guard (0.12.5)


# Every hook entry the plugin expects to find in ~/.claude/settings.json.
#
# `args` is a TEMPLATE, not a command line: the concrete paths differ per machine
# (the plugin source lives wherever the user cloned it) and per release (the
# version dir moves on every bump), so storing a literal command would produce a
# manifest that is wrong on every machine but the one that built it.
#
#   {hooks}       ~/.claude/golden-thread/hooks   -- the stable install path
#   {src}         the version dir being installed (.../golden-thread/0.9.13)
#   {plugin_root} the plugin repo root            (.../golden-thread-plugin)
#
# `owner` records WHICH installer wires the entry, because two of them do and the
# repair advice differs. install.sh wires the seven reporting hooks; vault_init.py
# wires the three enforcement hooks when a vault is initialised. Declaring all
# ten here means the check covers the enforcement layer too -- gt_lint.py checks
# those, but only when someone runs it, which is exactly the on-demand gap that
# let this class of failure survive.
HOOK_REGISTRATIONS = (
    # -- reporting: registered by install.sh step 6 --------------------------
    # Handed the VERSION DIR: this check is pinned to the release it shipped with.
    {"event": "SessionStart", "script": "gt_components.py",
     "args": ["check", "{src}", "--hook"], "owner": "install.sh"},
    {"event": "SessionStart", "script": "gt_workers.py",
     "args": ["check", "--hook"], "owner": "install.sh"},
    # Handed the PLUGIN ROOT, not a version dir -- deliberately. gt_components is
    # pinned to its own release, which is what makes it blind to a newer one sitting
    # beside it; passing the root keeps this hook correct across version bumps.
    {"event": "SessionStart", "script": "gt_version_check.py",
     "args": ["check", "{plugin_root}", "--hook"], "owner": "install.sh"},
    # No path: reads vault-config.json itself, as the vault's own tooling does.
    {"event": "SessionStart", "script": "gt_push_check.py",
     "args": ["check", "--hook"], "owner": "install.sh"},
    # Read-only report of the upstream watch queue; silent while `watch` is off (0.10.0).
    {"event": "SessionStart", "script": "gt_watch.py",
     "args": ["--hook"], "owner": "install.sh"},
    {"event": "PreCompact", "script": "gt_report_card.py",
     "args": [], "owner": "install.sh"},
    {"event": "SessionEnd", "script": "gt_report_card.py",
     "args": [], "owner": "install.sh"},
    # -- enforcement: registered by vault_init.py install-core-rules ---------
    {"event": "UserPromptSubmit", "script": "inject_core_rules.sh",
     "args": [], "owner": "vault_init.py install-core-rules"},
    {"event": "Stop", "script": "validate_response.sh",
     "args": [], "owner": "vault_init.py install-core-rules"},
    {"event": "PreToolUse", "script": "guard_session_claims.sh",
     "args": [], "owner": "vault_init.py install-core-rules"},
    # A SECOND PreToolUse entry, deliberately separate from the first: one inspects
    # Write/Edit targets, this one Bash command lines. Folding them together would
    # make each blind to the other's tool set, and this event now carries two.
    # A THIRD PreToolUse entry. Same reasoning as the second: one guard inspects
    # Write/Edit targets, one inspects vault-tool command lines, and this one inspects
    # `git commit`. Three narrow guards that each fail open beat one guard that has to
    # understand everything.
    {"event": "PreToolUse", "script": "guard_test_before_commit.sh",
     "args": [], "owner": "vault_init.py install-core-rules"},
    {"event": "PreToolUse", "script": "guard_vault_writes.sh",
     "args": [], "owner": "vault_init.py install-core-rules"},
)


def hook_commands(version_dir, plugin_root=None):
    """Resolve HOOK_REGISTRATIONS into concrete commands for THIS machine.

    install.sh calls this (`gt_components.py hook-registrations <version-dir>`)
    instead of carrying its own copy of the list. A second copy in shell would be
    a copy that drifts -- the same reason HOOK_DIR_SCRIPTS is read rather than
    duplicated.
    """
    version_dir = os.path.abspath(version_dir)
    # .../golden-thread/<version>  ->  the repo root two levels up.
    plugin_root = os.path.abspath(
        plugin_root or os.path.dirname(os.path.dirname(version_dir)))
    subs = {"hooks": INSTALLED_HOOKS, "src": version_dir, "plugin_root": plugin_root}
    out = []
    for reg in HOOK_REGISTRATIONS:
        target = os.path.join(INSTALLED_HOOKS, reg["script"])
        argv = [a.format(**subs) for a in reg["args"]]
        # Shell scripts execute directly; .py files are run through python3 so the
        # entry does not depend on the file keeping its +x bit through a sync.
        parts = ([target] if reg["script"].endswith(".sh")
                 else ["python3", target]) + argv
        # Quote every part: the plugin source lives under a path containing a space
        # ("Golden Thread"), and an unquoted argument split on it so the checker was
        # handed "Golden" and reported a bogus no-manifest drift on every start.
        out.append({"event": reg["event"], "script": reg["script"],
                    "owner": reg["owner"],
                    "command": " ".join(shlex.quote(x) for x in parts)})
    return out


def check_wiring(version_dir, settings_path=None, owner=None):
    """-> list of {script, event, state, detail}. States, worst first:

      unwired   no entry under that event references this script -- it never runs
      badpath   wired, but a path argument does not exist on this machine
      (ok)      omitted from the result; only problems are returned

    Reads the manifest's `hooks` section when present so an OLD installed release
    is judged against what IT declared, not against this file's newer list. Falls
    back to HOOK_REGISTRATIONS when the manifest predates 0.9.13.

    `owner` narrows the question to one installer's entries. install.sh needs this:
    it runs before any vault exists, so the three enforcement hooks are legitimately
    unwired at that moment -- vault_init.py wires them when a vault is initialised.
    Asserting all nine at install time would report a failure on every first install
    and teach the reader to ignore the warning, which is worse than not checking.
    """
    settings_path = settings_path or os.path.expanduser("~/.claude/settings.json")
    try:
        with open(os.path.join(version_dir, MANIFEST_NAME)) as fh:
            declared = json.load(fh).get("hooks")
    except Exception:
        declared = None
    if not declared:
        declared = [{"event": r["event"], "script": r["script"], "owner": r["owner"]}
                    for r in HOOK_REGISTRATIONS]
    if owner:
        declared = [r for r in declared if r.get("owner") == owner]

    try:
        with open(settings_path) as fh:
            events = json.load(fh).get("hooks", {}) or {}
    except Exception:
        # No readable settings.json means nothing is wired -- report it as such
        # rather than passing quietly, which is the whole failure being closed.
        events = {}

    rows = []
    for reg in declared:
        cmds = [h.get("command") or ""
                for block in events.get(reg["event"], []) or []
                for h in (block.get("hooks") or [])]
        hit = next((c for c in cmds if reg["script"] in c), None)
        if hit is None:
            rows.append({"script": reg["script"], "event": reg["event"],
                         "owner": reg.get("owner", ""), "state": "unwired",
                         "detail": ""})
            continue
        # Wired. Now: do the paths it names still exist? This catches the version
        # dir deleted by a later bump, and the unquoted-path split that silently
        # handed the checker half a directory name.
        try:
            argv = shlex.split(hit)
        except ValueError:
            continue                       # unparseable quoting: not ours to judge
        for a in argv[1:]:
            if (os.sep in a) and not a.startswith("-") and not os.path.exists(a):
                rows.append({"script": reg["script"], "event": reg["event"],
                             "owner": reg.get("owner", ""), "state": "badpath",
                             "detail": a})
                break
    return rows


def hooks_installed_map(rel):
    """hooks/<f> and scripts/<f in HOOK_DIR_SCRIPTS> -> ~/.claude/golden-thread/hooks/<f>;
    everything else uninstalled."""
    parts = rel.split(os.sep)
    if len(parts) == 2 and parts[0] == "hooks":
        return os.path.join(INSTALLED_HOOKS, parts[1])
    if len(parts) == 2 and parts[0] == "scripts" and parts[1] in HOOK_DIR_SCRIPTS:
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
    wiring = check_wiring(version_dir)
    actionable = [r for r in rows if r["state"] in ("stale", "missing")]
    blocked = [r for r in rows if r["state"] in ("differs", "ahead", "no-manifest")]
    if not rows and not ex and not wiring:
        # Say so out loud rather than exiting silently -- at SessionStart a mute
        # check cannot be told apart from one that never ran. See gt_workers.
        #
        # "and wired" is load-bearing wording, not decoration: before 0.9.13 this
        # line said "clean" on a machine where nothing was connected to anything.
        # The word names the second axis so a clean report cannot be misread as
        # covering more than it does.
        print("GOLDEN THREAD components: clean — installed matches %s, "
              "all %d hooks wired." % (os.path.basename(version_dir),
                                       len(HOOK_REGISTRATIONS)))
        return 0

    lines = []
    for r in actionable:
        lines.append("  %-9s %s" % (r["state"], r["rel"]))
    for r in blocked:
        if r["state"] == "differs":
            lines.append("  %-9s %s  (installed copy differs from the plugin source and "
                         "which is newer could NOT be established — see below)"
                         % ("differs", r["rel"]))
        elif r["state"] == "ahead":
            lines.append("  %-9s %s  (installed is NEWER — the plugin source needs "
                         "updating from it, not the other way round)" % ("ahead", r["rel"]))
        else:
            lines.append("  %-9s %s" % (r["state"], r["rel"]))
    for p in ex:
        lines.append("  %-9s %s  (installed, absent from plugin source)"
                     % ("extra", os.path.basename(p)))
    for w in wiring:
        if w["state"] == "unwired":
            lines.append("  %-9s %s  (no %s entry in settings.json — it NEVER RUNS)"
                         % ("unwired", w["script"], w["event"]))
        else:
            lines.append("  %-9s %s  (%s hook names a path that does not exist: %s)"
                         % ("badpath", w["script"], w["event"], w["detail"]))

    if pol == "auto" and actionable:
        done = apply(actionable)
        lines.append("  applied automatically: %s" % (", ".join(done) or "nothing"))
    print("GOLDEN THREAD components drifted from %s:" % os.path.basename(version_dir))
    print("\n".join(lines))
    if pol == "confirm" and actionable:
        print("  apply with: python3 %s apply %s" % (os.path.abspath(__file__), version_dir))
    if blocked or ex:
        print("  `differs`/`ahead`/`extra` are never auto-applied — that would revert or "
              "delete work that exists only here.")
        # A message with no way out is a message people learn to scroll past. This one
        # was exactly that until 2026-09-12: another machine reported gt_paths.py `ahead`
        # at every session start, the advice said to capture it into the plugin, and the
        # installed copy turned out to be OLDER than the source -- the direction was never
        # established, only assumed from an mtime that `cp` had stamped at install time.
        for r in blocked:
            if r["state"] != "differs":
                continue
            print("  resolve %s:" % r["rel"])
            print("    1. see the difference:  diff %s %s" % (r["dst"], r["src"]))
            print("    2. installed copy worth keeping?  copy it into the plugin source, "
                  "regenerate the manifest, bump the version")
            print("    3. not worth keeping?  rm %s   then re-run install.sh" % r["dst"])
    if wiring:
        # Name the installer that owns each one. Hand-editing settings.json is not
        # offered as advice: the installer is idempotent and gets the quoting and
        # the two different path arguments right, which is where hand-wiring fails.
        owners = sorted({w["owner"] for w in wiring if w["owner"]})
        print("  A present-but-unwired file is the failure this check exists for — "
              "re-run the installer that owns it:")
        for o in owners:
            print("    %s" % ("bash <plugin-repo>/install.sh" if o == "install.sh"
                              else "python3 <plugin>/scripts/" + o))
    return len(rows) + len(ex) + len(wiring)



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
    if not args:
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_components.py [manifest|check|apply|wiring] <version-dir>"
              "  |  verify-source <version-dir>"
              "  |  hook-registrations <version-dir> [plugin-root]  |  hookdir-scripts")
        return 2
    cmd = args[0]
    # Skip flags -- and --owner's value -- when locating the positional version dir,
    # or `wiring --owner X <dir>` would take "--owner" or X as the directory.
    pos, rest = [], iter(args[1:])
    for a in rest:
        if a == "--owner":
            next(rest, None)
        elif not a.startswith("--"):
            pos.append(a)
    vdir = pos[0] if pos else None
    # every command below reads the version dir. check and apply used to pass
    # None straight into os.path.join and die on a raw TypeError, so a typo at
    # the command line looked like a broken install rather than a missing arg.
    if cmd in ("manifest", "check", "apply", "hook-registrations", "wiring",
               "verify-source") and not vdir:
        print("need a version dir")
        print("usage: gt_components.py [manifest|check|apply|wiring] <version-dir>"
              "  |  verify-source <version-dir>"
              "  |  hook-registrations <version-dir> [plugin-root]  |  hookdir-scripts")
        return 2
    if cmd == "verify-source":
        code, _bad, _dirty = verify_source(vdir)
        return code
    if cmd == "hookdir-scripts":
        print("\n".join(HOOK_DIR_SCRIPTS))
        return 0
    if cmd == "hook-registrations":
        # JSON on stdout: install.sh consumes this instead of hardcoding the list.
        print(json.dumps(hook_commands(vdir, args[2] if len(args) > 2 else None),
                         indent=1))
        return 0
    if cmd == "wiring":
        # Standalone so selftest.sh and install.sh can ask the question from OUTSIDE
        # the hooks -- the one vantage point that still works when nothing is wired.
        own = None
        for k, a in enumerate(args):
            if a == "--owner" and k + 1 < len(args):
                own = args[k + 1]
        bad = check_wiring(vdir, owner=own)
        for w in bad:
            print("%-8s %-24s %s  (%s)" % (w["state"], w["script"], w["event"],
                                           w["detail"] or w["owner"]))
        if not bad:
            n = len([r for r in HOOK_REGISTRATIONS
                     if not own or r["owner"] == own])
            print("all %d declared hooks are wired%s"
                  % (n, " (%s)" % own if own else ""))
        return 1 if bad else 0
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
        print("usage: gt_components.py [manifest|check|apply|wiring] <version-dir>"
              "  |  verify-source <version-dir>"
              "  |  hook-registrations <version-dir> [plugin-root]  |  hookdir-scripts")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
