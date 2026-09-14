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
    "push_check": {
        "default": "report",
        "values": ["off", "report"],
        "summary": "Check at session start whether the vault has commits not yet pushed.",
        "detail": (
            "off     no checking\n"
            "report  name the count, the age of the oldest, and the push command  (default)\n"
            "\n"
            "The vault is a git repo so its truth survives one disk and reaches the other\n"
            "machines. A commit that never leaves is not backed up, is invisible to a\n"
            "session on another host, and looks finished: gt-work reports success and the\n"
            "tree goes clean. On 2026-09-02 the vault was found 16 commits ahead of origin\n"
            "with the oldest dating back weeks. Nothing had failed -- every session had\n"
            "committed correctly, none had pushed, and no check asked.\n"
            "\n"
            "A branch with NO UPSTREAM is reported separately and more loudly. It cannot be\n"
            "ahead, so a naive count returns zero and reads as healthy, when in fact the\n"
            "commits have nowhere to go at all.\n"
            "\n"
            "There is deliberately no `auto`. Pushing is outward-facing: it publishes to a\n"
            "remote others read, it can be rejected, and an unpushed commit is sometimes\n"
            "correct -- work held back on purpose. A push that surprises its author is\n"
            "worse than a delay that annoys them."),
    },
    "watch": {
        "default": "off",
        "values": ["off", "report"],
        "summary": "Watch upstream git repos (/gt:gt-watch) and report their changes at session start.",
        "detail": (
            "off     no fetching and no report  (default)\n"
            "report  the cron fetch runs, and session start lists unacknowledged upstream\n"
            "        changes: P0s first with repo and reason, review items one line each,\n"
            "        routine changes as a count\n"
            "\n"
            "A watch is a note in Projects/golden-thread/watches/. The fetch runs from cron\n"
            "(gt_watch.py install-cron), never from a session; the session-start hook only\n"
            "reads the local queue in ~/.claude/golden-thread/watch/. P0 comes from fixed\n"
            "rules -- a security advisory, a CVE or GHSA id, 'security fix' in a release --\n"
            "never from a reading of commit prose, because a P0 that cries wolf stops being\n"
            "read. GT_WATCH=off|report in the environment overrides this setting."),
    },
    "closeout_check": {
        "default": "ask",
        "values": ["off", "ask"],
        "summary": "At /compact and session end, ask whether a finished-looking project should be closed.",
        "detail": (
            "off     nothing\n"
            "ask     name each project whose signals say it may be finished, with the\n"
            "        reasons, so the assistant puts the question to you  (default)\n"
            "\n"
            "Runs the vault's own `Projects/golden-thread/tools/gt_closeout.py`. Signals:\n"
            "most open tasks past due, most tasks checked off with nothing urgent left,\n"
            "no new task and no write-back for three weeks, or no open task at all. Every\n"
            "time the question is put to you it is recorded with the signal values, and\n"
            "your answer is recorded beside it, so `gt_closeout.py history` can show what\n"
            "'ready to close' has actually looked like for you and the thresholds can be\n"
            "tuned to that rather than to a guess.\n"
            "\n"
            "On 2026-09-05 the CYC26 talk had been delivered for two days while 25 of its\n"
            "rehearsal tasks sat open and overdue at the top of the rollup, above live\n"
            "trading work. Nothing had asked."),
    },
    "test_gate": {
        "default": "auto",
        "values": ["off", "warn", "auto", "block"],
        "summary": "Refuse a `git commit` of code whose tests have not been seen to pass.",
        "detail": (
            "off    commit whatever you like; the Core rule is not injected either\n"
            "warn   allow the commit, but say the tests were not seen to pass\n"
            "auto   block when the repo HAS a discoverable test command, warn when it\n"
            "       does not  (default)\n"
            "block  block regardless -- a repo with no tests cannot commit code\n"
            "\n"
            "Enforces core_test_before_commit through a PreToolUse guard. Evidence is a\n"
            "receipt: tests/run.sh and dev/release-check.sh write one when they pass, any\n"
            "project can write one with `gt_test_receipt.py record --ok`, and editing a file\n"
            "after a run invalidates the receipt for that file automatically.\n"
            "\n"
            "`auto` exists because a gate that fires where it cannot be satisfied is a gate\n"
            "people switch off for everything. A repo with no test entry point is not asked\n"
            "to have one; a repo that has one is held to it.\n"
            "\n"
            "Never blocked: docs-only commits, a repo containing `.gt-no-test-gate`, a\n"
            "command run with GT_TEST_GATE=off, and anything the guard cannot parse.\n"
            "\n"
            "On 2026-09-12 a release was committed and pushed with a stale MANIFEST.json.\n"
            "The gate that catches exactly that existed and had been run before the last few\n"
            "edits rather than after. The discipline was not the problem; it had no\n"
            "mechanism."),
    },
    "parallel_work": {
        "default": "on",
        "values": ["off", "on"],
        "summary": "Run divisible work in parallel instead of one unit at a time.",
        "detail": (
            "off  everything runs serially, and the Core rule asking for parallelism is\n"
            "     NOT injected -- a rule the user has switched off must stop being\n"
            "     asserted, or the setting is decoration\n"
            "on   independent units run concurrently, up to `parallel_max`  (default)\n"
            "\n"
            "This is the switch behind the Core rule core_parallel_when_beneficial, and it\n"
            "reaches two places: the rule injected into every turn, and the default worker\n"
            "count of the test runner (tests/prun.py).\n"
            "\n"
            "The serial default was never a decision. A loop, a sweep over 43 projects and\n"
            "a test suite all run on one core unless someone says otherwise, and nothing\n"
            "reports the waste -- the work completes, correctly, slowly, and its output is\n"
            "identical to the fast version. Measured here on 2026-09-12: the 672-test suite\n"
            "took 509s serial and 108s parallel, 4.7x, with CPU going from roughly one core\n"
            "to 404%.\n"
            "\n"
            "`off` is for when a machine is busy with something else, or when a parallel run\n"
            "is producing a failure a serial run does not -- that difference is a finding\n"
            "worth keeping, which is why the serial path stays supported rather than being\n"
            "removed as dead weight."),
    },
    "parallel_max": {
        "default": "auto",
        "values": None,
        "validate": "auto, or a positive integer number of workers",
        "summary": "Ceiling on concurrent workers. `auto` = as many as the machine allows.",
        "detail": (
            "auto  as many as this MACHINE allows -- the ceilings are measured at install\n"
            "      and again at every upgrade, and stored as `parallel_profile`: cores for\n"
            "      CPU-bound work, 2x cores (bounded by memory) for I/O-bound work, which\n"
            "      is what the tools here mostly do  (default)\n"
            "N     never more than N workers at once, whatever the work\n"
            "\n"
            "`auto` is deliberately not a number YOU have to choose. It resolves through\n"
            "`parallel_profile`, which install.sh measures on this machine and re-measures on\n"
            "every upgrade -- so a new machine or a RAM change is picked up without anyone\n"
            "editing a config, and nothing in the source pretends to know your hardware.\n"
            "Run `gt_settings.py detect-machine --write` to re-measure by hand, or\n"
            "`detect-machine` alone to see what would be measured. Your OWN setting is never\n"
            "overwritten by that: the profile records the hardware, this setting records what\n"
            "you will allow.\n"
            "\n"
            "Set a number when the machine has to stay responsive for something else, or\n"
            "when a remote end is rate-limited. `1` is not the same as parallel_work=off:\n"
            "one worker still runs through the parallel path, so it does not tell you\n"
            "whether the parallel path is what broke a test. Use `off` for that."),
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
    "protected_paths": {
        "default": "ask",
        "values": ["off", "ask"],
        "summary": "Prompt before Write/Edit to core-rules, global-memory, gt hooks or settings.json; refuse overwriting a Source.",
        "detail": (
            "ask  a Write or Edit to the vault's core-rules/ or global-memory/, to\n"
            "     ~/.claude/golden-thread/, or to ~/.claude/settings.json always shows the\n"
            "     permission prompt, whatever the permission mode; overwriting or editing\n"
            "     an EXISTING file under Sources/ is refused -- supersede it with a new\n"
            "     file instead. Creating a new Source is unaffected.  (default)\n"
            "off  no check\n"
            "\n"
            "These paths load into every session of every project (core-rules,\n"
            "global-memory), enforce everything else (the hooks and settings.json), or are\n"
            "immutable by convention (Sources). Until 0.12.9 nothing but skill prose kept a\n"
            "session from writing them. `ask` does not block legitimate work -- promoting a\n"
            "fact to global-memory still works -- it makes a person see it happen.\n"
            "\n"
            "Covers the Write, Edit, MultiEdit and NotebookEdit tools. It does NOT see a\n"
            "shell command that writes the same file (cp, sed -i, a script); gt's own vault\n"
            "tools are covered by guard_vault_writes instead."),
    },
    # install_demo was removed in 0.14.0: the demo is a module, and whether it is
    # installed is a module choice (install.sh --with/--without demo, recorded in
    # ~/.claude/golden-thread/install-choices.json). A user's install_demo key in
    # vault-config.json is left alone; a later migration removes it.
}


