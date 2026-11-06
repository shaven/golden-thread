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
import sys as _sys
import os as _os
# gt-src is a published tree that must keep matching its SHA256SUMS, and this script is
# run from it or handed it: write no bytecode, here or in any child (0.19.1).
_sys.dont_write_bytecode = True
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import hashlib
import json
import os
import re
import shlex
import sys
import time

MANIFEST_NAME = "MANIFEST.json"
CONFIG = os.path.expanduser("~/.claude/vault-config.json")
INSTALLED_HOOKS = os.path.expanduser("~/.claude/golden-thread/hooks")
DEFAULT_POLICY = "report"
# The address of the public gt repository (a git URL), the default for the `gt_release_source` setting. It is EMPTY in the
# source tree on purpose: no account name is committed, and an empty value reads as "no release source set" (the release
# watch stays quiet, nothing breaks). The PUBLISH step stamps the real address here, in this one line, and nowhere else in
# this plugin (the gt-lotr module has its own line, lotrlib/catalog.py REPO, stamped the same way).
DEFAULT_RELEASE_SOURCE = "https://github.com/shaven/golden-thread"
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


# agents/ and workflows/ joined in 0.20.1 (review, low): an agent definition decides which tools
# a stage agent gets, and a workflow script runs; drift checks must see an edit to either.
MANIFEST_GROUPS = ("hooks", "scripts", "templates", "packs", "agents", "workflows")


def build_manifest(version_dir, groups=MANIFEST_GROUPS):
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
                # Keys are "/"-separated on every platform: MANIFEST.json is built on one
                # machine and read on another. Native Windows Python walks with "\\", so
                # every file read as both missing and extra and the installer refused.
                rel = os.path.relpath(p, version_dir).replace(os.sep, "/")
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

# packs/ is data, but it is the INPUT to security controls (ignore, secrets, classify)
# and is model-reachable for runbook/vocabulary, so a mismatch refuses like code rather
# than warning like an inert file (security review, 2026-09-16).
EXECUTABLE_PREFIXES = ("hooks/", "scripts/", "packs/", "agents/", "workflows/")

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
            # Say WHY it is refused, per kind. Labelling a JSON pack "EXECUTES" invited the
            # reader to conclude the checker was wrong and reach for the override, which is the
            # one reaction a refusal must not provoke (security review 2026-09-16).
            ("INPUT TO SECURITY CONTROLS" if rel.startswith("packs/")
             else "EXECUTES" if rel.startswith(EXECUTABLE_PREFIXES) else "copied, not run"))
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
        if _came_from_an_install(version_dir, rel, installed_sha, cache_plugin, dst):
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


