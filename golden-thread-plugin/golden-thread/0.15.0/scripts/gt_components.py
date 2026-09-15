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


def compare(version_dir, installed_map, cache_plugin="gt"):
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
        if _came_from_an_install(version_dir, rel, installed_sha, cache_plugin):
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


def _came_from_an_install(version_dir, rel, installed_sha, cache_plugin="gt"):
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

    # A module's copies live under its own plugin name (0.15.0: modules are drift-checked too).
    cache = os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/%s" % cache_plugin)
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
#
# gt_watch.py and gt_report_card.py left this list in 0.15.0: they ship in the watch and
# report-card MODULES, whose module.json declares them (hookdir_scripts, hooks) and which
# install them to the same path with the same settings.json command, so an existing
# install's files and entries are recognised as the module's rather than reported unknown.
HOOK_DIR_SCRIPTS = ("gt_paths.py", "gt_components.py",
                    "gt_settings.py", "gt_workers.py", "gt_version_check.py",
                    "gt_push_check.py",
                    "gt_lint.py",         # the vault linter lives in the hooks dir too (used by gt_lint_weekly.py and /gt:gt-lint)
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
# repair advice differs. install.sh wires the reporting hooks; vault_init.py wires
# the enforcement hooks when a vault is initialised (the counts are whatever the
# list below holds -- never restate them here). Declaring both sets here means the
# check covers the enforcement layer too -- gt_lint.py checks
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
    # gt_watch.py (SessionStart) and gt_report_card.py (PreCompact, SessionEnd, SessionStart
    # surface) were registered here until 0.14.0. Since 0.15.0 the watch and report-card
    # modules declare them in module.json; module_registrations() appends them, tagged.
    # Protected paths: Write/Edit to core-rules/, global-memory/, the gt hooks dir or
    # settings.json forces the permission prompt; overwriting an existing Sources/ file
    # is denied. Not tied to a Core rule, and needs no vault to be wired, so install.sh
    # owns it rather than install-core-rules (0.12.9).
    {"event": "PreToolUse", "script": "guard_protected_paths.sh",
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


# --- modules (0.14.0) -----------------------------------------------------------------
#
# A module is a top-level plugin directory beside golden-thread/ whose newest release
# carries a module.json. The validator below is DELIBERATELY a copy of the one in
# dev/plugins.py: this file ships in the release and dev/ does not, so it cannot import
# it. tests/test_module_format.py runs both on the same fixtures and requires identical
# verdicts -- that test is what keeps two copies from becoming two rules.

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
        for k in sorted(set(h) - {"event", "script", "args", "kind"}):
            reasons.append("hooks[%d] unknown key %r" % (i, k))
        for k in ("event", "script", "kind"):
            if k not in h:
                reasons.append("hooks[%d] missing %r" % (i, k))
        if "event" in h and not (isinstance(h["event"], str) and re.match(_EVENT_RE, h["event"])):
            reasons.append("hooks[%d] event %r is not a hook event name" % (i, h["event"]))
        if "kind" in h and h["kind"] not in MODULE_HOOK_KINDS:
            reasons.append("hooks[%d] kind %r is not guard or reporter" % (i, h["kind"]))
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


def _newest_release(plugin_dir):
    """Newest N.N.N dir holding .claude-plugin/plugin.json -- the rule install.sh uses."""
    import re
    best = None
    try:
        names = os.listdir(plugin_dir)
    except OSError:
        return None
    for n in names:
        d = os.path.join(plugin_dir, n)
        if (re.fullmatch(r"\d+\.\d+\.\d+", n)
                and os.path.isfile(os.path.join(d, ".claude-plugin", "plugin.json"))):
            k = tuple(int(x) for x in n.split("."))
            if best is None or k > best[0]:
                best = (k, d)
    return best[1] if best else None


def discover_modules(plugin_root):
    """-> [{name, dir, version_dir, data, reasons}] for every module, sorted by name.

    A plugin dir whose NEWEST release has no module.json is not a module (gt core). An
    invalid module is still listed, reasons non-empty, so a user who types its name is
    told why it cannot be used rather than that it does not exist.
    """
    out = []
    if not plugin_root or not os.path.isdir(plugin_root):
        return out
    for d in sorted(os.listdir(plugin_root)):
        full = os.path.join(plugin_root, d)
        if d.startswith(".") or not os.path.isdir(full):
            continue
        vd = _newest_release(full)
        if vd is None:
            continue
        data, reasons = validate_module(vd)
        if data is None:
            continue
        name = data.get("name") if isinstance(data.get("name"), str) else d
        out.append({"name": name, "dir": d, "version_dir": vd, "data": data,
                    "reasons": reasons})
    return sorted(out, key=lambda m: m["name"])


def choices_path(home=None):
    """install-choices.json under `home` (the user's HOME, the dir holding .claude)."""
    return os.path.join(home or os.path.expanduser("~"), ".claude", "golden-thread",
                        "install-choices.json")


def read_choices(home=None):
    """-> {name: 'on'|'off'}; a missing or malformed file is no choices at all."""
    try:
        with open(choices_path(home), encoding="utf-8") as fh:
            ch = json.load(fh).get("choices") or {}
    except Exception:
        return {}
    if not isinstance(ch, dict):
        return {}
    return {k: v for k, v in ch.items() if isinstance(k, str) and v in ("on", "off")}


class ModuleNameError(ValueError):
    """An override or choice named gt, or something that is not a module."""


def record_choice(home, name, state, plugin_root=None):
    """Persist one module choice, keeping every other key. Atomic replace.

    With `plugin_root`, the name must be one of its modules."""
    import re
    if state not in ("on", "off"):
        raise ValueError("state must be on or off, not %r" % (state,))
    if name == "gt":
        raise ModuleNameError("gt is the core, not a module: it cannot be switched on or off")
    if not (isinstance(name, str) and re.match(_MODULE_NAME_RE, name)):
        raise ModuleNameError("%r is not a module name" % (name,))
    if plugin_root is not None:
        names = [m["name"] for m in discover_modules(plugin_root)]
        if name not in names:
            raise ModuleNameError("unknown module %r. modules: %s"
                                  % (name, ", ".join(names) or "(none)"))
    path = choices_path(home)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    doc.setdefault("version", 1)
    if not isinstance(doc.get("choices"), dict):
        doc["choices"] = {}
    doc["choices"][name] = state
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Same bytes and mode as gt_machine_migrate's write of this file (0.15.0, R1): sorted
    # keys, 0600 like every other state file the installer writes under ~/.claude
    # (settings.json, installed_plugins.json, machine-state.json all land 0600). The
    # file holds no secret; one mode is the point, so an upgrade equals a fresh install.
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def newest_gt_version(plugin_root):
    vd = _newest_release(os.path.join(plugin_root or "", "golden-thread"))
    return os.path.basename(vd) if vd else None


def module_detail(plugin_root, home=None, with_=(), without=(), gt_version=None):
    """-> {name: {state, reason, version, plugin, default, choice, requires_gt, admitted,
    valid, reasons, version_dir}}.

    Effective state, highest precedence first: this run's with/without overrides, the
    recorded choice, the module default. An invalid module, or one whose requires_gt
    does not admit `gt_version` (default: newest gt under plugin_root), is OFF for this
    run whatever was chosen; the recorded choice is not changed. Raises ModuleNameError
    for gt, for an unknown name, and for a name given both ways.
    """
    mods = discover_modules(plugin_root)
    names = [m["name"] for m in mods]
    for n in list(with_) + list(without):
        if n == "gt":
            raise ModuleNameError("gt is the core, not a module; --with/--without gt is "
                                  "refused. modules: %s" % (", ".join(names) or "(none)"))
        if n not in names:
            raise ModuleNameError("unknown module %r. modules: %s"
                                  % (n, ", ".join(names) or "(none)"))
    both = sorted(set(with_) & set(without))
    if both:
        raise ModuleNameError("both --with and --without given for: %s" % ", ".join(both))
    if gt_version is None:
        gt_version = newest_gt_version(plugin_root)
    choices = read_choices(home)
    out = {}
    for m in mods:
        d, n = m["data"], m["name"]
        default = d.get("default") if d.get("default") in ("on", "off") else "off"
        if n in with_:
            state, reason = "on", "--with"
        elif n in without:
            state, reason = "off", "--without"
        elif n in choices:
            state, reason = choices[n], "recorded choice"
        else:
            state, reason = default, "module default"
        admitted = None
        if not m["reasons"] and gt_version:
            try:
                admitted = requires_gt_admits(d.get("requires_gt"), gt_version)
            except ValueError:
                admitted = False
        if state == "on" and m["reasons"]:
            state, reason = "off", "invalid module.json: " + "; ".join(m["reasons"])
        elif state == "on" and admitted is False:
            state, reason = "off", ("requires gt %s, not %s -- skipped"
                                    % (d.get("requires_gt"), gt_version))
        out[n] = {"state": state, "reason": reason, "version": d.get("version"),
                  "plugin": d.get("plugin"), "default": default,
                  "choice": choices.get(n), "requires_gt": d.get("requires_gt"),
                  "admitted": admitted, "valid": not m["reasons"],
                  "reasons": m["reasons"], "version_dir": m["version_dir"]}
    return out


def module_states(plugin_root, home=None, with_=(), without=(), gt_version=None):
    """-> {name: 'on'|'off'}. See module_detail for the precedence."""
    return {n: v["state"] for n, v in
            module_detail(plugin_root, home, with_, without, gt_version).items()}


def active_modules(plugin_root, home=None, gt_version=None):
    """-> [(name, version_dir, data)] for every module effectively ON. Never raises."""
    try:
        det = module_detail(plugin_root, home, gt_version=gt_version)
        mods = discover_modules(plugin_root)
    except Exception:
        return []
    return [(m["name"], m["version_dir"], m["data"]) for m in mods
            if det.get(m["name"], {}).get("state") == "on"]


def plugin_root_from_settings(home=None):
    """The plugin source root, read from the gt_version_check registration install.sh
    wrote -- the same place gt_doctor reads it. None when not wired."""
    path = os.path.join(home or os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    events = (data.get("hooks") or {}) if isinstance(data, dict) else {}
    for blocks in events.values() if isinstance(events, dict) else []:
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                cmd = (h.get("command") or "") if isinstance(h, dict) else ""
                if "gt_version_check.py" not in cmd:
                    continue
                try:
                    parts = shlex.split(cmd)
                except ValueError:
                    continue
                for i, tok in enumerate(parts):
                    if tok == "check" and i + 1 < len(parts):
                        return parts[i + 1]
    return None


def module_registrations(plugin_root, home=None, gt_version=None, include_off=False):
    """Declared module hooks: [{event, script, args, owner, module, src, state}].

    Only valid modules; only ON ones unless include_off. Never raises."""
    try:
        det = module_detail(plugin_root, home, gt_version=gt_version)
        mods = discover_modules(plugin_root)
    except Exception:
        return []
    out = []
    for m in mods:
        st = det.get(m["name"], {}).get("state")
        if m["reasons"] or (st != "on" and not include_off):
            continue
        for h in m["data"].get("hooks") or []:
            out.append({"event": h["event"], "script": h["script"],
                        "args": list(h.get("args") or []), "owner": "install.sh",
                        "module": m["name"], "src": m["version_dir"], "state": st})
    return out


def hookdir_scripts(plugin_root=None, home=None, gt_version=None):
    """HOOK_DIR_SCRIPTS, then every ON module's hookdir_scripts, deduplicated."""
    names = list(HOOK_DIR_SCRIPTS)
    if plugin_root:
        for _n, _vd, data in active_modules(plugin_root, home, gt_version):
            for s in data.get("hookdir_scripts") or []:
                if s not in names:
                    names.append(s)
    return names


def module_hook_files(plugin_root, home=None, gt_version=None, include_off=False):
    """Basenames a module installs into the hooks dir (hook scripts + hookdir_scripts)."""
    try:
        det = module_detail(plugin_root, home, gt_version=gt_version)
        mods = discover_modules(plugin_root)
    except Exception:
        return set()
    out = set()
    for m in mods:
        if m["reasons"] or (det[m["name"]]["state"] != "on" and not include_off):
            continue
        out.update(h.get("script") for h in m["data"].get("hooks") or [])
        out.update(m["data"].get("hookdir_scripts") or [])
    return out


def hook_commands(version_dir, plugin_root=None, home=None):
    """Resolve HOOK_REGISTRATIONS into concrete commands for THIS machine.

    install.sh calls this (`gt_components.py hook-registrations <version-dir>`)
    instead of carrying its own copy of the list. A second copy in shell would be
    a copy that drifts -- the same reason HOOK_DIR_SCRIPTS is read rather than
    duplicated.

    Hooks of every module effectively ON (its newest release; choices read under `home`)
    follow gt's own, each dict gaining "module": <name>, owner install.sh. In a module
    hook's args `{src}` is the MODULE's version dir.
    """
    version_dir = os.path.abspath(version_dir)
    # .../golden-thread/<version>  ->  the repo root two levels up.
    plugin_root = os.path.abspath(
        plugin_root or os.path.dirname(os.path.dirname(version_dir)))
    regs = [dict(r, src=version_dir) for r in HOOK_REGISTRATIONS]
    regs += module_registrations(plugin_root, home, os.path.basename(version_dir))
    out = []
    for reg in regs:
        subs = {"hooks": INSTALLED_HOOKS, "src": reg["src"], "plugin_root": plugin_root}
        target = os.path.join(INSTALLED_HOOKS, reg["script"])
        argv = [a.format(**subs) for a in reg["args"]]
        # Shell scripts execute directly; .py files are run through python3 so the
        # entry does not depend on the file keeping its +x bit through a sync.
        parts = ([target] if reg["script"].endswith(".sh")
                 else ["python3", target]) + argv
        # Quote every part: the plugin source lives under a path containing a space
        # ("Golden Thread"), and an unquoted argument split on it so the checker was
        # handed "Golden" and reported a bogus no-manifest drift on every start.
        row = {"event": reg["event"], "script": reg["script"], "owner": reg["owner"],
               "command": " ".join(shlex.quote(x) for x in parts)}
        if "module" in reg:
            row["module"] = reg["module"]
        out.append(row)
    return out


def check_wiring(version_dir, settings_path=None, owner=None, plugin_root=None, home=None):
    """-> list of {script, event, state, detail}. States, worst first:

      unwired   no entry under that event references this script -- it never runs
      badpath   wired, but a path argument does not exist on this machine
      (ok)      omitted from the result; only problems are returned

    Reads the manifest's `hooks` section when present so an OLD installed release
    is judged against what IT declared, not against this file's newer list. Falls
    back to HOOK_REGISTRATIONS when the manifest predates 0.9.13.

    `owner` narrows the question to one installer's entries. install.sh needs this:
    it runs before any vault exists, so the enforcement hooks are legitimately
    unwired at that moment -- vault_init.py wires them when a vault is initialised.
    Asserting every registration at install time would report a failure on every first install
    and teach the reader to ignore the warning, which is worse than not checking.

    Module hooks (0.14.0) are appended from each module's newest module.json under
    `plugin_root` (default: two levels above version_dir). A registration whose module
    is effectively OFF is skipped: a module the user chose not to install is not drift.
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
    declared = list(declared) + declared_module_hooks(version_dir, plugin_root, home)
    # One row per (event, script). A manifest built before a script moved into a module
    # still declares it, and the ON module declares it again; counted twice it would be
    # reported twice when unwired and inflate "all N hooks wired" when not (0.15.0).
    seen, unique = set(), []
    for r in declared:
        k = (r.get("event"), r.get("script"))
        if k not in seen:
            seen.add(k)
            unique.append(r)
    declared = unique
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


def _hooks_dir_script(command, hooks_dir):
    """-> basename of the script in `hooks_dir` that `command` runs, or None."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        tokens = (command or "").split()
    real = os.path.realpath(hooks_dir)
    for tok in tokens:
        t = os.path.expanduser(tok.replace("${HOME}", "~").replace("$HOME", "~"))
        if os.path.isabs(t) and os.path.realpath(os.path.dirname(t)) == real:
            return os.path.basename(t)
    return None


def order_hook_blocks(hooks, order, hooks_dir=None):
    """Put gt's hook entries in ONE canonical order for every event. -> True if changed.

    Requirement R1 (0.15.0): an upgrade must leave settings.json as a fresh install does.
    Each writer used to append its own entry, so the order depended on history -- a
    fresh install wired guard_protected_paths.sh first (install.sh runs before the vault
    step), an upgrade from 0.13.0/0.14.0 moved it last (install.sh re-adds its entries at
    the end, the vault step finds its own already there).

    The canonical order, per event:
      1. every block that is not purely gt's -- the user's own hooks, or a block mixing
         theirs with ours -- first, in the relative order they already had;
      2. then gt's blocks in `order`, a list of (event, script) -- HOOK_REGISTRATIONS
         followed by module registrations, the order hook-registrations prints;
      3. then any other block running a script from the gt hooks dir that `order` does
         not name, in its existing relative order (left for the prune to judge).
    A block is gt's when every hook in it runs a script from the gt hooks dir. Nothing is
    added or removed here; blocks only move."""
    hooks_dir = hooks_dir or INSTALLED_HOOKS
    rank = {}
    for i, (event, script) in enumerate(order):
        rank.setdefault((event, script), i)
    changed = False
    for event, blocks in list((hooks or {}).items()):
        if not isinstance(blocks, list):
            continue
        theirs, ours, unknown = [], [], []
        for pos, b in enumerate(blocks):
            hs = b.get("hooks") if isinstance(b, dict) else None
            scripts = ([_hooks_dir_script(h.get("command") if isinstance(h, dict) else None,
                                          hooks_dir) for h in hs]
                       if isinstance(hs, list) and hs else [None])
            if any(s is None for s in scripts):
                theirs.append(b)
            elif all((event, s) in rank for s in scripts):
                ours.append((min(rank[(event, s)] for s in scripts), pos, b))
            else:
                unknown.append(b)
        new = theirs + [b for _r, _p, b in sorted(ours, key=lambda x: (x[0], x[1]))] + unknown
        if any(a is not b for a, b in zip(new, blocks)):
            blocks[:] = new
            changed = True
    return order_hook_events(hooks, hooks_dir) or changed


# Claude Code's hook events in lifecycle order: the order gt's events are written in.
EVENT_ORDER = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "SubagentStop", "Stop", "PreCompact", "SessionEnd")


def order_hook_events(hooks, hooks_dir=None):
    """Put the EVENT keys of settings.json "hooks" in one canonical order. -> True if changed.

    R1 (0.15.0): a validator found the event key order differed between an upgrade and a
    fresh install (each writer appended the events it added). Rule: events with no block
    running a gt hooks-dir script first, in their existing relative order; then events
    that have one, in EVENT_ORDER; then any such event EVENT_ORDER does not name, in their
    existing relative order. The dict is reordered in place; no key or value changes."""
    if not isinstance(hooks, dict):
        return False
    hooks_dir = hooks_dir or INSTALLED_HOOKS

    def has_ours(blocks):
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") if isinstance(b, dict) else None) or []:
                if isinstance(h, dict) and _hooks_dir_script(h.get("command"), hooks_dir):
                    return True
        return False

    items = list(hooks.items())
    user = [(k, v) for k, v in items if not has_ours(v)]
    ours = [(k, v) for k, v in items if has_ours(v)]
    known = sorted((kv for kv in ours if kv[0] in EVENT_ORDER),
                   key=lambda kv: EVENT_ORDER.index(kv[0]))
    other = [kv for kv in ours if kv[0] not in EVENT_ORDER]
    new = user + known + other
    if [k for k, _ in new] == [k for k, _ in items]:
        return False
    hooks.clear()
    hooks.update(new)
    return True


def order_plugin_keys(mapping, first_keys, marketplace):
    """Order a plugin-keyed dict (enabledPlugins, installed_plugins.json plugins) in place.
    -> True if changed. Keys of other marketplaces first, in existing relative order; then
    `first_keys` (this install's plugins, gt first) in that order; then any other key of
    `marketplace` in existing relative order. R1 (0.15.0): an upgrade kept an older
    release's insertion order where a fresh install wrote this one."""
    if not isinstance(mapping, dict):
        return False
    suffix = "@" + marketplace
    items = list(mapping.items())
    foreign = [(k, v) for k, v in items if not str(k).endswith(suffix)]
    listed = [(k, mapping[k]) for k in first_keys if k in mapping]
    rest = [(k, v) for k, v in items if str(k).endswith(suffix) and k not in first_keys]
    new = foreign + listed + rest
    if [k for k, _ in new] == [k for k, _ in items]:
        return False
    mapping.clear()
    mapping.update(new)
    return True


def declared_module_hooks(version_dir, plugin_root=None, home=None):
    """ON modules' hook registrations, as check_wiring declares them. Never raises."""
    version_dir = os.path.abspath(version_dir)
    root = os.path.abspath(plugin_root or os.path.dirname(os.path.dirname(version_dir)))
    return [{"event": r["event"], "script": r["script"], "owner": r["owner"],
             "module": r["module"]}
            for r in module_registrations(root, home, os.path.basename(version_dir))]


def hooks_installed_map(rel):
    """hooks/<f> and scripts/<f in HOOK_DIR_SCRIPTS> -> ~/.claude/golden-thread/hooks/<f>;
    everything else uninstalled."""
    parts = rel.split(os.sep)
    if len(parts) == 2 and parts[0] == "hooks":
        return os.path.join(INSTALLED_HOOKS, parts[1])
    if len(parts) == 2 and parts[0] == "scripts" and parts[1] in HOOK_DIR_SCRIPTS:
        return os.path.join(INSTALLED_HOOKS, parts[1])
    return None


def module_compare(version_dir, home=None):
    """Drift rows for the hook-dir files of every ON module, each against ITS manifest.

    Until 0.15.0 no module installed anything into the hooks dir (demo and wiki declare no
    hooks), so compare() only ever read gt's manifest and nobody noticed that a module's
    hook-dir copy was never checked. Once watch and report card moved out of gt, their
    installed gt_watch.py / gt_report_card.py -- which run at every session start -- would
    have gone stale without a word. Each row's `rel` is prefixed with the module's plugin
    name so the report says whose file it is. Never raises.
    """
    version_dir = os.path.abspath(version_dir)
    root = os.path.dirname(os.path.dirname(version_dir))
    rows = []
    for _name, vd, data in active_modules(root, home, os.path.basename(version_dir)):
        names = set(data.get("hookdir_scripts") or [])
        names |= {h.get("script") for h in data.get("hooks") or [] if isinstance(h, dict)}
        names.discard(None)
        if not names:
            continue

        def mapper(rel, names=names):
            parts = rel.split(os.sep)
            if len(parts) == 2 and parts[0] in ("hooks", "scripts") and parts[1] in names:
                return os.path.join(INSTALLED_HOOKS, parts[1])
            return None
        plugin = data.get("plugin") or _name
        try:
            found = compare(vd, mapper, cache_plugin=plugin)
        except Exception:
            continue
        rows.extend(dict(r, rel="%s %s" % (plugin, r["rel"])) for r in found)
    return rows



def duplicate_destinations(version_dir, installed_map=None):
    """-> {destination: [manifest rels]} for destinations claimed by MORE THAN ONE file.

    Two shipped files that install to the same path are a defect even when the install
    happens to resolve correctly, because the MANIFEST then records two different hashes
    for one destination and the drift check must disagree with one of them forever.

    The incident: `gt_paths.py` shipped from BOTH hooks/ (a 0.12.2-era copy, last correct
    before 0.12.4) and scripts/ (current). install.sh copies hooks/* first and then
    overwrites with scripts/gt_paths.py, so the right file won -- by ordering, not by
    design. Every machine then reported permanent drift on hooks/gt_paths.py, and once
    0.12.7 started resolving direction by identity it called that row `stale`, which is
    the AUTO-APPLIABLE state: `component_updates=auto` would have copied the 0.12.2 file
    over the correct one and silently disabled the gated_by/budget_from keys the parallel
    Core rule depends on. A phantom that became destructive.

    Uses hooks_installed_map by default -- the same mapper the drift check uses, so this
    cannot disagree with it about where a file goes.
    """
    installed_map = installed_map or hooks_installed_map
    try:
        with open(os.path.join(version_dir, MANIFEST_NAME)) as fh:
            files = json.load(fh).get("files") or {}
    except Exception:
        return {}
    claims = {}
    for rel in sorted(files):
        dst = installed_map(rel)
        if dst:
            claims.setdefault(dst, []).append(rel)
    return {dst: rels for dst, rels in claims.items() if len(rels) > 1}

def extras(version_dir):
    """Installed hook files with no counterpart in the source. Never deleted."""
    man_path = os.path.join(version_dir, MANIFEST_NAME)
    try:
        # Match on BASENAME across the WHOLE manifest, not just hooks/. The install
        # layout deliberately differs from the ship layout: gt_components.py and
        # gt_settings.py ship under scripts/ but install into the hooks dir so
        # settings.json can address them by a stable absolute path. Scoping this to
        # hooks/ made the checker report its own two files as unknown extras on
        # every session start -- a check crying wolf about itself.
        with open(man_path) as fh:
            known = {os.path.basename(k) for k in json.load(fh).get("files", {})}
    except Exception:
        return []
    # An ON module's hook-dir files are expected there; they live in ITS manifest.
    root = os.path.dirname(os.path.dirname(os.path.abspath(version_dir)))
    known |= module_hook_files(root, gt_version=os.path.basename(os.path.abspath(version_dir)))
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
    rows = compare(version_dir, hooks_installed_map) + module_compare(version_dir)
    ex = extras(version_dir)
    wiring = check_wiring(version_dir)
    n_hooks = len(HOOK_REGISTRATIONS) + len(declared_module_hooks(version_dir))
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
                                       n_hooks))
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