def _module_settings():
    """Register the settings every effectively-ON module declares (0.14.0).

    Read from each module's newest module.json via the gt_components beside this file
    (not whatever `gt_components` sys.path would find), so the registry and the
    installer agree on which modules are on. The plugin root is this file's own when it
    runs from a release (<root>/golden-thread/<ver>/scripts), otherwise the one
    install.sh wired into settings.json. A module setting never overrides a gt one.
    Anything missing -- an older gt_components, no plugin root -- registers nothing.
    """
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "gt_components_for_settings", os.path.join(here, "gt_components.py"))
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)
        ver_dir = os.path.dirname(here)
        if (os.path.basename(os.path.dirname(ver_dir)) == "golden-thread"
                and gc._newest_release(os.path.dirname(ver_dir)) is not None):
            root, gt_version = os.path.dirname(os.path.dirname(ver_dir)), os.path.basename(ver_dir)
        else:
            root, gt_version = gc.plugin_root_from_settings(), None
        if not root:
            return
        for name, _vd, data in gc.active_modules(root, gt_version=gt_version):
            for s in data.get("settings") or []:
                if s["key"] in SETTINGS:
                    continue
                SETTINGS[s["key"]] = {
                    "default": s["default"], "values": list(s["values"]),
                    "summary": s["summary"], "module": name,
                    "detail": "%s\n\nProvided by the %s module." % (s["summary"], name)}
    except Exception:
        return