def _came_from_an_install(version_dir, rel, installed_sha, cache_plugin="gt", dst=None):
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
        # Never let a file witness ITSELF. Packs install INTO this cache, so the candidate and
        # the file being judged are the same path: every edited pack answered True and was
        # labelled `stale`, the auto-apply state, so a tamper was silently overwritten and
        # never surfaced (review 2026-09-16). A hook-dir file is a different path and was
        # unaffected, which is why this went unnoticed until packs joined the check.
        if dst is not None and os.path.exists(cached):
            try:
                if os.path.samefile(cached, dst):
                    continue
            except OSError:
                pass
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
                    "gt_uninstall.py",     # the uninstall works after the source tree is gone (0.20.1)
                    "gt_test_receipt.py",  # test-run receipts, read by the commit guard (0.12.5)
                    # the scope contract (0.21.4): guard_scope_contract.py imports it to judge a
                    # raw `git commit` the way /gt:gt-allin-commit does; gt_settings (above) is
                    # the only other thing it imports
                    "gt_scope.py",
                    "gt_refusals.py",      # the repeat counter on guard refusals, imported by the eight guards (0.22.0)
                    "gt_fact_check.py",    # the claim check on vault writes, imported by gt_broker.py (0.22.0)
                    "gt_write_check.py",   # the subject check on new vault pages, imported by gt_broker.py (0.22.0)
                    # imported by hook-dir scripts (0.22.0); missing here, the imports fail open and the
                    # feature silently does nothing: gt_intake.py and gt_state.py record savings through
                    # gt_ledger; gt_broker.py scans writes through gt_island_scan (which starts its island
                    # through gt_sandbox_launch) and records would-have-blocked events through gt_shadow
                    "gt_ledger.py", "gt_shadow.py", "gt_island_scan.py", "gt_sandbox_launch.py",
                    "gt_queue_autodrain.py",  # drains the write queue at Stop and SessionStart (0.22.0)
                    "gt_secret_shapes.py",  # the credential shapes validate_response.sh checks every reply against (0.22.0)
                    # run by gt-lotr's `lotr onboard` (a separate plugin that finds it here): the no-tools reader
                    # of untrusted tool descriptions, and the summariser of large results (0.22.0)
                    "gt_sandbox_reader.py",
                    "gt_session_search.py",  # SessionStart catch-up of the opt-in session index (0.22.0)
                    "gt_addon_scan.py",      # SessionStart scan of a new or changed skill, plugin or CLAUDE.md (0.22.0)
                    "gt_mcp_pin.py",         # SessionStart pin of connected MCP servers' tool descriptions (0.22.0)
                    "gt_skill_report.py",    # the doctor's `skills` row: local skills unused for 60 days (0.22.0)
                    "gt_procedure_drafts.py",  # the doctor's `skills` row counts draft skills waiting; also the stage/approve CLI (0.22.0)
                    "gt_entry_dates.py",   # dates on findings: the broker's capture stamp and supersession notices, recall's entry age (0.22.0)
                    "gt_state.py",         # session state written before the context runs out
                    "gt_surface.py",       # MUST DO, handoffs and state shown at session start (0.17.2)
                    "gt_farm_link.py",     # how the surface (and settings) find the gt-farm module's scripts (phase 76: engine status line)
                    "gt_next.py",          # gt_surface's calm session start counts tasks with its parser (0.22.0)
                    "gt_bypass.py",        # gt_surface's BYPASS PERMISSIONS line; gt_io_policy's launch-flag check (0.22.0)
                    "gt_handoff_status.py",  # gt_surface imports it: what "waiting" means (0.17.2)
                    # The write queue and its broker (0.17.11): gt_lint_weekly, gt_daily and
                    # gt_handoff_status queue their vault writes from here, and the vault's
                    # gt_task.py finds the broker at this stable path to drain what it queued.
                    # gt_demote.py: the broker's claim reader when a vault has no gt_session.py.
                    "gt_write_queue.py", "gt_broker.py", "gt_demote.py",
                    # Every gt_schedule.JOBS script, and what it imports at run time (0.17.2).
                    # A scheduled job runs the INSTALLED copy -- a script under CloudStorage
                    # fails under launchd -- and gt_schedule refuses to install a job whose
                    # script is not here. gt_daily's job shipped in 0.17.1 and could never be
                    # installed, and gt_sweep's docstring promised a launchd job that did not
                    # exist (audit, 2026-09-28). dev/check_wiring_coverage.py now fails a JOBS
                    # script missing from this tuple, which is how both shipped unnoticed.
                    "gt_schedule.py",      # also imported by gt_doctor's `schedule` check
                    "gt_keyword_recall.py",  # vault_hints.py matches with it (0.19.1)
                    "gt_hints.py",         # Stop / UserPromptSubmit hint hooks (0.21.0, #34)
                    "gt_cut_advisor.py",   # Stop hook: suggests when a cut pays back (0.22.0)
                    "gt_auto_clear.py",    # Stop hook: shadow-mode auto-clear policy log (0.22.0, phase 143)
                    "gt_batch_ledger.py",  # PostToolBatch hook: batch-size ledger for Core rule 8, measure only (0.22.0, GAP-1)
                    # gt_surface (section order) and gt_hints (featured next steps) read the
                    # user's persona from beside themselves; its packs are ../packs (0.21.4)
                    "gt_persona.py",
                    "gt_turn_notes.py",    # inject_core_rules.sh adds reply_length, allowance and choices lines (0.21.0)
                    "gt_calibration_schedule.py",  # SessionStart: a due scheduled calibration fires once (0.21.0, #44)
                    # SessionStart (0.21.4): the skill run log's catch-up; it scans each new task
                    # digest with gt_intake_scan.py, which runs gt_secrets.py from beside itself
                    "gt_skill_runlog.py", "gt_intake_scan.py",
                    # the install guard body (0.22.0): hooks/guard_install.sh runs it
                    "gt_install_guard.py",
                    # PreToolUse on Agent|Task (0.21.0): an ad-hoc spawn gets a model and effort by
                    # its task; it resolves through the intent pack with gt_model_policy/gt_model
                    "gt_agent_model.py", "gt_model_policy.py", "gt_model.py",
                    "gt_agent_spec.py",    # gt_model_policy imports it; the doctor's model-policy row crashed without it (0.22.0)
                    # gt_model and the gt_auto_clear Stop hook import gt_model_records at module
                    # level and it reads gt_model_prices.json from beside itself; the installed
                    # hook died with ModuleNotFoundError without them. gt_agent_model's advice
                    # note imports the other two inside a fail-open try, so a miss was silent (0.22.0)
                    "gt_model_records.py", "gt_model_prices.json",
                    "gt_model_advice.py", "gt_model_classes.py",
                    "gt_supersede.py",     # gt_lint's supersedes-missing check imports it (0.19.1)
                    # 0.21.0: read and use recency -- gt_supersede and gt_keyword_recall order by
                    # it, and the guards record an `enforced` use when they name a Core rule.
                    "gt_use.py", "gt_use_record.py",
                    "gt_daily.py",         # job `daily` (stdlib only)
                    "gt_sweep.py",         # job `sweep`; its members and their imports follow
                    "gt_secrets.py", "gt_scan_code.py", "gt_check_report.py",
                    "gt_baseline.py",      # both scanners' content-keyed baselines (0.20.1)
                    "gt_registry.py", "gt_staged.py",
                    # 0.18.1: gt_push_check imports gt_sync for the opt-in behind-check
                    # (sync_check); gt_reminder is the `reminder` job's script, and
                    # gt_surface refreshes its deadlines mirror at session start.
                    "gt_sync.py", "gt_reminder.py",
                    # gt unlock (0.20.1): the authority, its client and CLI run from here so the
                    # hooks, gt-lotr and scheduled jobs find them at one stable path (gt-lotr
                    # imports gt_unlock_client and gt_ipc from this directory).
                    "gt_tty.py",           # has_terminal(): every presence/confirm guard imports it (0.21.4)
                    "gt_vault_location.py",  # gt_doctor's vault-location check imports it (0.21.4)
                    "gt_ipc.py", "gt_unlock.py", "gt_unlockd.py", "gt_unlockd_methods.py",
                    "gt_unlock_client.py", "gt_unlock_policy.py", "gt_unlock_factors.py",
                    "gt_unlock_totp.py", "gt_unlock_crypto.py", "gt_unlock_seal.py",
                    "gt_unlock_brokers.py", "gt_unlock_sso.py", "gt_unlock_touchid.py",
                    "gt_unlock_hello.py", "gt_unlock_hello.ps1", "gt_unlock_verify.py",
                    "gt_lock.py", "qrcodegen.py", "qrcodegen.LICENSE",
                    # gt sandbox mode (0.20.1): gt_settings, gt_write_queue, gt_doctor and
                    # gt_unlock_verify import it from beside themselves.
                    "gt_sandbox.py",
                    # stage agents' private scratch folders (0.20.1): gt_sandbox allows
                    # writes to their root and gt_doctor reports leaks, both from here.
                    "gt_scratch.py",
                    # can this Python write where gt writes? (0.20.1): gt_schedule's
                    # choose-interpreter, the broker's EPERM line and gt_doctor import it.
                    "gt_write_probe.py",
                    # one line, not a traceback, when the OS or gt sandbox mode refuses a
                    # vault tool (0.20.1): gt_broker, gt_write_queue, gt_lint and
                    # gt_write_probe import it from beside themselves.
                    "gt_errors.py",
                    # which vault paths are review targets, by identity not spelling (0.20.1):
                    # gt_broker imports it from beside itself.
                    "gt_review_target.py",
                    # the vault layout manifest reader (0.22.0): gt_write_queue and gt_broker
                    # import it from beside themselves
                    "gt_vault_manifest.py",
                    # the owner-confirmed #conflict accept (0.21.0): gt_broker imports it at install
                    "gt_accept.py",
                    # how a needs_owner refusal hands its command over (clipboard, or a
                    # self-deleting script) (0.21.4): gt_accept imports it from beside itself
                    "gt_owner_cmd.py",
                    # the pre-push hook of push_fingerprint calls it from this stable path
                    # (the plugin cache path changes with every version) (0.20.3)
                    "gt_push_guard.py", "gt_sign.py",
                    # timed grants and per-module lockdown rules (0.21.0): the hooks below call
                    # gt_grant.py from here, and it imports gt_lockdown (and gt_sandbox) beside it
                    "gt_grant.py", "gt_lockdown.py",
                    # the set-and-forget testing profile (0.21.0, #40): gt_lockdown's sweep and
                    # gt_settings import it from here; it judges a sandbox with gt_io_policy
                    "gt_testing_profile.py", "gt_io_policy.py",
                    # "why is my test run blocked?" (0.21.4): `gt_doctor.py testing` hands over to it,
                    # and it changes settings only through gt_testing_profile (above)
                    "gt_testing_doctor.py",
                    "gt_disposable.py",
                    # the cache-expiry guard (0.21.0): its idle_prompt and UserPromptSubmit hooks, and
                    # the one-shot job it schedules, all run it from this stable path
                    "gt_expiry_guard.py",
                    # MCP servers outside LOTR (0.20.1): gt_doctor's `mcp` row and
                    # gt_unlock_verify's `mcp` row import it from beside themselves.
                    "gt_mcp_inventory.py",
                    # installed-code integrity (0.21.0, the `paranoid` group): the group's data and
                    # words, the hooks' own check (imported by guard wrappers and hook scripts),
                    # and the CLI (status / verify / unlock / lock / the Bash guard hook)
                    "gt_paranoid.py", "gt_integrity_gate.py", "gt_integrity.py",
                    # single-use action tokens (0.21.0 #32): LOTR's engine loads the shared check from here
                    "gt_action_token.py",
                    # keeping the main conversation small (0.21.4): the spill hook (PostToolUse on Bash) and
                    # wrapper, the paste-intake hook (UserPromptSubmit) and digest packets, the Core-rules-once
                    # marker that inject_core_rules.sh calls, the break-even rule they all ask, and the
                    # owner-only file helper under them
                    "gt_private.py", "gt_rules_once.py", "gt_break_even.py", "gt_spill.py", "gt_spill_kinds.py",
                    "gt_intake.py",
                    # the Touch ID helper's source: install.sh builds it from here (macOS)
                    "gt-presence.swift")


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
    # THE STATE WRITE, on two events for two different reasons.
    #
    # UserPromptSubmit is the PRIMARY: it fires every turn, sees the context climb, and writes
    # ONCE when it crosses (flag point - margin) -- while there is still room to write properly.
    # `--write` is what makes it act rather than merely report; without it this would be another
    # meter nobody reads.
    #
    # PreCompact is the BACKSTOP and says so in its own output. By the time it fires the
    # compaction is already under way, so the write competes with the thing it exists to
    # survive. A backstop that is mistaken for the primary is how a gap stays open.
    {"event": "UserPromptSubmit", "script": "gt_state.py",
     "args": ["check", "--write"], "owner": "install.sh"},
    {"event": "PreCompact", "script": "gt_state.py",
     "args": ["hook"], "owner": "install.sh"},
    # THE READER for everything above (0.17.2). gt_state, gt_handoff and gt-work's handoff
    # tasks all wrote for "the next session" and nothing at SessionStart read any of them.
    # SessionStart also fires after a compaction (`source: compact`), which is when the state
    # file written by the two entries above is handed back to the model in full.
    {"event": "SessionStart", "script": "gt_surface.py",
     "args": ["check", "--hook"], "owner": "install.sh"},
    # gt_watch.py (SessionStart) and gt_report_card.py (PreCompact, SessionEnd, SessionStart
    # surface) were registered here until 0.14.0. Since 0.15.0 the watch and report-card
    # modules declare them in module.json; module_registrations() appends them, tagged.
    # Protected paths: Write/Edit to core-rules/, global-memory/, the gt hooks dir or
    # settings.json forces the permission prompt; overwriting an existing Sources/ file
    # is denied. Not tied to a Core rule, and needs no vault to be wired, so install.sh
    # owns it rather than install-core-rules (0.12.9).
    {"event": "PreToolUse", "script": "guard_protected_paths.sh", "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
     "timeout": 10, "onFailure": "block", "args": [], "owner": "install.sh"},
    # Foreign checkouts (0.18.1): deny `git commit` / `git push` inside a checkout the user
    # DECLARED as another machine's (`foreign_checkouts` in vault-config.json). Inert with
    # nothing declared; not tied to a Core rule and needs no vault, so install.sh owns it,
    # like guard_protected_paths. Switch: the `foreign_checkout_guard` setting.
    {"event": "PreToolUse", "script": "guard_foreign_checkout.sh", "matcher": "Bash",
     "if": "Bash(*git*)", "timeout": 10, "onFailure": "block", "args": [], "owner": "install.sh"},
    # Scope contract (0.21.4): a `git commit` the model types in a Bash call is checked against the
    # approved plan's `scope:` block, as /gt:gt-allin-commit already does for its own commit. Its own
    # guard, not a clause of guard_test_before_commit (the test gate's early exits and escapes are
    # wrong for it, and its denials are recorded as uses of a Core rule); install.sh owns it like the
    # other non-Core guards, so it needs no vault. `matcher: Bash` starts no process for Read/Edit.
    # Inert with no approved plan for the branch; `scope_gate` off|warn|block decides what it does
    # with one; fails open, and says so only when a plan exists.
    {"event": "PreToolUse", "script": "guard_scope_contract.sh", "matcher": "Bash",
     "if": "Bash(*git*)", "timeout": 10, "args": [], "owner": "install.sh"},
    # gt-lotr onboarding (0.22.0): an agent made by `lotr onboard agent` may run only its one script. An agent
    # file cannot confine its own Bash (measured), so this hook does, keyed on `agent_type` starting `lotr-`.
    # Silent for every other agent and the main session; fails closed for the lotr- agents only. Inert with
    # no such agent, and it needs no vault.
    {"event": "PreToolUse", "script": "guard_lotr_agent.py", "matcher": "Bash", "timeout": 5, "args": [],
     "owner": "install.sh"},
    # Knowledge read log (0.18.1): after a Read of <vault>/Knowledge/**.md, one JSON line to
    # <vault>/usage/knowledge.jsonl, so gt_optimize can report pages nobody reads. A hook, not
    # skill prose: prose logs only the reads the model remembered to log. Governed by the
    # `knowledge_access_log` setting; fails open and silent; a 5 s budget it never needs.
    {"event": "PostToolUse", "script": "log_knowledge_read.sh", "matcher": "Read",
     "timeout": 5, "args": [], "owner": "install.sh"},
    # Prompt-relevant vault hints (0.19.1): up to three index.md titles that share words with
    # the prompt, never a page body. OFF by default (`vault_hints` setting) -- registered always,
    # silent and instant when off; a 3 s budget it never needs (its own is 0.8 s).
    {"event": "UserPromptSubmit", "script": "vault_hints.py", "timeout": 3, "args": [],
     "owner": "install.sh"},
    # gt unlock (0.20.1): register the session with the unlock authority at start, revoke its
    # grant at end (the authority also watches the claude process and the MCP shim exit).
    # When unlock is off (the default) both return at once, print nothing and start nothing.
    {"event": "SessionStart", "script": "gt_unlock.py", "args": ["hook", "session-start"],
     "owner": "install.sh"},
    {"event": "SessionEnd", "script": "gt_unlock.py", "args": ["hook", "session-end"],
     "owner": "install.sh"},
    # Timed grants (0.21.0, D1). A grant is a permission rule gt wrote with an expiry; these four
    # entries end it and report on it. NONE of them can allow anything: the sweep and the spend
    # only remove rules, the gate can only make Claude Code ask again, and PermissionDenied only
    # tells the user the cure. With no grant on the machine each returns before importing anything.
    {"event": "SessionStart", "script": "gt_grant.py", "args": ["sweep", "--hook"],
     "owner": "install.sh"},
    {"event": "PreToolUse", "script": "gt_grant.py", "matcher": "Bash", "timeout": 5,
     "args": ["gate", "--hook"], "owner": "install.sh"},
    {"event": "PostToolUse", "script": "gt_grant.py", "matcher": "Bash", "timeout": 5,
     "args": ["spend", "--hook"], "owner": "install.sh"},
    {"event": "PermissionDenied", "script": "gt_grant.py", "timeout": 5,
     "args": ["denied", "--hook"], "owner": "install.sh"},
    # Cache-expiry guard (0.21.0). idle_prompt fires ~60 s after Claude finishes with no typing:
    # arm ONE one-shot job to cut the session while its prompt cache is still warm. The
    # UserPromptSubmit entry cancels it on any prompt and, on a return to a large expired session
    # the guard could not cut, blocks the first prompt once (a blocked prompt costs nothing).
    # `expiry_guard` ships warn-only and off returns at once; both fail open and print nothing.
    {"event": "Notification", "script": "gt_expiry_guard.py", "matcher": "idle_prompt",
     "timeout": 10, "args": ["arm", "--hook"], "owner": "install.sh"},
    {"event": "UserPromptSubmit", "script": "gt_expiry_guard.py", "timeout": 5,
     "args": ["backstop", "--hook"], "owner": "install.sh"},
    # The same arm on Stop (0.21.2): idle_prompt is a one-shot 60 s timer that Claude Code silently
    # drops when the terminal was touched, a dialog is up or background work runs, so a session that
    # was armed only there could go unarmed for hours (2026-10-12: 295K tokens, nothing logged).
    # Stop fires at the end of every reply; the arm is idempotent per idle period.
    {"event": "Stop", "script": "gt_expiry_guard.py", "timeout": 10,
     "args": ["arm", "--hook"], "owner": "install.sh"},
    # Installed-code integrity (0.21.0, `paranoid.bash_guard`): ask when a shell command writes to
    # gt's installed code or tries to clear its lock. Registered always, inert unless the part is
    # on (it reads one small config file and prints nothing); it can only ASK or DENY, never allow.
    # Install guard (0.22.0, tool vetting A3): before a Bash/PowerShell command installs something
    # (git clone, npx/uvx, pip/npm install, claude mcp add, a download piped to a shell), fetch it into
    # a hardened temp dir without running it and scan it with gt_intake_scan. `install_guard`
    # off|warn|block (default warn); clean prints nothing; its own 50 s budget answers "ask" before
    # the 75 s hook timeout, and onFailure blocks if the hook itself cannot finish.
    {"event": "PreToolUse", "script": "guard_install.sh", "matcher": "Bash|PowerShell",
     "timeout": 75, "onFailure": "block", "args": [], "owner": "install.sh"},
    {"event": "PreToolUse", "script": "gt_integrity.py", "matcher": "Bash", "timeout": 5,
     "args": ["bash-guard", "--hook"], "owner": "install.sh"},
    # Next-step and prompt hints (0.21.0, item #34). Both print ONLY a `systemMessage` -- shown to
    # the user, never added to the model's context, so they cost no tokens -- and never block.
    # `next_step_hints` / `prompt_hints` (both on by default) turn each off; a run past its budget
    # or any error shows nothing. Stop fires after each reply; the prompt hook has a 0.2 s budget.
    {"event": "Stop", "script": "gt_hints.py", "timeout": 5, "args": ["next", "--hook"],
     "owner": "install.sh"},
    # Cut advisor (0.22.0, `cut_advisor` off | suggest, default off). After a reply, reads the tail of the
    # transcript and looks the session's size and pace up in a measured table; prints ONE `systemMessage` to
    # the user when a cut would pay back. No model call, suggest-only, fails open.
    {"event": "Stop", "script": "gt_cut_advisor.py", "timeout": 5, "args": ["stop", "--hook"],
     "owner": "install.sh"},
    # Auto-clear policy (0.22.0, phase 143, `auto_clear` off | shadow, default off). Shadow mode only: after a
    # reply, scores the "clear now?" rule on the session's context sizes and appends one line to a decision
    # log so regret can be scored later. Prints nothing, never clears, no model call, fails open.
    {"event": "Stop", "script": "gt_auto_clear.py", "timeout": 5, "args": ["stop", "--hook"],
     "owner": "install.sh"},
    # Batch-size ledger (0.22.0, GAP-1 follow-up). PostToolBatch fires once after every call in a batch has
    # resolved; the hook appends one line (the batch size and the tool NAMES, never an input or a response) to a
    # local file. It MEASURES Core rule 8 and cannot enforce it: the batch has already run. Prints nothing,
    # no model call, fails open.
    {"event": "PostToolBatch", "script": "gt_batch_ledger.py", "timeout": 5, "args": ["record", "--hook"],
     "owner": "install.sh"},
    # Queue auto-drain (0.22.0, `queue_autodrain`, default on): at the end of a turn and at session
    # start, drain the write queue through the broker, which decides exactly as a manual drain does.
    # Silent when nothing is queued; never fails the hook.
    {"event": "Stop", "script": "gt_queue_autodrain.py", "timeout": 30, "args": ["stop", "--hook"],
     "owner": "install.sh"},
    {"event": "SessionStart", "script": "gt_queue_autodrain.py", "timeout": 30,
     "args": ["session-start", "--hook"], "owner": "install.sh"},
    {"event": "UserPromptSubmit", "script": "gt_hints.py", "timeout": 3,
     "args": ["prompt", "--hook"], "owner": "install.sh"},
    # Scheduled model calibration (0.21.0, item 44). With model_calibration_schedule weekly or monthly
    # and a run due (counted from the last run of any kind), start ONE background pass at session
    # start -- a missed run fires once, never a backlog. manual (the default) and off return at once
    # and print nothing; a compaction start is ignored; it fails open.
    {"event": "SessionStart", "script": "gt_calibration_schedule.py", "timeout": 5,
     "args": ["check", "--hook"], "owner": "install.sh"},
    # Skill run log (0.21.4, `skill_runlog`, off by default). Reads the session transcripts that changed
    # since last time and appends one record per gt skill run (model, effort, tokens, wall clock, outcome,
    # a redacted and intake-scanned digest of the task) so a skill's model and effort can be calibrated.
    # A catch-up at the START of a session, not a hook on the skill call: the transcript holds the whole
    # run, and a session killed without a SessionEnd still contributes at the next start. Off returns
    # before opening a transcript; it prints nothing and always exits 0.
    {"event": "SessionStart", "script": "gt_skill_runlog.py", "timeout": 10,
     "args": ["catch-up", "--hook"], "owner": "install.sh"},
    # Session search (0.22.0, `session_search`, off by default). Indexes the text of the session transcripts
    # that changed since last time, so "what did we decide last week?" can be searched. Off returns before
    # opening a transcript; it prints nothing, takes 8 seconds at most, and always exits 0.
    {"event": "SessionStart", "script": "gt_session_search.py", "timeout": 10,
     "args": ["catch-up", "--hook"], "owner": "install.sh"},
    # Add-on scan (0.22.0, `addon_scan`, on by default). Looks at a skill, plugin or CLAUDE.md the first
    # time it appears or when it changes, on the island scan (no network, no writes); a finding is
    # reported once. It reads and reports, never edits; over three new folders it says "run
    # /gt:gt-doctor"; a scan that cannot run says "not scanned". Prints nothing when clean; exits 0.
    {"event": "SessionStart", "script": "gt_addon_scan.py", "timeout": 10,
     "args": ["scan", "--hook"], "owner": "install.sh"},
    # MCP tool-description pin (0.22.0, `mcp_pin`, warn by default). Reads the tool definitions Claude Code
    # recorded in the session transcripts (it never runs or asks a server), hashes each MCP tool's
    # description and schema, and reports once a server first seen, a tool added or a description
    # changed; new text goes through the island scan. Warn only; prints nothing when quiet; exits 0.
    {"event": "SessionStart", "script": "gt_mcp_pin.py", "timeout": 10,
     "args": ["check", "--hook"], "owner": "install.sh"},
    # Ad-hoc agent models (0.21.0). gt's own agents carry a model and effort in their definitions;
    # an ad-hoc spawn (general-purpose, Explore, Plan, claude) carries none, so it ran on whatever
    # model the session was on. This sets one by task through `updatedInput` when the spawn names
    # no model (`adhoc_agent_model` task, the default), or refuses it (`require`). It never
    # touches a spawn that names a model, a plugin's own agent or a fork; it fails open, silently.
    {"event": "PreToolUse", "script": "gt_agent_model.py", "matcher": "Agent|Task", "timeout": 5,
     "args": ["--hook"], "owner": "install.sh"},
    # Keeping the main conversation small (0.21.4). Both are registered always and inert until their
    # setting is on; both fail open, print nothing on any error and exit 0.
    #   gt_spill.py hook (PostToolUse on Bash): with spill_threshold_lines a number, a successful
    #     command whose output is longer is written to an owner-only file and Claude sees the head, the
    #     tail, the line count and the path instead (updatedToolOutput, Claude Code 2.1.121+). `off`
    #     (the default) returns before reading the output.
    #   gt_intake.py hook (UserPromptSubmit): with paste_intake ask or auto, a prompt longer than
    #     paste_intake_chars is saved to an owner-only file and BLOCKED with a one-line reason, so the
    #     paste never reaches the conversation; /gt:gt-digest hands the file to a subagent or a local
    #     engine. `off` (the default) returns before reading the prompt.
    {"event": "PostToolUse", "script": "gt_spill.py", "matcher": "Bash", "timeout": 5,
     "args": ["hook"], "owner": "install.sh"},
    {"event": "UserPromptSubmit", "script": "gt_intake.py", "timeout": 10, "args": ["hook"],
     "owner": "install.sh"},
    # -- enforcement: registered by vault_init.py install-core-rules ---------
    {"event": "UserPromptSubmit", "script": "inject_core_rules.sh", "timeout": 10,
     "args": [], "owner": "vault_init.py install-core-rules"},
    # THE SAME SCRIPT ON A SECOND EVENT, because compaction does not preserve what a hook
    # added. The docs are explicit: project-root CLAUDE.md and auto memory are "Re-injected
    # from disk", but "Context that hooks added earlier" is "Summarized with the rest of the
    # conversation" -- so after a compaction the Core rules survive only as whatever the
    # summariser chose to keep, which is not the same as being asserted.
    #
    # The window is narrow and was measured before this was added: UserPromptSubmit has no
    # matcher and no compaction exception, so the very next user prompt restores the rules
    # verbatim. What is exposed is the REMAINDER OF THE TURN in which an auto-compaction
    # fired -- however many tool calls run before the user speaks again. Narrow is not the
    # same as closed, and this is the one seam where a rule is present in degraded form while
    # everything reads as healthy: precisely the shape of the 2026-08-17 Dropbox incident
    # this script's own header describes.
    #
    # SessionStart/compact rather than PostCompact: both work, and the hooks guide names this
    # one for this purpose. The mechanical tier (PreToolUse guards, the Stop validator) is
    # unaffected either way -- those are event-driven commands, not context, so compaction
    # cannot weaken them. What is being restored here is the re-assertion, not the backstop.
    {"event": "SessionStart", "script": "inject_core_rules.sh", "matcher": "compact", "timeout": 10,
     "args": [], "owner": "vault_init.py install-core-rules"},
    {"event": "Stop", "script": "validate_response.sh", "timeout": 10,
     "args": [], "owner": "vault_init.py install-core-rules"},
    {"event": "PreToolUse", "script": "guard_session_claims.sh",
     "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell", "timeout": 10, "onFailure": "block",
     "args": [], "owner": "vault_init.py install-core-rules"},
    # A SECOND PreToolUse entry, deliberately separate from the first: one inspects
    # Write/Edit targets, this one Bash command lines. Folding them together would
    # make each blind to the other's tool set, and this event now carries two.
    # A THIRD PreToolUse entry. Same reasoning as the second: one guard inspects
    # Write/Edit targets, one inspects vault-tool command lines, and this one inspects
    # `git commit`. Three narrow guards that each fail open beat one guard that has to
    # understand everything.
    {"event": "PreToolUse", "script": "guard_test_before_commit.sh", "matcher": "Bash",
     "if": "Bash(*git*)", "timeout": 10, "onFailure": "block", "args": [],
     "owner": "vault_init.py install-core-rules"},
    {"event": "PreToolUse", "script": "guard_vault_writes.sh", "matcher": "Bash|PowerShell",
     "timeout": 10, "onFailure": "block", "args": [], "owner": "vault_init.py install-core-rules"},
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
               "requires_modules", "replaces_core", "demo", "checkers", "lockdown")