MODULE_USAGE = ("modules <plugin-root>  |  module-check <module-version-dir> [--gt V]"
                "  |  module-states <plugin-root> [--home H] [--gt-version V]"
                " [--with NAME]... [--without NAME]... [--detail]"
                "  |  record-choice <home> <name> on|off [--plugin-root R]")


def _take_opt(args, flag, multi=False):
    """Remove `flag VALUE` pairs from args -> (remaining, value or [values])."""
    rest, vals, it = [], [], iter(args)
    for a in it:
        if a == flag:
            v = next(it, None)
            if v is None:
                raise ValueError("%s needs a value" % flag)
            vals.append(v)
        else:
            rest.append(a)
    return rest, (vals if multi else (vals[-1] if vals else None))


def module_main(cmd, args):
    """The module CLI. install.sh codes against these exact signatures:

      modules <plugin-root>                      "name plugin version default" per module
      module-check <module-version-dir> [--gt V] 0 valid, 1 invalid (reasons), 2 not a module
      module-states <plugin-root> [--home H] [--gt-version V] [--with N]... [--without N]...
                    [--detail]                   JSON {name: on|off} (or detail); exit 2
                                                 naming the modules for an unknown name/gt
      record-choice <home> <name> on|off [--plugin-root R]
    """
    try:
        args, home = _take_opt(args, "--home")
        args, gtv = _take_opt(args, "--gt-version")
        args, gt_admit = _take_opt(args, "--gt")
        args, withs = _take_opt(args, "--with", multi=True)
        args, withouts = _take_opt(args, "--without", multi=True)
        args, proot = _take_opt(args, "--plugin-root")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    detail = "--detail" in args
    pos = [a for a in args if not a.startswith("--")]
    if cmd == "modules" and len(pos) == 1:
        for m in discover_modules(pos[0]):
            d = m["data"]
            print("%s %s %s %s" % (m["name"], d.get("plugin"), d.get("version"),
                                   d.get("default")))
        return 0
    if cmd == "module-check" and len(pos) == 1:
        data, reasons = validate_module(pos[0])
        if data is None:
            print("%s is not a module (no %s)" % (pos[0], MODULE_FILE))
            return 2
        if not reasons and gt_admit:
            try:
                if not requires_gt_admits(data.get("requires_gt"), gt_admit):
                    reasons = ["requires_gt %s does not admit gt %s"
                               % (data.get("requires_gt"), gt_admit)]
            except ValueError as e:
                reasons = [str(e)]
        for r in reasons:
            print("  %s" % r)
        print("%s: %s" % (pos[0], "invalid" if reasons else "valid module %s %s"
                          % (data.get("name"), data.get("version"))))
        return 1 if reasons else 0
    if cmd == "module-states" and len(pos) == 1:
        try:
            det = module_detail(pos[0], home, withs, withouts, gtv)
        except ModuleNameError as e:
            print(str(e), file=sys.stderr)
            print(str(e))
            return 2
        for n, v in det.items():
            if v["state"] == "off" and v["reason"].startswith(("requires gt", "invalid")):
                print("module %s: %s" % (n, v["reason"]), file=sys.stderr)
        print(json.dumps(det if detail else {n: v["state"] for n, v in det.items()},
                         indent=1, sort_keys=True))
        return 0
    if cmd == "record-choice" and len(pos) == 3:
        try:
            path = record_choice(pos[0], pos[1], pos[2], plugin_root=proot)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            print(str(e))
            return 2
        print("recorded %s=%s in %s" % (pos[1], pos[2], path))
        return 0
    print("usage: gt_components.py " + MODULE_USAGE)
    return 2