_module_settings()


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
    v = _load().get(name)
    if v is None:
        v = ""
    elif isinstance(v, bool) and spec["values"] == ["yes", "no"]:
        # JSON true/false is the natural way to hand-write a yes/no flag.
        v = "yes" if v else "no"
    elif not isinstance(v, str):
        sys.stderr.write("gt_settings: %s=%s in %s is not a string; using the default %r\n"
                         % (name, json.dumps(v), CONFIG, spec["default"]))
        v = ""
    v = v.strip().lower()
    if spec["values"] is None:
        return v if _freeform_ok(name, v) else spec["default"]
    return v if v in spec["values"] else spec["default"]


def _freeform_ok(name, value):
    """Validate a setting whose values are not a closed list.

    Only `parallel_max` is free-form, and it is free-form for a reason: a worker
    ceiling is a number, and enumerating 1..64 in `values` would be a list nobody
    reads pretending to be a type. Anything unparseable falls back to the registered
    default rather than being written through, so a typo cannot silently uncap or
    serialise every run.
    """
    if name != "parallel_max":
        return False
    if value == "auto":
        return True
    try:
        return int(value) >= 1
    except ValueError:
        return False


def parallel_jobs(want, io_bound=False):
    """-> worker count to use for `want` units of independent work, honouring settings.

    The single place the two settings turn into a number, so every caller answers
    "how many workers" the same way instead of each inventing a policy.

    `parallel_work=off` returns 1 -- serial, through whatever path the caller uses.
    `parallel_max=auto` returns cores, or cores + 4 (capped) when the work is
    I/O-bound: these tools spend their time waiting on install.sh, SSH and HTTP, so
    more workers than cores is the correct answer there and `auto` means "as many as
    the machine allows", not "exactly ncpu".
    """
    want = max(1, int(want))
    if get("parallel_work") == "off":
        return 1
    prof = machine_profile()
    cap = prof["io_max"] if io_bound else prof["cpu_max"]
    limit = get("parallel_max")
    if limit != "auto":
        cap = min(cap, int(limit))
    return max(1, min(want, cap))