MODULE_REQUIRED = ("schema", "name", "plugin", "version", "requires_gt", "summary", "default")
MODULE_LISTS = ("skills", "scripts", "templates", "hooks", "hookdir_scripts", "settings",
                "requires_modules", "replaces_core", "checkers")
# Validation checkers (0.18.1): a module contributes deterministic checks to the core
# validation host, gt_check.py. Every key is closed; `fixes` is the grant under which the
# checker may return fix proposals (gt_apply.py writes them, never the checker).
CHECKER_KEYS = ("id", "script", "globs", "mime", "events", "requires_tools", "timeout",
                "fixes", "rules", "summary")
CHECKER_EVENTS = ("pre-commit", "commit-msg")
CHECKER_RULESETS = ("html",)
# A fix grant may never name these, whatever the module says; gt_apply.py refuses them again
# at write time, so a grant that slips past here still cannot write there.
# Lockdown rules (0.21.0): a module declares what Claude may run without asking at each level,
# {"<level>": {"allow": [...], "deny": [...]}}. The shape is checked here; the content of each
# rule (well-formed, never loosening a core deny, never a consent op) is judged by gt_lockdown.py,
# which reports a bad rule by module name and ignores it rather than switching the module off.
LOCKDOWN_LEVELS = ("strict", "trust-local", "trust-network", "insecure")
LOCKDOWN_KEYS = ("allow", "deny")
PROTECTED_FIX_SEGMENTS = ("core-rules", "global-memory", "Sources", ".githooks", ".git",
                          ".claude")
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
    seen_ids = set()
    for i, c in enumerate(lst("checkers")):
        if not isinstance(c, dict):
            reasons.append("checkers[%d] is not an object" % i)
            continue
        for k in sorted(set(c) - set(CHECKER_KEYS)):
            reasons.append("checkers[%d] unknown key %r" % (i, k))
        for k in ("id", "script"):
            if k not in c:
                reasons.append("checkers[%d] missing %r" % (i, k))
        cid = c.get("id")
        if "id" in c:
            if not (isinstance(cid, str) and re.match(_MODULE_NAME_RE, cid)):
                reasons.append("checkers[%d] id %r does not match %s" % (i, cid, _MODULE_NAME_RE))
            elif cid in seen_ids:
                reasons.append("checkers[%d] id %r is declared twice" % (i, cid))
            else:
                seen_ids.add(cid)
        s = c.get("script")
        if "script" in c:
            if not _plain_entry(s):
                reasons.append("checkers[%d] script %r is not a plain name" % (i, s))
            elif not os.path.isfile(os.path.join(version_dir, "scripts", s)):
                reasons.append("checkers[%d] script %r does not exist under scripts/" % (i, s))
        for k in ("globs", "mime", "events", "requires_tools", "fixes", "rules"):
            if k in c and not (isinstance(c[k], list)
                               and all(isinstance(x, str) and x for x in c[k])):
                reasons.append("checkers[%d] %s must be a list of non-empty strings" % (i, k))
        if not any(c.get(k) for k in ("globs", "mime", "events")):
            reasons.append("checkers[%d] applies to nothing: give globs, mime or events" % i)
        for e in c.get("events") or [] if isinstance(c.get("events"), list) else []:
            if e not in CHECKER_EVENTS:
                reasons.append("checkers[%d] event %r is not one of %s"
                               % (i, e, ", ".join(CHECKER_EVENTS)))
        for r in c.get("rules") or [] if isinstance(c.get("rules"), list) else []:
            if r not in CHECKER_RULESETS:
                reasons.append("checkers[%d] rule set %r is not one of %s"
                               % (i, r, ", ".join(CHECKER_RULESETS)))
        for t in c.get("requires_tools") or [] if isinstance(c.get("requires_tools"), list) else []:
            if not _plain_entry(t):
                reasons.append("checkers[%d] required tool %r is not a plain name" % (i, t))
        for g in c.get("fixes") or [] if isinstance(c.get("fixes"), list) else []:
            if not _rel_entry(g):
                reasons.append("checkers[%d] fixes %r is not a relative glob" % (i, g))
            elif any(seg in PROTECTED_FIX_SEGMENTS for seg in g.split("/")):
                reasons.append("checkers[%d] fixes %r names a protected path; fixes there are "
                               "never granted" % (i, g))
        if "timeout" in c and not (isinstance(c["timeout"], int)
                                   and not isinstance(c["timeout"], bool)
                                   and 1 <= c["timeout"] <= 600):
            reasons.append("checkers[%d] timeout %r is not a whole number of seconds in 1..600"
                           % (i, c["timeout"]))
        if "summary" in c and not isinstance(c["summary"], str):
            reasons.append("checkers[%d] summary must be a string" % i)
    if "lockdown" in data:
        ld = data["lockdown"]
        if not isinstance(ld, dict):
            reasons.append("lockdown must be an object keyed by level")
        else:
            for lvl in sorted(ld, key=str):
                if lvl not in LOCKDOWN_LEVELS:
                    reasons.append("lockdown level %r is not one of %s"
                                   % (lvl, ", ".join(LOCKDOWN_LEVELS)))
                    continue
                blk = ld[lvl]
                if not isinstance(blk, dict):
                    reasons.append("lockdown[%s] must be an object with allow and deny lists"
                                   % lvl)
                    continue
                for k in sorted(set(blk) - set(LOCKDOWN_KEYS)):
                    reasons.append("lockdown[%s] unknown key %r" % (lvl, k))
                for k in LOCKDOWN_KEYS:
                    if k in blk and not isinstance(blk[k], list):
                        reasons.append("lockdown[%s].%s must be a list of rule strings"
                                       % (lvl, k))
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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def newest_gt_version(plugin_root):
    vd = _newest_release(os.path.join(plugin_root or "", "golden-thread"))
    return os.path.basename(vd) if vd else None