def main():
    args = [x for x in sys.argv[1:] if x != "--hook"]   # see gt_settings.hook_args
    if args and args[0] in ("modules", "module-check", "module-states", "record-choice"):
        return module_main(args[0], args[1:])
    try:
        args, home_opt = _take_opt(args, "--home")
    except ValueError as e:
        print(str(e))
        return 2
    if not args:
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_components.py [manifest|check|apply|wiring] <version-dir>"
              "  |  verify-source <version-dir>"
              "  |  hook-registrations <version-dir> [plugin-root] [--home H]"
              "  |  hookdir-scripts [plugin-root] [--home H]  |  " + MODULE_USAGE)
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
              "  |  hook-registrations <version-dir> [plugin-root] [--home H]"
              "  |  hookdir-scripts [plugin-root] [--home H]  |  " + MODULE_USAGE)
        return 2
    if cmd == "verify-source":
        code, _bad, _dirty = verify_source(vdir)
        return code
    if cmd == "hookdir-scripts":
        # With a plugin root, ON modules' hookdir_scripts follow gt's own.
        print("\n".join(hookdir_scripts(pos[0] if pos else None, home_opt)))
        return 0
    if cmd == "hook-registrations":
        # JSON on stdout: install.sh consumes this instead of hardcoding the list.
        print(json.dumps(hook_commands(vdir, args[2] if len(args) > 2 else None, home_opt),
                         indent=1))
        return 0
    if cmd == "wiring":
        # Standalone so selftest.sh and install.sh can ask the question from OUTSIDE
        # the hooks -- the one vantage point that still works when nothing is wired.
        own = None
        for k, a in enumerate(args):
            if a == "--owner" and k + 1 < len(args):
                own = args[k + 1]
        bad = check_wiring(vdir, owner=own, home=home_opt)
        for w in bad:
            print("%-8s %-24s %s  (%s)" % (w["state"], w["script"], w["event"],
                                           w["detail"] or w["owner"]))
        if not bad:
            n = len([r for r in list(HOOK_REGISTRATIONS)
                     + declared_module_hooks(vdir, home=home_opt)
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
        rows = compare(vdir, hooks_installed_map) + module_compare(vdir, home_opt)
        done = apply([r for r in rows if r["state"] in ("stale", "missing")])
        print("applied: %s" % (", ".join(done) or "nothing"))
    else:
        print("usage: gt_components.py [manifest|check|apply|wiring] <version-dir>"
              "  |  verify-source <version-dir>"
              "  |  hook-registrations <version-dir> [plugin-root] [--home H]"
              "  |  hookdir-scripts [plugin-root] [--home H]  |  " + MODULE_USAGE)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