def detect_machine():
    """-> the ceilings THIS machine supports, measured now.

    `auto` has to mean "as much as this machine allows", and the machine is not
    knowable from a number someone wrote down: 20 workers was right for the laptop it
    was chosen on and arbitrary everywhere else. So the ceilings are derived from the
    hardware and stored at install time (see write_machine_profile), not guessed at
    each call and not frozen into the source.

    cpu_max = logical cores. More processes than cores on CPU-bound work costs
    context switching and returns nothing.

    io_max = 2x cores, and never more workers than there is memory to hold them
    (~1 worker per 256 MB, which is generous for a subprocess that spends its life
    waiting). I/O-bound work is waiting, not computing, so oversubscribing cores is
    the correct answer -- the limit is memory and the remote end, not the CPU.
    """
    import platform
    import socket
    import subprocess

    def sysctl(name):
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
            return int(out.stdout.strip())
        except Exception:
            return 0

    cores = os.cpu_count() or 4
    physical = sysctl("hw.physicalcpu") or cores
    membytes = sysctl("hw.memsize")
    if not membytes:                      # Linux
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        membytes = int(line.split()[1]) * 1024
                        break
        except Exception:
            membytes = 0
    memory_gb = round(membytes / (1024 ** 3), 1) if membytes else 0.0
    by_memory = int(memory_gb * 4) if memory_gb else 10 ** 6   # ~1 worker / 256 MB
    return {
        "cores": cores,
        "physical_cores": physical,
        "memory_gb": memory_gb,
        "cpu_max": max(1, cores),
        "io_max": max(2, min(cores * 2, by_memory)),
        "host": socket.gethostname(),
        "platform": platform.platform(),
    }


def machine_profile():
    """The stored profile, or a live reading if none has been written yet.

    Stored, because a profile is measured at install and re-measured at update -- it
    must not drift under a running session, and every tool on the machine must get the
    same answer. Missing is not an error: a clone with no ~/.claude still has to work,
    it just falls back to reading the machine now.
    """
    prof = _load().get("parallel_profile")
    if isinstance(prof, dict) and prof.get("cpu_max") and prof.get("io_max"):
        try:
            return {"cpu_max": max(1, int(prof["cpu_max"])),
                    "io_max": max(1, int(prof["io_max"]))}
        except (TypeError, ValueError):
            pass
    return detect_machine()


def write_machine_profile(force=False):
    """Measure and store the profile. Called by install.sh on install AND upgrade.

    Returns (profile, changed). It rewrites `parallel_profile`, which is a record of
    the HARDWARE, and never touches `parallel_work` or `parallel_max`, which are the
    user's preferences: re-running the installer must not undo a ceiling somebody chose
    deliberately. A profile whose numbers are unchanged is not rewritten, so the
    detected_at timestamp means "when this machine last looked different".
    """
    import datetime
    d = _load()
    if not d.get("vault_path"):
        return None, False
    fresh = detect_machine()
    old = d.get("parallel_profile") or {}
    same = all(old.get(k) == fresh[k] for k in ("cores", "cpu_max", "io_max", "host"))
    if same and not force:
        return old, False
    fresh["detected_at"] = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    d["parallel_profile"] = fresh
    _save(d)
    return fresh, True


HOOK_FLAG = "--hook"