# Modules that cannot run on native Windows, by design rather than by defect (0.20.1). LOTR is a
# gateway served on a Unix-domain socket whose clients are authenticated by peer uid and whose
# secrets are protected by 0600 permission bits -- none of which Windows has. On Windows such a
# module is OFF whatever was chosen, said in words, and the recorded choice is left alone.
POSIX_ONLY_MODULES = {
    "lotr": "a Unix-domain-socket gateway authenticated by peer uid and 0600 files",
}
# ...UNTIL the version named here (0.20.1): gt-lotr 0.3.0 serves its local front door on a named
# pipe on Windows (gt_ipc: a user-SID-only DACL, remote clients rejected, first instance only,
# the client's process and SID read from the kernel), so from 0.3.0 it installs there too.
POSIX_ONLY_BELOW = {"lotr": "0.3.0"}


def posix_only(name, version):
    """True when module `name` at `version` cannot run on native Windows."""
    if name not in POSIX_ONLY_MODULES:
        return False
    below = POSIX_ONLY_BELOW.get(name)
    if not below or not version:
        return True
    try:
        return tuple(int(x) for x in str(version).split(".")) < \
            tuple(int(x) for x in below.split("."))
    except ValueError:
        return True


def _native_windows():
    return os.name == "nt"


# Modules that need a minimum Claude Code (0.21.2). The gt pane draws with Claude Code mods,
# which work from 2.1.290 (owner, 2026-10-15; was 2.1.296 from 2026-10-12). Below it, or when the version cannot be read,
# such a module is OFF whatever was chosen, its install question is not asked, and the recorded
# choice is left alone -- so the next install after Claude Code is upgraded turns it back on.
MODULE_MIN_CC = {"pane": (2, 1, 290)}
_CC_SEEN = []


def module_cc_gap(name):
    """-> None when `name` runs on this machine's Claude Code, else the reason it does not."""
    need = MODULE_MIN_CC.get(name)
    if not need:
        return None
    if not _CC_SEEN:
        _CC_SEEN.append(claude_code_version())
    have, want = _CC_SEEN[0], ".".join(str(x) for x in need)
    if have is None:
        return "needs Claude Code %s or newer; this machine's version could not be read" % want
    if have < need:
        return "needs Claude Code %s or newer; this machine has %s" % (
            want, ".".join(str(x) for x in have))
    return None


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
        chose_on = state == "on" and (n in with_ or choices.get(n) == "on")
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
        elif state == "on" and posix_only(n, d.get("version")) and _native_windows():
            state, reason = "off", ("POSIX-only (%s) -- skipped on Windows"
                                    % POSIX_ONLY_MODULES[n])
        elif state == "on" and module_cc_gap(n):
            state, reason = "off", module_cc_gap(n) + " -- skipped"
        elif reason == "module default" and module_cc_gap(n):
            reason = module_cc_gap(n) + " -- skipped"     # off anyway; say why it is not offered
        out[n] = {"state": state, "reason": reason, "version": d.get("version"),
                  "plugin": d.get("plugin"), "default": default,
                  "choice": choices.get(n), "requires_gt": d.get("requires_gt"),
                  "admitted": admitted, "valid": not m["reasons"],
                  "reasons": m["reasons"], "version_dir": m["version_dir"],
                  # 0.21.4: set when the owner chose this module ON and it is off for want of a
                  # new enough / readable Claude Code, so the doctor can say so instead of "by choice".
                  "cc_gap": (module_cc_gap(n) if chose_on and state == "off" else None)}
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
    # Since 0.21.0 the registration may be a wired file (the plugin route) rather than a
    # settings.json block; effective_hook_events reads both.
    events = effective_hook_events(home=home)
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
            row = {"event": h["event"], "script": h["script"],
                   "args": list(h.get("args") or []), "owner": "install.sh",
                   "module": m["name"], "src": m["version_dir"], "state": st}
            if "timeout" in h:
                row["timeout"] = h["timeout"]
            out.append(row)
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


def hook_python(home=None):
    """-> the interpreter part of a settings.json hook command, as a list.

    ["python3"] everywhere but native Windows, where Claude Code runs hook commands through
    Git Bash and `python3` is the Microsoft Store stub ("Python was not found", exit 9009):
    every .py hook would fail open, silently. There the command names the interpreter
    running this -- the one install.sh resolved and checked -- by absolute path, in UTF-8
    mode (`-X utf8`): otherwise its stdout is cp1252 and the first "⚠" a hook prints is a
    UnicodeEncodeError instead of a message."""
    if os.name != "nt" or not sys.executable:
        rec = _recorded_mac_python(home) if sys.platform == "darwin" else None
        return [rec] if rec else ["python3"]
    return [_cmd_path(sys.executable), "-X", "utf8"]


def _recorded_mac_python(home=None):
    """macOS (0.20.1): the interpreter install.sh recorded for hooks, tools and jobs -- the one
    that passed gt's write probe (gt_schedule.py choose-interpreter) -- read from the file the
    hook wrappers read (~/.claude/golden-thread/python). macOS can refuse Homebrew's python3
    writes it allows /usr/bin/python3 (com.apple.provenance; the vault's log.md, 2026-10-03), so
    a .py hook command names the recorded one. None when there is no usable record: `python3`.
    A recorded interpreter that later disappears is reported, not silent: check_wiring flags
    a command whose interpreter path no longer exists as `badpath`."""
    p = (os.path.join(home, ".claude", "golden-thread", "python") if home
         else os.path.join(os.path.dirname(INSTALLED_HOOKS), "python"))
    try:
        with open(p, encoding="utf-8") as fh:
            py = fh.readline().strip()
    except OSError:
        return None
    return py if py and os.path.isabs(py) and os.path.isfile(py) and os.access(py, os.X_OK) \
        else None


def _cmd_path(p):
    """A path as a hook COMMAND carries it: "/"-separated on Windows, untouched elsewhere.

    expanduser() there gives "C:\\Users\\x/.claude/..." -- mixed separators -- and Git Bash
    reads the command, where a backslash outside quotes is an escape character. Windows
    accepts "/" in every path, so forward slashes are the one form both sides agree on."""
    return p.replace("\\", "/") if os.name == "nt" else p


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
        target = _cmd_path(os.path.join(INSTALLED_HOOKS, reg["script"]))
        argv = [_cmd_path(a.format(**subs)) for a in reg["args"]]
        # Shell scripts execute directly; .py files are run through python3 so the
        # entry does not depend on the file keeping its +x bit through a sync.
        parts = ([target] if reg["script"].endswith(".sh")
                 else hook_python(home) + ["-B", target]) + argv
        # Quote every part: the plugin source lives under a path containing a space
        # ("Golden Thread"), and an unquoted argument split on it so the checker was
        # handed "Golden" and reported a bogus no-manifest drift on every start.
        row = {"event": reg["event"], "script": reg["script"], "owner": reg["owner"],
               "command": " ".join(shlex.quote(x) for x in parts)}
        if "module" in reg:
            row["module"] = reg["module"]
        # Carried through to whoever writes settings.json. Both are optional and both change
        # the ENTRY rather than the command, so a wirer that ignores them still produces a
        # working hook -- just one that fires on every source (no matcher) or under the
        # event's default budget (no timeout).
        for k in ("matcher", "if", "timeout"):
            if k in reg:
                row[k] = reg[k]
        out.append(row)
    return out


# --- plugin-shipped hooks (0.21.0) ---------------------------------------------------------
#
# Until 0.21.0 every gt hook was a block install.sh (and vault_init) wrote into
# ~/.claude/settings.json -- a file the user and Claude Code also write, so gt had to merge
# into it, order its blocks among the user's, prune what an older release left, and back it
# up first. Each of those was a bug at least once.
#
# Claude Code loads a plugin's `hooks/hooks.json` while the plugin is enabled
# (code.claude.com/docs/en/plugins-reference, "hooks": "Loaded together with
# hooks/hooks.json"; standard layout "Hooks | hooks/hooks.json"), merging its handlers with the
# settings ones ("When a plugin is enabled, its hooks merge with your user and project hooks.
# All matching hooks run in parallel." -- code.claude.com/docs/en/hooks). So since 0.21.0 the
# gt plugin, and each module plugin that declares hooks, SHIPS its hooks.json.
#
# A shipped file is the same on every machine, but a gt hook command is not: it names the
# interpreter install.sh resolved (native Windows), the source version dir ({src}) and the
# plugin source root ({plugin_root}). So each shipped entry is a fixed one-line dispatcher
# that SOURCES a per-machine file in ~/.claude/golden-thread/wired/ -- written by install.sh
# (and vault_init for the enforcement hooks), gt's own directory, which nothing else writes:
#
#     f="${HOME:-$USERPROFILE}/.claude/golden-thread/wired/<event>__<script>"
#     if [ -f "$f" ]; then . "$f"; fi
#
# and the wired file is `exec <the exact command settings.json used to carry>`. Sourcing runs
# in the shell Claude Code already started, and `exec` replaces it, so no extra process runs.
# An absent wired file is a silent no-op, which is what keeps three properties:
#   * the enforcement hooks stay unwired until a vault exists (vault_init writes them) -- the
#     injector would otherwise print its "vault unreachable" banner on every turn;
#   * on the settings route (below) the plugin's entries do nothing, so nothing runs twice
#     (the docs: "A plugin's or skill's copy of the same handler stays separate" -- a handler
#     in both places runs twice);
#   * a module switched off has no wired files, even if its plugin is still enabled.
#
# THE SETTINGS ROUTE STAYS for a Claude Code that predates reliable plugin hooks: plugins
# (with hooks) arrived in 2.0.12, but plugin hooks failed with "No such file or directory"
# when CLAUDE_PLUGIN_ROOT was unset until 2.1.94, and plugin Stop/UserPromptSubmit hooks --
# the Stop validator and the Core-rule injector -- failed when cache cleanup deleted a version
# a running session still used until 2.1.136 (Claude Code CHANGELOG). Below 2.1.136, or when
# the version cannot be read, install.sh wires settings.json exactly as 0.20.0 did.
WIRED_DIRNAME = "wired"
# Claude Code mods gt itself ships in hooks/ (0.21.2): the expiry guard's in-session half. Claude
# Code 2.1.267+ loads them from hooks.json's `modules`; older versions ignore the key and still run
# every hook in the file (checked on 2.1.136, 2.1.240 and 2.1.292).
GT_MODS = ("./guard_mod.js",)
PLUGIN_HOOKS_MIN_CC = (2, 1, 136)
HOOK_MODES = ("plugin", "settings")
WIRED_HEADER = "# gt-wired "


def wired_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".claude", "golden-thread",
                        WIRED_DIRNAME)


def wired_name(event, script):
    """The wired file for one (event, script) registration. Both are plain names (an event
    is a Claude Code identifier; a script a file name in the hooks dir), so the pair is a
    safe single path component."""
    name = "%s__%s" % (event, script)
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("unsafe wired name: %r" % name)
    return name


def plugin_hook_command(event, script):
    """The shell-form command a shipped hooks.json carries for one registration."""
    # ${HOME:-$USERPROFILE}: Git Bash on native Windows normally sets HOME, but a hook that
    # silently found nothing because it did not would be every gt hook dead at once.
    return ('f="${HOME:-$USERPROFILE}/.claude/golden-thread/%s/%s"; '
            'if [ -f "$f" ]; then . "$f"; fi' % (WIRED_DIRNAME, wired_name(event, script)))


# Not a registration: no wired file stands behind it (see plugin_hooks_for). ${CLAUDE_PLUGIN_ROOT}
# is the installed gt plugin's own directory, expanded by Claude Code.
FIRST_RUN_HOOK = {
    "type": "command",
    # Before setup there is no recorded interpreter. On Windows `python3` is often only the Store
    # stub, so the py launcher is tried first there, then python3, then python (a Windows test machine, 2026-10-13).
    "command": ('[ -f "${HOME:-$USERPROFILE}/.claude/golden-thread/hooks/gt_paths.py" ] || '
                '{ s="${CLAUDE_PLUGIN_ROOT}/scripts/gt_setup.py"; '
                '{ command -v py >/dev/null 2>&1 && py -3 -I "$s" hook; } || python3 -I "$s" hook '
                '2>/dev/null || python -I "$s" hook; }'),
    "timeout": 5,
}


def plugin_hooks_doc(regs, description):
    """-> the hooks.json document for `regs` (dicts with event, script, owner and optional
    matcher/timeout), one matcher group per registration, in registration order.

    Timeouts: a registration's own, else Claude Code's default. (Until 0.22.0 the enforcement hooks got a
    silent 10 s here; their registrations now state it, so a missing one is a test failure, not a default.)"""
    events = {}
    for r in regs:
        h = {"type": "command", "command": plugin_hook_command(r["event"], r["script"])}
        t = r.get("timeout")
        if t is not None:
            h["timeout"] = t
        # `if` (0.22.0) is a HANDLER field: a permission rule such as "Bash(*git*)" that Claude Code
        # checks before spawning the hook, so a guard that only acts on git commands starts no
        # process for the other ~85% of Bash calls. Measured 2026-10-17 on Claude Code 2.1.302;
        # an unknown handler key was ignored there too, so a build without `if` still runs the guard (which re-checks for
        # itself) still runs there -- the filter saves work, it never carries the decision.
        if r.get("if"):
            h["if"] = r["if"]
        # `onFailure: "block"` (0.22.0, Claude Code 2.1.295+): a guard that times out or cannot
        # start BLOCKS the tool instead of letting it run. Claude Code also blocks on any exit
        # other than 0 or 2, so it is set only on the shell guards, whose wrappers end
        # `|| true; exit 0` -- never on a bare `exec python3` entry (gt_grant, gt_integrity),
        # where an import error would block every Bash call. Proven on 2.1.302 (2026-10-17);
        # an unknown handler key is ignored there, so an older build keeps failing open.
        if r.get("onFailure"):
            h["onFailure"] = r["onFailure"]
        block = {"hooks": [h]}
        if r.get("matcher") is not None:
            block["matcher"] = r["matcher"]
        events.setdefault(r["event"], []).append(block)
    return {"description": description, "hooks": events}


def plugin_hooks_for(version_dir):
    """The hooks.json a release directory should ship: gt's HOOK_REGISTRATIONS for the gt
    plugin, a module's module.json `hooks` for a module. None when it declares no hooks."""
    try:
        with open(os.path.join(version_dir, MODULE_FILE), encoding="utf-8") as fh:
            mod = json.load(fh)
    except (OSError, ValueError):
        mod = None
    if isinstance(mod, dict):
        regs = [dict(h, owner="install.sh") for h in mod.get("hooks") or []]
        if not regs:
            return None
        return plugin_hooks_doc(regs, "gt module %s: each entry runs the command gt's installer "
                                      "wired for this machine, when it wired one"
                                % mod.get("name", "?"))
    doc = plugin_hooks_doc(HOOK_REGISTRATIONS,
                           "Golden Thread (gt): each entry runs the command gt's installer "
                           "wired for this machine, when it wired one")
    doc["modules"] = list(GT_MODS)
    # The one entry that does NOT go through a wired file (0.21.3): before the installer has run
    # there is nothing wired, so the dispatchers above are all silent -- and that is exactly when a
    # person who installed from the Claude prompt needs telling. It costs one `test -f` once set up.
    doc["hooks"].setdefault("SessionStart", []).insert(0, {"hooks": [FIRST_RUN_HOOK]})
    return doc