def hook_args(argv=None):
    """-> (argv without the hook flag, True if it was present).

    install.sh registers every hook command with `--hook`. That flag, not a
    guess, is what decides whether output is wrapped as hook JSON. 0.9.6 used
    `sys.stdout.isatty()`, which is False for a hook but ALSO for `| tee`, cron,
    a subagent and every command the assistant runs through its Bash tool -- so
    the plain-text report the docstrings promised came out as an escaped JSON
    blob for everyone except a human at a terminal.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    is_hook = HOOK_FLAG in argv or os.environ.get("GT_HOOK") == "1"
    return [a for a in argv if a != HOOK_FLAG], is_hook


def emit(text, event="SessionStart", as_hook=None):
    """Deliver a startup report to BOTH the user and the model.

    A SessionStart hook's plain stdout goes into the model's context only -- it is
    never rendered in the user's terminal. So every one of these checks was already
    "said out loud" in the sense its own comments claim, but only to the assistant.
    From the user's side a clean check and a hook that never ran are the same thing:
    silence. That is the 2026-08-30 shape again, one layer out -- the check was
    honest, its delivery went to the wrong audience.

    `systemMessage` is the field that surfaces to the user on any platform;
    `additionalContext` is what the model reads. Both are sent deliberately: the
    model must keep receiving this, because CLAUDE.md asks the assistant to open
    each session by stating these results, and that prose rule is the backstop for
    anything this JSON cannot reach.

    Hook JSON is produced only when the caller says it is a hook (`as_hook`, or
    the `--hook` flag or GT_HOOK=1 seen by `hook_args`). Every other caller -- a
    human, a pipe, the assistant's Bash tool -- gets the plain text unchanged.
    """
    text = (text or "").strip()
    if not text:
        return
    if as_hook is None:
        as_hook = hook_args()[1]
    if not as_hook:
        print(text)
        return
    json.dump({
        "systemMessage": text,
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        },
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def capture(fn, *a, **kw):
    """Run fn, returning (result, text-it-printed).

    Lets the reporting functions keep their many plain print() calls -- they are
    readable that way, and they are also the manual CLI output -- while the hook
    entry point decides how the result is delivered.

    If fn raises part-way, everything it printed so far is KEPT and the failure is
    appended to it, so the report is delivered with the crash rather than replaced
    by it. 0.9.6 dropped the buffer on the way out: a worker report that had
    already listed three orphans, then hit an unexpected error in reap(), reached
    nobody -- only a traceback on stderr, which a hook shows to no one.
    """
    import contextlib
    import io
    import traceback
    buf = io.StringIO()
    r = None
    try:
        with contextlib.redirect_stdout(buf):
            r = fn(*a, **kw)
    except Exception as exc:
        buf.write("\n  ... check aborted: %s: %s\n" % (type(exc).__name__, exc))
        sys.stderr.write(traceback.format_exc())
    return r, buf.getvalue()


def show():
    cfg = _load()
    print("Golden Thread settings   (%s)" % CONFIG)
    print()
    groups = [(None, [n for n, s in SETTINGS.items() if not s.get("module")])]
    for mod in sorted({s["module"] for s in SETTINGS.values() if s.get("module")}):
        groups.append((mod, [n for n, s in SETTINGS.items() if s.get("module") == mod]))
    for mod, names in groups:
        if mod:
            print("module %s" % mod)     # a module's settings, under its name
            print()
        for name in names:
            spec = SETTINGS[name]
            raw = cfg.get(name)
            cur = get(name)
            mark = "" if raw not in (None, "") else "   (default — not set in the file)"
            print("  %-20s %s%s" % (name, cur, mark))
            print("  %-20s %s" % ("", spec["summary"]))
            opts = " | ".join(spec["values"]) if spec["values"] else spec["validate"]
            print("  %-20s options: %s" % ("", opts))
            print()
    prof = cfg.get("parallel_profile")
    if isinstance(prof, dict):
        print("  %-20s %s" % ("parallel_profile",
                              "cpu_max %s, io_max %s   (%s core(s), %s GB, measured %s)"
                              % (prof.get("cpu_max"), prof.get("io_max"),
                                 prof.get("cores"), prof.get("memory_gb"),
                                 prof.get("detected_at", "?"))))
        print("  %-20s %s" % ("", "what `parallel_max: auto` resolves to on this machine; "
                                  "measured at install, re-measured on upgrade"))
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


def check_against_machine(value, force=False):
    """-> (refuse: bool, message or None) for a parallel_max the user typed.

    A ceiling the hardware cannot honour is not a preference, it is a typo with
    consequences: `40` on a four-core laptop makes nothing faster, it thrashes, and the
    person who typed it has no way to learn that from a confirmation message. So the
    value is checked against the measured profile at the moment it is set -- the only
    moment anyone is paying attention to it.

    Two bands, because "more than the processors" is not automatically wrong:

      * above io_max (2x cores)  -> refused. Nothing here can use it.
      * cores < N <= io_max      -> allowed WITH a warning. Oversubscribing cores is
                                    correct for I/O-bound work and useless for
                                    CPU-bound work; the user should know which they
                                    are buying.

    --force covers the case the profile cannot see: a fan-out over a hundred
    high-latency remotes is bounded by the network, not by this CPU. It is explicit, so
    it shows up in the transcript instead of being assumed.
    """
    if value == "auto":
        return False, None
    n = int(value)
    prof = machine_profile()
    cores, io_max = prof["cpu_max"], prof["io_max"]
    if n > io_max and not force:
        return True, ("%d is more than this machine can use: %d core(s), so at most %d "
                      "workers even for I/O-bound work.\n"
                      "  `auto` already means \"as many as this machine allows\", and it "
                      "re-measures itself on every upgrade.\n"
                      "  If you meant it -- a fan-out bounded by the network rather than "
                      "this CPU -- re-run with --force." % (n, cores, io_max))
    if n > cores:
        return False, ("note: %d exceeds this machine's %d core(s). That helps I/O-bound "
                       "work (waiting on SSH, HTTP, subprocesses) and does nothing for "
                       "CPU-bound work." % (n, cores))
    return False, None


def set_value(name, value, force=False):
    spec = SETTINGS.get(name)
    if not spec:
        print("unknown setting: %s" % name)
        print("known: %s" % ", ".join(SETTINGS))
        return 2
    value = (value or "").strip().lower()
    bad = (not _freeform_ok(name, value)) if spec["values"] is None \
        else (value not in spec["values"])
    if bad:
        print("invalid value %r for %s" % (value, name))
        print("valid: %s" % (spec["validate"] if spec["values"] is None
                             else " | ".join(spec["values"])))
        return 2
    if name == "parallel_max":
        refuse, msg = check_against_machine(value, force=force)
        if refuse:
            print("invalid value %r for parallel_max" % value)
            print("  " + msg)
            return 2
        if msg:
            print("  " + msg)
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
        force = "--force" in a
        rest = [x for x in a[1:] if x != "--force"]
        if len(rest) < 2:
            print("usage: gt_settings.py set <name> <value> [--force]")
            return 2
        return set_value(rest[0], rest[1], force=force)
    if a[0] == "get" and len(a) > 1:
        print(get(a[1]) or "")
        return 0
    if a[0] in ("detect-machine", "machine"):
        force = "--force" in a
        if "--write" in a or force:
            prof, changed = write_machine_profile(force=force)
            if prof is None:
                print("refusing to write %s: it has no vault_path. Run /gt:gt-init first."
                      % CONFIG)
                return 2
            print("parallel_profile %s: %d core(s), %.1f GB -> cpu_max %d, io_max %d"
                  % ("updated" if changed else "unchanged", prof.get("cores", 0),
                     prof.get("memory_gb", 0.0), prof["cpu_max"], prof["io_max"]))
            return 0
        prof = detect_machine()
        print(json.dumps(prof, indent=2))
        return 0
    if a[0] == "jobs":
        # The budget as a NUMBER, for shell scripts and for any project's own tooling:
        #   JOBS=$(gt_settings.py jobs 42 --io-bound)
        # Without this the two settings are only reachable from Python, and a bash
        # script's only option is to invent its own policy -- which is what the Core
        # rule core_parallel_when_beneficial is trying to stop happening in every repo.
        rest = [x for x in a[1:] if x not in ("--io-bound", "--io")]
        io_bound = len(rest) != len(a[1:])
        try:
            want = int(rest[0]) if rest else 10 ** 6
        except ValueError:
            print("usage: gt_settings.py jobs [<units>] [--io-bound]", file=sys.stderr)
            return 2
        print(parallel_jobs(want, io_bound=io_bound))
        return 0
    print("usage: gt_settings.py [show | get <name> | set <name> <value> | "
          "explain <name> | jobs [<units>] [--io-bound] | "
          "detect-machine [--write] [--force]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