def _windows_profile_dir():
    """The account's profile folder from the OS, not from USERPROFILE. None off Windows."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(520)
        # CSIDL_PROFILE = 0x28; SHGFP_TYPE_CURRENT = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x28, None, 0, buf) == 0:
            return buf.value or None
    except Exception:                                       # noqa: BLE001 -- best effort
        pass
    return None


def claude_code_version():
    """-> (major, minor, patch) of the `claude` on PATH, or None. GT_CLAUDE_VERSION overrides
    (tests; a machine whose Claude Code is not the CLI on PATH). Never raises."""
    raw = os.environ.get("GT_CLAUDE_VERSION")
    if raw is None:
        try:
            import shutil
            import subprocess
            exe = shutil.which("claude")      # PATHEXT on Windows: claude.exe or claude.cmd
            if not exe:
                # The native installer puts claude in ~/.local/bin and, on Windows, does not add it
                # to PATH (a Windows test machine, 2026-10-13: the pane was skipped as "version could not be read").
                # A test or a wrapper may point HOME / USERPROFILE at a temp dir; on Windows the
                # account's real profile (SHGetFolderPath, which ignores the environment) is
                # probed too, so the pane is not dropped for a version that is right there.
                homes = [os.environ.get("USERPROFILE") or os.path.expanduser("~")]
                real = _windows_profile_dir()
                if real and real not in homes:
                    homes.append(real)
                for home in homes:
                    for name in ("claude.exe", "claude"):
                        cand = os.path.join(home, ".local", "bin", name)
                        if os.path.isfile(cand) and os.access(cand, os.X_OK):
                            exe = cand
                            break
                    if exe:
                        break
            if not exe:
                return None
            p = subprocess.run([exe, "--version"], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL)
            raw = p.stdout if p.returncode == 0 else ""
        except (OSError, ValueError, Exception):
            raw = ""
    import re as _re
    m = _re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    return tuple(int(x) for x in m.groups()) if m else None


def choose_hook_mode():
    """-> (mode, reason): which route install.sh should wire. GT_HOOK_MODE (plugin|settings)
    overrides; otherwise plugin from Claude Code 2.1.136, settings below it or when the
    version cannot be read -- the route that is known to fire on that machine."""
    forced = (os.environ.get("GT_HOOK_MODE") or "").strip().lower()
    if forced in HOOK_MODES:
        return forced, "GT_HOOK_MODE=%s" % forced
    v = claude_code_version()
    need = ".".join(str(x) for x in PLUGIN_HOOKS_MIN_CC)
    if v is None:
        return "settings", "Claude Code version unknown (no `claude` on PATH)"
    have = ".".join(str(x) for x in v)
    if v >= PLUGIN_HOOKS_MIN_CC:
        return "plugin", "Claude Code %s >= %s" % (have, need)
    return "settings", "Claude Code %s < %s" % (have, need)


def current_hook_mode(home=None):
    """What this machine is wired for: "plugin" when the wired dir exists, else "settings"."""
    return "plugin" if os.path.isdir(wired_dir(home)) else "settings"


def _write_text_atomic(path, text):
    import tempfile
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".gt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _target_check(row):
    """Lines that end the wired file quietly when the hook it execs is gone, and run a present hook
    that lost its execute bit through sh (0.22.0). Without the first, a stale wired file execs a
    missing target, exits 126, and a hook registered with onFailure: block then blocks every tool
    call it matches; an absent target is the same silent no-op as an absent wired file. Without
    the second, a sync that drops +x would turn a guard off without a word."""
    try:
        toks = shlex.split(row["command"])
    except ValueError:
        return ""
    script = row["script"]
    for i, tok in enumerate(toks):
        if tok == script or tok.endswith("/" + script):
            q = shlex.quote(tok)
            text = "[ -f %s ] || exit 0\n" % q
            if i == 0 and tok.endswith(".sh"):
                text += "[ -x %s ] || exec sh %s\n" % (q, row["command"])
            return text
    return ""


def wired_text(row):
    meta = {k: row[k] for k in ("event", "script", "owner", "matcher", "module") if k in row}
    return "%s%s\n%sexec %s\n" % (WIRED_HEADER, json.dumps(meta, sort_keys=True),
                                  _target_check(row), row["command"])


WIRED_MANIFEST = "wired-manifest.json"


def wired_manifest_path(home=None):
    return os.path.join(os.path.dirname(wired_dir(home)), WIRED_MANIFEST)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_wired_manifest(home=None):
    """-> {wired name: sha256} the installer recorded, or None when there is no record (an install
    from before 0.22.0, or a damaged file): then nothing is judged, and the next install writes one."""
    try:
        with open(wired_manifest_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
        files = data.get("files") if isinstance(data, dict) else None
        if isinstance(files, dict) and all(isinstance(v, str) for v in files.values()):
            return files
    except (OSError, ValueError):
        pass
    return None


def _save_wired_manifest(files, home=None):
    _write_text_atomic(wired_manifest_path(home),
                       json.dumps({"schema": 1, "files": dict(sorted(files.items()))}, indent=1) + "\n")


def write_wired(rows, home=None):
    """Write one wired file per row (hook_commands rows). -> names changed. Idempotent. Also records
    each file's SHA-256 in wired-manifest.json (0.22.0, review A4), so a wired file the installer
    did not write, or one edited since, is found and not run (`wired_problems`)."""
    d = wired_dir(home)
    os.makedirs(d, exist_ok=True)
    changed = []
    names = []
    for r in rows:
        name = wired_name(r["event"], r["script"])
        names.append(name)
        p = os.path.join(d, name)
        text = wired_text(r)
        try:
            with open(p, encoding="utf-8") as fh:
                if fh.read() == text:
                    continue
        except OSError:
            pass
        _write_text_atomic(p, text)
        changed.append(name)
    files = dict(read_wired_manifest(home) or {})
    for name in names:
        files[name] = _sha256_file(os.path.join(d, name))
    _save_wired_manifest(files, home)
    return changed


def wired_problems(home=None):
    """-> [(kind, wired name)] where kind is `untracked` (a file the installer never wrote),
    `changed` (its bytes differ from the record) or `missing` (recorded, gone). [] with no record."""
    files = read_wired_manifest(home)
    d = wired_dir(home)
    if files is None or not os.path.isdir(d):
        return []            # no record, or no wired route at all (the settings route removes the folder)
    out = []
    try:
        present = sorted(n for n in os.listdir(d)
                         if not n.startswith(".") and "__" in n and os.path.isfile(os.path.join(d, n)))
    except OSError:
        present = []
    for name in present:
        if name not in files:
            out.append(("untracked", name))
        else:
            try:
                if _sha256_file(os.path.join(d, name)) != files[name]:
                    out.append(("changed", name))
            except OSError:
                out.append(("changed", name))
    out += [("missing", n) for n in sorted(files) if n not in present]
    return out


def quarantine_wired(home=None):
    """Move every untracked or changed wired file to wired/.quarantine/, out of the dispatch path
    (the stub tests `[ -f ]` on the wired name, so a moved file is not sourced). -> names moved.
    A missing file is only reported, never recreated: the installer owns that."""
    moved = []
    d = wired_dir(home)
    q = os.path.join(d, ".quarantine")
    for kind, name in wired_problems(home):
        if kind not in ("untracked", "changed"):
            continue
        try:
            os.makedirs(q, exist_ok=True)
            os.replace(os.path.join(d, name), os.path.join(q, name))
            moved.append(name)
        except OSError:
            pass
    return moved


def wired_session_line(home=None):
    """The SessionStart line, or "" when every wired file matches the record. Names the files;
    never their contents or the commands in them."""
    moved = quarantine_wired(home)
    missing = [n for k, n in wired_problems(home) if k == "missing"]
    if not moved and not missing:
        return ""
    parts = []
    if moved:
        parts.append("%d wired hook file(s) did not match the install record and were not run (moved "
                     "to wired/.quarantine): %s" % (len(moved), ", ".join(moved)))
    if missing:
        parts.append("%d recorded wired hook file(s) are missing: %s" % (len(missing), ", ".join(missing)))
    return "GOLDEN THREAD wired hooks: %s. Re-run install.sh to restore." % "; ".join(parts)


def _wired_session_report():
    line = wired_session_line()
    if line:
        print(line)


def read_wired(home=None):
    """-> [{event, script, owner, matcher?, module?, command, file}] for every wired file.
    A file without the gt-wired header is reported with event/script from its name only."""
    d = wired_dir(home)
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        p = os.path.join(d, name)
        if name.startswith(".") or "__" not in name or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        meta = {}
        if lines and lines[0].startswith(WIRED_HEADER):
            try:
                meta = json.loads(lines[0][len(WIRED_HEADER):])
            except ValueError:
                meta = {}
        event, _, script = name.partition("__")
        cmd = next((ln[5:] for ln in lines if ln.startswith("exec ")), "")
        row = {"event": meta.get("event", event), "script": meta.get("script", script),
               "owner": meta.get("owner", ""), "command": cmd, "file": p}
        for k in ("matcher", "module"):
            if k in meta:
                row[k] = meta[k]
        out.append(row)
    return out


def prune_wired(keep, home=None):
    """Remove wired files whose name is not in `keep` (a set of wired names). -> removed."""
    removed = []
    for r in read_wired(home):
        name = os.path.basename(r["file"])
        if name not in keep:
            try:
                os.unlink(r["file"])
                removed.append(name)
            except OSError:
                pass
    if removed:
        files = read_wired_manifest(home)
        if files is not None:
            _save_wired_manifest({k: v for k, v in files.items() if k not in removed}, home)
    return removed


def remove_wired_dir(home=None):
    """The settings route: no wired files at all, so the plugin's entries do nothing."""
    import shutil
    d = wired_dir(home)
    try:
        os.unlink(wired_manifest_path(home))
    except OSError:
        pass
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def wired_events(home=None):
    """The wired files as settings.json-shaped events: {event: [block, ...]}. Readers that
    used to look only at settings.json (the wiring check, doctor, gt_lint's core-unenforced,
    the plugin root lookup) merge this in, so both routes read the same way."""
    events = {}
    for r in read_wired(home):
        block = {"hooks": [{"type": "command", "command": r["command"], "gt_wired": r["file"]}]}
        if r.get("matcher") is not None:
            block["matcher"] = r["matcher"]
        events.setdefault(r["event"], []).append(block)
    return events


def effective_hook_events(settings_path=None, home=None):
    """settings.json's hooks plus the wired ones. Unreadable settings count as none."""
    path = settings_path or os.path.join(home or os.path.expanduser("~"), ".claude",
                                         "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        events = dict((data.get("hooks") or {}) if isinstance(data, dict) else {})
    except Exception:
        events = {}
    for ev, blocks in wired_events(home).items():
        cur = events.get(ev)
        events[ev] = (list(cur) if isinstance(cur, list) else []) + blocks
    return events


def shipped_hooks_declares(version_dir, event, script):
    """Does `version_dir/hooks/hooks.json` carry the dispatcher for (event, script)?"""
    try:
        with open(os.path.join(version_dir, "hooks", "hooks.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        want = plugin_hook_command(event, script)
        return any(h.get("command") == want
                   for b in (doc.get("hooks") or {}).get(event) or []
                   for h in b.get("hooks") or [])
    except Exception:
        return False


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

    # No readable settings.json means nothing is wired there -- report it as such rather
    # than passing quietly, which is the whole failure being closed. Since 0.21.0 the wired
    # files of the plugin route count too (effective_hook_events).
    events = effective_hook_events(settings_path, home)
    mod_dirs = {}
    if any(r.get("module") for r in declared):
        root = os.path.abspath(plugin_root or os.path.dirname(os.path.dirname(
            os.path.abspath(version_dir))))
        mod_dirs = {r["module"]: r["src"] for r in module_registrations(
            root, home, os.path.basename(os.path.abspath(version_dir)))}

    rows = []
    for reg in declared:
        hooks_here = [h for block in events.get(reg["event"], []) or []
                      if isinstance(block, dict)
                      for h in (block.get("hooks") or []) if isinstance(h, dict)]
        found = next((h for h in hooks_here if reg["script"] in (h.get("command") or "")), None)
        hit = (found.get("command") or "") if found else None
        if hit is None:
            rows.append({"script": reg["script"], "event": reg["event"],
                         "owner": reg.get("owner", ""), "state": "unwired",
                         "detail": ""})
            continue
        # The plugin route: a wired file runs only if the plugin's hooks.json carries its
        # dispatcher. A release whose hooks.json lacks the entry leaves the file inert.
        if found.get("gt_wired"):
            vd = mod_dirs.get(reg["module"]) if reg.get("module") else version_dir
            if vd and not shipped_hooks_declares(vd, reg["event"], reg["script"]):
                rows.append({"script": reg["script"], "event": reg["event"],
                             "owner": reg.get("owner", ""), "state": "unwired",
                             "detail": "wired, but %s/hooks/hooks.json has no entry for it"
                                       % os.path.basename(os.path.abspath(vd))})
                continue
        # Wired. Now: do the paths it names still exist? This catches the version
        # dir deleted by a later bump, and the unquoted-path split that silently
        # handed the checker half a directory name.
        try:
            argv = shlex.split(hit)
        except ValueError:
            continue                       # unparseable quoting: not ours to judge
        # The command IS the hook script but the shell reads it with no "/" left: an
        # unquoted Windows path loses every backslash to the shell ("C:Users..."), so the
        # hook never ran while this row said wired (0.19.2, seen on Windows). An absolute
        # POSIX path always keeps its "/", so nothing changes on macOS or Linux.
        if (argv and argv[0] != reg["script"] and argv[0].endswith(reg["script"])
                and "/" not in argv[0]):
            rows.append({"script": reg["script"], "event": reg["event"],
                         "owner": reg.get("owner", ""), "state": "badpath",
                         "detail": argv[0]})
            continue
        # The hook file itself: a wired entry whose script was deleted from the hooks folder ran
        # nothing while this row said wired (0.22.0, validation F13). Only an absolute path is
        # judged; a bare name resolves through the shell's own lookup.
        # Only for a script this release ships (apply can copy it back); a module's or another
        # release's script is judged by the checks above.
        shipped = any(os.path.isfile(os.path.join(version_dir, d, reg["script"])) for d in ("hooks", "scripts"))
        gone = next((a for a in argv if shipped and os.path.isabs(a) and os.path.basename(a) == reg["script"]
                     and not os.path.exists(a)), None)
        if gone:
            rows.append({"script": reg["script"], "event": reg["event"],
                         "owner": reg.get("owner", ""), "state": "badpath",
                         "detail": "%s is missing" % gone})
            continue
        # An interpreter named by absolute path (macOS's recorded one, Windows's) that is gone
        # -- a `brew upgrade` removes python@3.9 -- means every .py hook fails open (0.20.1).
        if argv and os.path.isabs(argv[0]) and not reg["script"].endswith(".sh") \
                and not os.path.exists(argv[0]):
            rows.append({"script": reg["script"], "event": reg["event"],
                         "owner": reg.get("owner", ""), "state": "badpath",
                         "detail": argv[0]})
            continue
        for i, a in enumerate(argv[1:], 1):
            # gt_version_check's `check <plugin-root>` (0.20.1): a source tree deleted after the
            # install (INSTALL.md option C installs from a temporary folder) is not a broken
            # hook -- it runs, and says the source is gone. The doctor's version row reports it;
            # as `badpath` it failed the post-install gate on a healthy install (M7).
            if reg["script"] == "gt_version_check.py" and argv[i - 1] == "check":
                continue
            # The same for this file's own `check <release-dir>`, when the installed copy of
            # that release is in the plugin cache: report() falls back to it and says so.
            if reg["script"] == "gt_components.py" and argv[i - 1] == "check" \
                    and not os.path.exists(a) and os.path.isfile(os.path.join(
                        os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"),
                        os.path.basename(a.rstrip("/\\")), MANIFEST_NAME)):
                continue
            if ("/" in a or os.sep in a) and not a.startswith("-") and not os.path.exists(a):
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
               "Notification", "SubagentStop", "Stop", "PreCompact", "SessionEnd",
               # 0.22.0: every event gt registers must be named here, or two unnamed events keep the order their
               # history gave them and a fresh install and an upgrade disagree (the order a fresh install writes).
               "PermissionDenied", "PostToolBatch")


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


def packs_installed_path(rel, version, cache_plugin="gt"):
    """packs/<tier>/<f>.pack.json -> its copy in the plugin cache, or None.

    Packs are hashed in MANIFEST.json but this mapper used to return None for them -- three
    path segments, not two -- so compare() skipped every one and an edited INSTALLED pack was
    reported as `clean` (security review 2026-09-16). A pack is data, not code, but it is data
    that defines what counts as a credential and what an ignore path is, so a silent edit to an
    installed one is a change to a security control."""
    parts = rel.split("/")              # manifest keys are "/" on every platform
    # workflows/ (0.20.1) lives in the plugin cache exactly like packs. agents/ does too, but
    # its INSTALLED copies are rewritten on purpose (gt_model_policy apply writes model and
    # effort into them), so a hash cannot judge them: gt_agent_spec `resolve` compares each
    # installed definition with the one it should be and falls back when it is not. The
    # manifest still pins agents/ in the source tree (verify-source, the release gate).
    if len(parts) < 2 or parts[0] not in ("packs", "workflows"):
        return None
    return os.path.join(os.path.expanduser(
        "~/.claude/plugins/cache/golden-thread-plugin/%s" % cache_plugin), version, rel)


def hooks_installed_map(rel):
    """hooks/<f> and scripts/<f in HOOK_DIR_SCRIPTS> -> ~/.claude/golden-thread/hooks/<f>;
    everything else uninstalled. Packs need the version, so they go through
    hooks_and_packs_map(version) rather than a second argument here: compare()'s mappers are
    called with exactly one argument, and module_compare's closure takes `names` as its second
    parameter -- passing a version positionally bound it to `names` instead of raising."""
    parts = rel.split("/")
    if len(parts) == 2 and parts[0] == "hooks":
        return os.path.join(INSTALLED_HOOKS, parts[1])
    if len(parts) == 2 and parts[0] == "scripts" and parts[1] in HOOK_DIR_SCRIPTS:
        return os.path.join(INSTALLED_HOOKS, parts[1])
    return None


def hooks_and_packs_map(version):
    """-> a one-argument mapper covering the hook dir AND this version's installed packs."""
    def mapper(rel):
        return hooks_installed_map(rel) or packs_installed_path(rel, version)
    return mapper


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
            parts = rel.split("/")
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


def _integrity_mod():
    """gt_integrity.py beside this file, or None (an older copy, a tree without it)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import gt_integrity
        return gt_integrity
    except Exception:
        return None


def apply(rows):
    """Copy source over installed for `stale` and `missing` ONLY.

    paranoid (0.21.0): when gt's installed code is OS-locked or sealed this is a CODE WRITE, so it
    goes through the same presence-gated unlock the installer does (gate `gt-code-write`): ask a
    person, unlock, repair, then seal and lock again. With no terminal and no unlock authority
    that person cannot be asked -- the repair is skipped, and the message names the route. With
    the group off this is exactly the plain copy it always was."""
    todo = [r for r in rows if r["state"] in ("stale", "missing")]
    gi = _integrity_mod() if todo else None
    if gi is None or not gi.active():
        return _apply_files(rows)
    ok, how, msg = gi.require_presence("repair %d drifted gt component(s)" % len(todo))
    if not ok:
        print("  paranoid: the repair was NOT made -- %s.\n"
              "  Repair it yourself: %s   then re-run install.sh" % (msg, gi.ROUTE_UNLOCK))
        return []
    gi.unlock_all()
    try:
        done = _apply_files(rows)
    finally:
        msgs, errs = gi.converge()
        for m in msgs:
            print("  paranoid: " + m)
        for e in errs:
            print("  paranoid: " + e)
    gi.log_change(gi.home_dir(), event="repair", by=how, files=done)
    return done


def integrity_session_line():
    """paranoid.signed_manifest (0.21.0): at session start, verify the sealed record in full
    (every file, and the signature when it is signed) and say so either way -- a check that
    prints nothing cannot be told from one that never ran. Silent when nothing is sealed."""
    if not os.path.exists(os.path.expanduser("~/.claude/golden-thread/integrity.json")):
        return 0
    gi = _integrity_mod()
    if gi is None:
        print("GOLDEN THREAD integrity: a sealed record exists but gt_integrity.py is missing")
        return 0
    try:
        v = gi.verify()
    except Exception as e:
        print("GOLDEN THREAD integrity: could not be verified (%s)" % type(e).__name__)
        return 0
    if v["ok"]:
        print("GOLDEN THREAD integrity: ok -- %d installed code files match the %s record%s."
              % (v["checked"], v["mode"] or "sealed",
                 " (pinned, not signed: weaker)" if v["mode"] == "pinned" else ""))
    else:
        print("GOLDEN THREAD integrity: FAILED -- %d problem(s), first: %s\n  Route: %s, "
              "then re-run install.sh." % (len(v["problems"]), v["problems"][0], gi.ROUTE_UNLOCK))
    return 0


def _apply_files(rows):
    import shutil
    done = []
    for r in rows:
        if r["state"] not in ("stale", "missing"):
            continue
        try:
            os.makedirs(os.path.dirname(r["dst"]), exist_ok=True)
            shutil.copy2(r["src"], r["dst"])
            # Match install.sh's set_modes: 755 for what runs, 644 for data. A blanket 755 left
            # a repaired pack executable, so self-repair diverged from a fresh install on a file
            # that is input to a security control (review 2026-09-16).
            os.chmod(r["dst"], 0o755 if r["dst"].endswith((".sh", ".py")) else 0o644)
            done.append(r["rel"])
        except Exception:
            continue
    return done


def report(version_dir, pol=None):
    pol = pol or policy()
    if pol == "off":
        return 0
    # THE SOURCE MOVED (request 2026-09-29). This hook is registered with the path of the
    # source tree install.sh ran from. When that tree moves -- gt-src took the repository
    # layout in 0.17.3 and everything went one level down -- the path is gone, and the check
    # reported `badpath` plus `no-manifest`, i.e. it checked nothing and said so obliquely.
    # The installed copy in the plugin cache carries the same release's MANIFEST.json, so
    # compare against that and say plainly what happened and how to re-point the hook.
    moved = None
    if not os.path.isfile(os.path.join(version_dir, MANIFEST_NAME)):
        cached = os.path.join(os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"),
                              os.path.basename(version_dir))
        if os.path.isfile(os.path.join(cached, MANIFEST_NAME)):
            moved = version_dir
            print("GOLDEN THREAD components: the plugin source this check was installed from is "
                  "gone (%s) -- checked the INSTALLED copy of %s instead. To re-point this hook, "
                  "re-run install.sh from wherever the plugin source now lives."
                  % (version_dir, os.path.basename(version_dir)))
            version_dir = cached
    # hooks_and_packs_map, not hooks_installed_map: an edited INSTALLED pack was invisible to
    # the drift check because packs/ has three path segments and the mapper returned None.
    rows = compare(version_dir, hooks_and_packs_map(os.path.basename(version_dir)))
    try:
        rows += module_compare(moved or version_dir) if not moved else []
    except Exception:
        pass
    ex = extras(version_dir) if not moved else []
    wiring = check_wiring(moved or version_dir)
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

def _startup_compact():
    """`startup_output` compact (the default since 0.21.4), via gt_settings; an older copy of
    gt_settings without the helper means the long form, as it printed before."""
    try:
        import gt_settings
        return gt_settings.startup_compact()
    except Exception:
        return False


def _capture(fn, *a):
    """(result, printed text) -- gt_settings.capture when it is there, else a local redirect."""
    try:
        import gt_settings
        return gt_settings.capture(fn, *a)
    except Exception:
        import contextlib
        import io
        buf = io.StringIO()
        r = None
        try:
            with contextlib.redirect_stdout(buf):
                r = fn(*a)
        except Exception as exc:
            buf.write("\n  ... check aborted: %s: %s\n" % (type(exc).__name__, exc))
        return r, buf.getvalue()


def _version_part(src_root):
    """-> (fragment, healthy). gt_version_check's own report, read: 'current' when the newest
    release is installed. Anything else is spelled out by that check's own hook."""
    try:
        import gt_version_check as gv
        if gv.policy() == "off":
            return "version check off", True
        r, text = _capture(gv.report, src_root)
    except Exception as exc:
        return "version not checked (%s)" % type(exc).__name__, False
    if r == 0 and "version: current" in text:
        return "current", True
    if "available" in text:
        return "NEWER RELEASE available (below)", False
    if "AHEAD" in text:
        return "installed is ahead of the source (below)", False
    return "version unknown (below)", False


def _workers_part():
    """-> (fragment, healthy) from gt_workers' own report (read-only: reaping stays with the
    workers hook)."""
    try:
        import gt_workers as gw
        try:
            import gt_settings
            pol = gt_settings.get("orphan_check") or "report"
        except Exception:
            pol = "report"
        if pol == "off":
            return "workers check off", True
        r, text = _capture(gw.report)
    except Exception as exc:
        return "workers not checked (%s)" % type(exc).__name__, False
    if "NOT CHECKED" in text:
        return "workers NOT CHECKED (no ps here)", True
    if r:
        return "workers: %d need a decision (below)" % r, False
    m = re.search(r"clean \S+ (\d+) alive", text)
    return ("workers clean (%s alive, 0 orphaned)" % (m.group(1) if m else "0")), True


def _emit_startup_line(vdir):
    """The compact SessionStart report (0.21.4, `startup_output` compact): version, component
    drift and orphaned workers -- the three facts the vault's CLAUDE.md asks the first reply to
    name -- on ONE line when all are healthy, e.g.

        GOLDEN THREAD: gt 0.21.4 current · components clean (42 hooks wired) · workers clean (2 alive, 0 orphaned)

    A check that is not healthy says so on the line and is spelled out: component drift right
    here (it is this hook's own report), a newer release or an orphan by the version and workers
    hooks, which stay silent while healthy in compact mode. Never raises."""
    r, ctext = _capture(report, vdir)
    m = re.search(r"installed matches (\S+), all (\d+) hooks wired", ctext)
    if m:
        ver, comp, comp_ok = m.group(1), "components clean (%s hooks wired)" % m.group(2), True
    elif r == 0 and not ctext.strip():
        ver, comp, comp_ok = os.path.basename(vdir), "components check off", True
    else:
        ver, comp, comp_ok = os.path.basename(vdir), "components DRIFT (below)", False
    vpart, _ = _version_part(os.path.dirname(os.path.dirname(os.path.abspath(vdir))))
    wpart, _ = _workers_part()
    _r, itext = _capture(integrity_session_line)
    parts = ["gt %s %s" % (ver, vpart) if vpart == "current" else "gt %s" % ver]
    if vpart != "current":
        parts.append(vpart)
    parts += [comp, wpart]
    extra = []
    mi = re.search(r"integrity: ok -- (\d+) installed", itext)
    if mi:
        parts.append("integrity ok (%s files)" % mi.group(1))
    elif itext.strip():
        extra.append(itext.strip())
    if not comp_ok:
        extra.insert(0, ctext.strip())
    try:
        wline = wired_session_line()             # 0.22.0: silent while every wired file is the installer's
    except Exception:                            # noqa: BLE001 - advisory
        wline = ""
    if wline:
        extra.append(wline)
    text = "GOLDEN THREAD: " + " · ".join(parts)
    if extra:
        text += "\n" + "\n".join(extra)
    try:
        import gt_settings
        gt_settings.emit(text, as_hook=True)
    except Exception:
        print(text)


MODULE_USAGE = ("modules <plugin-root>  |  module-check <module-version-dir> [--gt V]"
                "  |  module-states <plugin-root> [--home H] [--gt-version V]"
                " [--with NAME]... [--without NAME]... [--detail]"
                "  |  record-choice <home> <name> on|off [--plugin-root R]"
                "  |  module-cc-gap <name>")


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
      module-cc-gap <name>                       why <name> cannot run on this Claude Code;
                                                 exit 1, printing nothing, when it can
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
            if v["state"] == "off" and v["reason"].startswith(("requires gt", "invalid",
                                                              "needs Claude Code")):
                print("module %s: %s" % (n, v["reason"]), file=sys.stderr)
        print(json.dumps(det if detail else {n: v["state"] for n, v in det.items()},
                         indent=1, sort_keys=True))
        return 0
    if cmd == "module-cc-gap" and len(pos) == 1:
        # 0.21.2: why module pos[0] cannot run on this Claude Code; exit 1 (silent) when it can
        gap = module_cc_gap(pos[0])
        if gap:
            print(gap)
        return 0 if gap else 1
    if cmd == "record-choice" and len(pos) == 3:
        try:
            path = record_choice(pos[0], pos[1], pos[2], plugin_root=proot)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            print(str(e))
            return 2
        print("recorded %s=%s in %s" % (pos[1], pos[2], path))
        if pos[2] == "off":
            # a module switched off takes its lockdown rules with it (0.21.0). Removal only: a
            # module switched ON never widens what Claude may run until the user applies a level.
            try:
                import gt_lockdown
                gt_lockdown.sweep(pos[0], proot)
            except Exception:                    # noqa: BLE001 - the choice is recorded either way
                pass
        return 0
    print("usage: gt_components.py " + MODULE_USAGE)
    return 2


SHELL_ENV_LINES = (
    'case ":$PATH:" in *":$HOME/.claude/golden-thread/bin:"*) ;; '
    '*) export PATH="$HOME/.claude/golden-thread/bin:$PATH" ;; esac',
    "export PYTHONUTF8=1",
)


# -- MCP server commands on native Windows (0.20.1) ----------------------------------------------
# A plugin's .claude-plugin/plugin.json starts its MCP server with `python3` (gt-lotr does). On
# native Windows that is %LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe, the Microsoft Store
# stub: it prints "Python was not found" and exits, so the server never started (0.20.1 open
# item). install.sh therefore rewrites the INSTALLED copies' command to the interpreter it
# resolved and checked -- an absolute python.exe, which Claude Code starts directly, so the
# server stays a DIRECT child of `claude` (gt unlock's register_shim checks exactly that, from
# the kernel; a launcher script in between would make every registration fail). macOS and
# Linux never get here: their `python3` is the real one and the manifest stays as shipped.
MCP_PYTHON_NAMES = ("python3", "python")


def localize_mcp(paths, python):
    """Rewrite `command: python3|python` in each plugin.json's mcpServers to `python` (an
    absolute path to an existing interpreter), with PYTHONUTF8=1 in that server's env.
    -> [(path, server name)] changed. Raises ValueError for an unusable interpreter."""
    if not (python and os.path.isabs(python.replace("/", os.sep)) and os.path.isfile(python)):
        raise ValueError("not an absolute path to an interpreter: %r" % (python,))
    changed = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        touched = False
        for name, cfg in servers.items():
            if not (isinstance(cfg, dict) and cfg.get("command") in MCP_PYTHON_NAMES):
                continue
            cfg["command"] = python
            env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
            env.setdefault("PYTHONUTF8", "1")
            cfg["env"] = env
            # gt starts its servers isolated (python -I, review L1), and -I ignores every
            # PYTHON* variable -- PYTHONUTF8 included. -X utf8 is the same switch as an option.
            args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
            if "-I" in args and "utf8" not in args:
                i = args.index("-I") + 1
                cfg["args"] = args[:i] + ["-X", "utf8"] + args[i:]
            changed.append((p, name))
            touched = True
        if touched:
            # mkstemp + atomic replace (review, low): a fixed "<file>.tmp" is a name another
            # process could plant first -- a symlink would have been written through.
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix=".plugin.json.", dir=os.path.dirname(p) or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(data, fh, indent=2)
                    fh.write("\n")
                try:
                    os.chmod(tmp, os.stat(p).st_mode & 0o777)   # keep the file's own mode
                except OSError:
                    pass
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    return changed


GT_SHIM_MARK = "gt-python3-shim"


def _is_gt_shim(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return GT_SHIM_MARK in fh.read(4096)
    except OSError:
        return False


def export_shell_env(home=None):
    """Windows (0.20.1), and macOS when install.sh wrote a shim (0.20.1): put gt's python3 shim
    on PATH for the model's Bash commands.

    Skills tell Claude to run `python3 <tool>.py`; in Git Bash that is the Microsoft Store stub.
    install.sh writes a shim to ~/.claude/golden-thread/bin/python3, and a SessionStart hook may
    append `export` lines to $CLAUDE_ENV_FILE, which Claude Code sources before every later Bash
    command (code.claude.com/docs/en/hooks, "Persist environment variables"). This check runs at
    SessionStart, so it does it -- only on Windows, only when the shim is installed, and never
    twice into one file. Returns True when it wrote. Never raises: a hook must not fail a session.
    """
    try:
        target = os.environ.get("CLAUDE_ENV_FILE")
        home = home or os.path.expanduser("~")
        shim = os.path.join(home, ".claude", "golden-thread", "bin", "python3")
        if not target or not os.path.isfile(shim):
            return False
        if os.name != "nt":
            # macOS (0.20.1): only a shim install.sh wrote -- it writes one there only when the
            # python3 on PATH FAILED gt's write probe (macOS refused it vault writes), so skills'
            # bare `python3` runs the interpreter that passed, as on Windows (usability run M9).
            # A file of anyone else's in that dir never puts gt's dir first on PATH.
            if sys.platform != "darwin" or not _is_gt_shim(shim):
                return False
        try:
            with open(target, encoding="utf-8") as fh:
                if SHELL_ENV_LINES[0] in fh.read():
                    return False
        except OSError:
            pass
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(SHELL_ENV_LINES) + "\n")
        return True
    except Exception:
        return False


def main():
    if "--hook" in sys.argv[1:] and sys.argv[1:2] == ["check"]:
        export_shell_env()
    args = [x for x in sys.argv[1:] if x != "--hook"]   # see gt_settings.hook_args
    if args and args[0] in ("modules", "module-check", "module-states", "record-choice",
                                         "module-cc-gap"):
        return module_main(args[0], args[1:])
    if args and args[0] == "localize-mcp":
        # localize-mcp --python <abs interpreter> <plugin.json>...   (install.sh, Windows only)
        try:
            rest, py = _take_opt(args[1:], "--python")
            done = localize_mcp(rest, py)
        except ValueError as e:
            print("localize-mcp: %s" % e)
            return 2
        for p, name in done:
            print("MCP server %s -> %s  (%s)" % (name, py, p))
        return 0
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
              "  |  hookdir-scripts [plugin-root] [--home H]"
              "  |  plugin-hooks <version-dir> [--check]  |  hook-mode [--choose] [--home H]"
              "  |  wire-plugin <version-dir> [plugin-root] [--owner O] [--home H]"
              "  |  prune-wired <version-dir> [plugin-root] [--home H]  |  unwire-plugin [--home H]"
              "  |  localize-mcp --python <interpreter> <plugin.json>...  |  " + MODULE_USAGE)
        return 2
    cmd = args[0]
    if cmd == "hook-mode":
        # hook-mode [--choose] [--home H]: which route install.sh should wire (--choose: the
        # bare word, for install.sh), and which one this machine is wired for now.
        mode, why = choose_hook_mode()
        if "--choose" in args:
            print(mode)
        else:
            print("route to wire: %s (%s); wired now: %s"
                  % (mode, why, current_hook_mode(home_opt)))
        return 0
    if cmd == "unwire-plugin":
        print("removed %s" % wired_dir(home_opt) if remove_wired_dir(home_opt)
              else "no wired dir")
        return 0
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
               "verify-source", "plugin-hooks", "wire-plugin", "prune-wired") and not vdir:
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
    if cmd == "plugin-hooks":
        # plugin-hooks <version-dir> [--check]: the hooks.json the release should ship
        # (0.21.0). --check compares it with the shipped file: exit 1 on any difference.
        want = plugin_hooks_for(vdir)
        if "--check" not in args:
            print(json.dumps(want, indent=2))
            return 0
        path = os.path.join(vdir, "hooks", "hooks.json")
        try:
            with open(path, encoding="utf-8") as fh:
                have = json.load(fh)
        except (OSError, ValueError):
            have = None
        # a module may keep a key of its own beside the generated two (the pane's Claude Mods
        # `modules` entry): the check is of what gt generates.
        if isinstance(have, dict):
            keep = ("hooks", "description") + tuple(want or ())
            have = {k: v for k, v in have.items() if k in keep}
        if have == want:
            print("hooks.json matches the declared registrations" if want
                  else "no hooks declared, none shipped")
            return 0
        print("hooks.json %s: regenerate with `gt_components.py plugin-hooks %s > %s`"
              % ("missing" if have is None else "differs from the registrations", vdir, path))
        return 1
    if cmd in ("wire-plugin", "prune-wired"):
        # wire-plugin <version-dir> [plugin-root] [--owner O] [--home H]: write the wired
        # files (the plugin route, 0.21.0) for the registrations of ON modules and gt,
        # optionally only one owner's. prune-wired: remove wired files no registration names.
        own = None
        for k, a in enumerate(args):
            if a == "--owner" and k + 1 < len(args):
                own = args[k + 1]
        rows = hook_commands(vdir, pos[1] if len(pos) > 1 else None, home_opt)
        if cmd == "prune-wired":
            gone = prune_wired({wired_name(r["event"], r["script"]) for r in rows}, home_opt)
            for g in gone:
                print("removed wired %s" % g)
            return 0
        rows = [r for r in rows if not own or r["owner"] == own]
        done = write_wired(rows, home_opt)
        print("wired %d hook(s) for the plugin route (%d changed)%s"
              % (len(rows), len(done), " (%s)" % own if own else ""))
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
        with open(os.path.join(vdir, MANIFEST_NAME), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(man, fh, indent=1, sort_keys=True)
        print("wrote %s with %d file(s)" % (MANIFEST_NAME, len(man["files"])))
    elif cmd == "check":
        if ("--hook" in sys.argv[1:] or os.environ.get("GT_HOOK") == "1") and _startup_compact():
            _emit_startup_line(vdir)
            return 0
        _emit(report, vdir)
        _emit(integrity_session_line)
        _emit(_wired_session_report)             # 0.22.0: a wired file that is not the installer's
        return 0                                 # never a failing exit: advisory only
    elif cmd == "apply":
        rows = (compare(vdir, hooks_and_packs_map(os.path.basename(vdir)))
                + module_compare(vdir, home_opt))
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
    # paranoid.signed_manifest (0.21.0): when gt's code is sealed, a hook checks its own files
    # first (gt_integrity_gate: one stat() when the group is off). Only as a hook -- the CLI
    # forms of this script stay usable, and are the repair route.
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None:
        _gig.check(__file__, "reporter", when="--hook" in sys.argv)
    raise SystemExit(main())
