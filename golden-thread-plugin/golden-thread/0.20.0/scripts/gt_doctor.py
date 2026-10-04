#!/usr/bin/env python3
"""One command for the whole health picture: `gt_doctor.py`.

    gt_doctor.py                 # every check, human-readable
    gt_doctor.py --json          # the same, machine-readable
    gt_doctor.py --fix           # apply only the repairs that cannot lose work
    gt_doctor.py --only wiring   # one check
    gt_doctor.py post-install [--vault V] [--release DIR] [--stage S] [--json] [--dry-run]

Exit code: 0 all clear, 1 something needs attention, 2 a check could not run.

## post-install: the release gate (0.17.11)

`post-install` is a PROFILE, not another check: one command the owner runs on a machine after
install.sh, a Claude Code restart and /gt:gt-upgrade, which proves that machine is correct for
THIS release. It prints a PASS/FAIL table and exits 1 on any FAIL, 0 otherwise (WARN, INFO and
PENDING never fail it). Every row fails closed: a row that cannot run is FAIL with "could not
run: why", never PASS. It is read-only -- it writes neither the vault nor settings.json; the
hooks it exercises are run against a copy of vault-config.json in a temporary HOME.

  release         which release dir the answers are relative to (--release, else this file's own
                  release when its directory is named as a version, else the installed release
                  from installed_plugins.json, else the newest under the wired plugin root)
  installed       installed gt version == the release, and every ON module at its newest version
  components      gt_components drift clean against the release (reuses `components`)
  wiring          every declared hook wired (reuses `wiring`) and each Core rule's mechanism
                  wired (reuses `core-rules`)
  rule1-vault     the vault's core_concurrent_session_claim.md imperative == the shipped template
  rule1-injected  the installed inject_core_rules.sh, fed '{}', makes rule 1 the queue-first text
  queue-scripts   gt_write_queue / gt_broker / gt_demote / gt_daily in the hooks dir, byte-identical
                  to the release
  guard-denies    the installed guard_session_claims.sh denies a synthetic Write to <vault>/INBOX.md
                  and raises no objection to a path outside the vault (nothing is written)
  queue           gt_broker.py status: empty = PASS, requests waiting = WARN with the drain command
  vault           stamp and migrations current for the release (reuses `vault`)
  lotr            module on: its installed lotr.py exists and `--help` exits 0; off: INFO
  daily-job       the launchd daily-note job, if installed, runs the hooks-dir gt_daily.py; else INFO

--stage install|session (used by install.sh and by the first SessionStart after a version change)
reports the rows that cannot be true until /gt:gt-upgrade has run -- rule1-vault, rule1-injected
when the vault copy is the reason, vault -- as PENDING with that next step, never PASS or FAIL.
The default stage, `final`, is the gate: those rows are FAIL there.

SMOKE rows prove the installed copies WORK. Each runs the installed scripts against its own
throwaway vault + HOME (removed afterwards, even on failure), in parallel with each other and
with the rows above; every step has a hard timeout, and a timeout is FAIL "timed out after Ns":

  smoke-queue     gt_write_queue.py append -> gt_broker.py drain -> the text is there, queue empty
  smoke-escalate  a replace-file whose target changed after queueing is escalated; the edit survives
  smoke-guard     guard_session_claims.sh denies a direct Write to the temp vault's .md
  smoke-rules     inject_core_rules.sh injects every shipped rule, rule 1 queue-first
  smoke-lotr      lotr on: init, lotrd up, `lotr.py find ""` ok, MCP tools/list = exactly 4 tools
  smoke-daily     gt_daily.py --dry-run exits 0 against a temp git vault

The summary line carries the elapsed time; the owner's budget for the whole run is about a minute.

When a run contains BOTH a finding and a check that could not run, the exit code is 1,
not 2: `Report.worst` ranks FAIL above UNKNOWN, and a known break outranks an unknown
one for the purpose of "what do I do next?". That is deliberate, and it is the one way
exit 1 does NOT mean "everything was checked and something is wrong" -- so the report
never leaves it implicit: the footer line and the JSON `unrunnable` key name every
check that could not run, whatever the exit code turned out to be.

## Why one command

Everything here already existed, in five scripts and a SessionStart message: version,
component drift, hook wiring, stray workers, push state, lint. Each is correct and
each reports somewhere different, and the SessionStart message reached only the
assistant until 0.9.6 -- so "is this install healthy?" had no answer a person could
ask for directly. Worse, a clean report from a check pinned to the WRONG version
reads exactly like a clean install: on 2026-08-30 a component check aimed at 0.6.0
reported clean while 0.9.4 sat uninstalled beside it.

So this states the version every other answer is relative to, at the top, always.

## Every check answers a different question

  version     is the newest release the one installed?
  components  do the installed FILES match that release?
  wiring      is every hook the release declares actually in settings.json?
  core-rules  does each rule that claims enforcement have ITS OWN mechanism wired --
              the right script, at the installed path, present on disk?
  modules     which modules are on or off, does each admit this gt, and is every ON
              module's plugin actually installed and enabled? (read-only)
  vault       is the vault reachable, what release is it stamped at, and does
              gt_upgrade have anything pending for it? (asks gt_upgrade itself)
  schedule    has every INSTALLED scheduled job (gt_schedule.py) run and exited
              normally? A job never installed is not a finding.
  workers     are there background processes nobody declared?
  push        do this machine's commits exist anywhere else?
  gt-src      does the publish destination still match what was published?
              (only where one is configured: $GT_SRC or `gt_src` in vault-config.json)
  lint        what does the vault linter say, in one line?
  repo-target which git repo does THIS working directory resolve to, and is it the vault?
              Always a `note` (i) row, never ok and never a finding (0.18.1)
  hooks-schema does every settings.json hook entry name an event Claude Code fires and,
              for tool events, a tool it knows? (allowlists hooks/known_events.json and
              hooks/known_tools.json; also any entry pointing at a missing gt hook file)
  execution   is the parallel profile measured or the default (and how old), is this shell
              translated under Rosetta, is TMPDIR inside a synced folder? (gt_bench.py health,
              0.18.1)

A check that cannot run says so and exits 2. "Could not check" is never "clean" --
that distinction is the whole reason this file exists.
"""
import sys as _sys
import os as _os
# gt-src is a published tree that must keep matching its SHA256SUMS, and this script is
# run from it or handed it: write no bytecode, here or in any child (0.19.1).
_sys.dont_write_bytecode = True
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".claude" / "vault-config.json"
SETTINGS = Path.home() / ".claude" / "settings.json"
INSTALLED_HOOKS = Path.home() / ".claude" / "golden-thread" / "hooks"
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
MARKET_NAME = "golden-thread-plugin"

OK, WARN, FAIL, UNKNOWN, SKIPPED = "ok", "warn", "fail", "unknown", "skipped"
# SKIPPED is a check that does not apply to this machine because nothing configured it
# (e.g. no publish destination). It never raises the exit code; it is not "clean" either,
# which is why it prints its own mark rather than "ok".
MARK = {OK: "ok   ", WARN: "WARN ", FAIL: "FAIL ", UNKNOWN: "?    ", SKIPPED: "-    "}
# NOTE (0.18.1) is orientation: a fact the operator needs to have been told, that is neither
# healthy nor broken -- e.g. "repo-scoped commands here review the vault". It never raises the
# exit code and never prints "ok", so it cannot be read as a clean result either.
NOTE = "note"
MARK[NOTE] = "i    "


class Report:
    def __init__(self):
        self.rows = []

    def add(self, check, state, summary, detail="", fix=""):
        self.rows.append({"check": check, "state": state, "summary": summary,
                          "detail": detail, "fix": fix})

    @property
    def worst(self):
        for s in (FAIL, UNKNOWN, WARN):
            if any(r["state"] == s for r in self.rows):
                return s
        return OK


def run(cmd, timeout=120):
    """-> (returncode, output). Never raises: a check that dies is UNKNOWN, not clean."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def vault_path(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("GT_VAULT"):
        return Path(os.environ["GT_VAULT"])
    try:
        return Path(json.loads(CONFIG.read_text(encoding="utf-8"))["vault_path"])
    except Exception:
        return None


def read_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def gt_src_path():
    """The publish destination, from $GT_SRC or `gt_src` in vault-config.json.

    Never guessed: only a maintainer who publishes has one, and a default path would
    be one person's machine shipped to everyone.
    """
    if os.environ.get("GT_SRC"):
        return Path(os.environ["GT_SRC"]).expanduser()
    value = read_config().get("gt_src")
    return Path(value).expanduser() if isinstance(value, str) and value.strip() else None


def plugin_root(explicit=None):
    """Where the plugin SOURCE lives.

    Read from the wiring rather than guessed: install.sh registers gt_version_check
    with the plugin root as an argument, so settings.json already knows. A guess here
    would be the same class of error as a version check pinned to the wrong release.
    """
    if explicit:
        return Path(explicit)
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return None
    for blocks in (data.get("hooks") or {}).values():
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                cmd = h.get("command") or ""
                if "gt_version_check.py" in cmd:
                    parts = shlex.split(cmd)
                    for i, tok in enumerate(parts):
                        if tok == "check" and i + 1 < len(parts):
                            return Path(parts[i + 1])
    return None


CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache" / MARKET_NAME


def source_present(root):
    """True when `root` is a plugin source tree that still holds an installable gt release."""
    return bool(root) and version_dir(root) is not None


def resolve_release(root):
    """-> (module root, release dir, source_gone) for the plain doctor (0.20.1).

    The release every answer is relative to is the one INSTALLED (a deliberate rollback
    included), not the newest directory in the tree: comparing a rolled-back 0.19.2 against
    0.20.0's files reported wiring FAIL and drift on a healthy rollback (usability run M3).
    Its copy in the source tree is preferred; else the installed copy in the plugin cache.

    And the source tree may be gone (INSTALL.md option C installs from a temporary folder):
    then the cache is both the release and the module root, as the SessionStart component
    check already did -- until 0.20.1 the doctor printed `?` rows there and exited 2 (M7)."""
    inst = installed_release()
    if source_present(root):
        if inst is not None:
            in_tree = Path(root) / "golden-thread" / inst.name
            if (in_tree / ".claude-plugin" / "plugin.json").is_file():
                return root, in_tree, False
            return root, inst, False
        return root, version_dir(root), False
    if inst is not None:
        return CACHE_ROOT, inst, True
    return root, None, bool(root)


def version_dir(root):
    """The newest installable version directory, the way install.sh picks it."""
    if not root:
        return None
    best = None
    for d in (root / "golden-thread").glob("*/"):
        name = d.name.rstrip("/")
        if not (d / ".claude-plugin" / "plugin.json").is_file():
            continue
        try:
            key = tuple(int(x) for x in name.split("."))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


# ---- the checks -------------------------------------------------------------

def check_version(rep, root):
    script = INSTALLED_HOOKS / "gt_version_check.py"
    if not script.is_file() or not root:
        rep.add("version", UNKNOWN, "cannot locate the plugin source",
                fix="run install.sh, then restart Claude Code")
        return
    rc, out = run([sys.executable, str(script), "check", str(root)])
    if rc is None:
        rep.add("version", UNKNOWN, "version check did not run", out)
    elif rc not in (0, 1) or "Traceback" in out or not out.strip():
        # gt_version_check is advisory too (main returns 0), so rc says little -- but
        # a usage error or a crash is not a version answer.
        rep.add("version", UNKNOWN, "the version check could not run", out.strip()[-800:])
    elif "version: unknown" in out or "not readable" in out:
        # It says so itself: nothing installed could be compared. Not clean, not drift.
        rep.add("version", UNKNOWN,
                next((l.strip() for l in out.splitlines() if "unknown" in l), out.strip()),
                fix="run install.sh, then restart Claude Code")
    elif "current" in out:
        rep.add("version", OK, out.strip().splitlines()[-1].strip())
    else:
        line = next((l.strip() for l in out.splitlines() if "installed" in l), out.strip())
        rep.add("version", WARN, line,
                fix='bash "%s/install.sh"   (then restart Claude Code)' % root)


# gt_components' drift vocabulary, split by what a person can do about it. Read from
# its `report()` output, NOT from its exit code: `gt_components.py check` ends with
# `return 0  # never a failing exit: advisory only`, because it also runs as a
# SessionStart hook where a failing exit would be wrong. Deciding from that exit code
# -- which this file did until 0.16.5 -- made every drift row invisible: a wall of
# `missing packs/core/*.pack.json` exited 0 and was rendered here as "installed files
# match <ver>", with the drift text discarded. Found when `version` said 0.16.3 was
# installed while `components` said the files matched 0.16.4; the files agreed with
# `version`.
COMP_ACTIONABLE = ("stale", "missing")            # gt_components.py apply fixes these
COMP_BLOCKED = ("differs", "ahead", "no-manifest")  # never auto-applied: needs a person
COMP_BENIGN = ("extra",)                          # installed, absent from source
COMP_WIRING = ("unwired", "badpath")              # the wiring check owns the detail
COMP_STATES = COMP_ACTIONABLE + COMP_BLOCKED + COMP_BENIGN + COMP_WIRING


def _first_word(line):
    parts = line.strip().split(None, 1)
    return parts[0] if parts else ""


def _component_rows(out):
    """-> [(state, line)] for every row gt_components' report() printed.

    report() prints each row as "  %-9s %s" -- two leading spaces, then one of the
    state words. Anything else (headings, the resolve recipe, installer advice) is
    not a row and is left out of the tally.
    """
    rows = []
    for line in out.splitlines():
        if not line.startswith("  "):
            continue
        word = _first_word(line)
        if word in COMP_STATES:
            rows.append((word, line.strip()))
    return rows


def check_components(rep, vdir):
    script = INSTALLED_HOOKS / "gt_components.py"
    if not script.is_file() or not vdir:
        rep.add("components", UNKNOWN, "gt_components.py or the release is missing")
        return
    rc, out = run([sys.executable, str(script), "check", str(vdir)])
    if rc is None:
        rep.add("components", UNKNOWN, "component check did not run", out)
        return
    text = out.strip()
    if not text or "Traceback" in out:
        # report() prints nothing when the drift policy is "off". Silence is the one
        # thing this file refuses to read as clean.
        rep.add("components", UNKNOWN,
                "the component check printed nothing readable — that is not 'clean'",
                text[-800:],
                fix="check `component_updates` (gt_settings); 'off' silences the check")
        return
    rows = _component_rows(out)
    actionable = [l for s, l in rows if s in COMP_ACTIONABLE]
    blocked = [l for s, l in rows if s in COMP_BLOCKED]
    benign = [l for s, l in rows if s in COMP_BENIGN]
    wiring = [l for s, l in rows if s in COMP_WIRING]
    # The check is pinned to the version it was handed: say which, always.
    if "drifted from" not in text and not rows:
        if "clean" in text:
            rep.add("components", OK, "installed files match %s" % vdir.name,
                    "\n".join(benign))
        else:
            rep.add("components", UNKNOWN,
                    "the component check said neither clean nor drifted", text[-800:])
        return
    if not (actionable or blocked or wiring):
        # `extra` alone is benign: module-installed scripts (gt_report_card.py,
        # gt_watch.py) legitimately sit in the hooks dir without being in gt's own
        # source. Raising the state on those would warn forever on a healthy install.
        rep.add("components", OK,
                "installed files match %s (%d extra file(s), benign)"
                % (vdir.name, len(benign)), "\n".join(benign))
        return
    parts = []
    for label, group in (("actionable", actionable), ("blocked", blocked),
                         ("wiring", wiring)):
        if group:
            parts.append("%d %s" % (len(group), label))
    if benign:
        parts.append("%d extra (benign)" % len(benign))
    fix = ""
    if actionable and not blocked:
        fix = "python3 %s apply %s" % (script, vdir)
    elif actionable:
        fix = ("python3 %s apply %s  (the stale/missing rows only — the rest needs a "
               "person)" % (script, vdir))
    elif blocked:
        # gt_components: "`differs`/`ahead`/`extra` are never auto-applied — that would
        # revert or delete work that exists only here." So do not offer `apply`.
        fix = "resolve by hand: diff the installed copy against the plugin source"
    else:
        fix = 'bash "<plugin-repo>/install.sh"   (then restart Claude Code)'
    detail = actionable + blocked + wiring + benign
    if len(detail) > 25:
        # Whole rows, never a character slice: a detail cut mid-path reads as a file
        # name that does not exist.
        detail = detail[:25] + ["… and %d more row(s)" % (len(detail) - 25)]
    rep.add("components", WARN,
            "drift against %s — %s" % (vdir.name, ", ".join(parts)),
            "\n".join(detail), fix=fix)


def check_wiring(rep, vdir):
    script = INSTALLED_HOOKS / "gt_components.py"
    if not script.is_file() or not vdir:
        rep.add("wiring", UNKNOWN, "cannot check hook wiring")
        return
    rc, out = run([sys.executable, str(script), "wiring", str(vdir)])
    if rc is None:
        rep.add("wiring", UNKNOWN, "wiring check did not run", out)
        return
    if rc not in (0, 1) or "Traceback" in out:
        # `wiring` exits 0 clean, 1 with findings; anything else is a usage error or a
        # crash, whose output carries no rows at all — and "no rows" used to render as
        # "every declared hook is wired".
        rep.add("wiring", UNKNOWN, "the wiring check could not run", out.strip()[-800:])
        return
    # `badpath` ("wired, but a path argument does not exist on this machine") is
    # reported by gt_components beside `unwired` and was dropped here until 0.16.5:
    # a hook pinned to a path that does not exist was reported as "every declared hook
    # is wired". That is exactly the 2026-08-30 shape this file exists to end.
    bad = [l.strip() for l in out.splitlines()
           if _first_word(l) in ("unwired", "badpath")]
    unwired = [l for l in bad if l.startswith("unwired")]
    badpath = [l for l in bad if l.startswith("badpath")]
    if rc == 1 and not bad:
        rep.add("wiring", UNKNOWN,
                "the wiring check reported findings in a shape this doctor cannot read",
                out.strip()[-800:])
        return
    if not bad:
        line = next((l.strip() for l in out.splitlines() if "declared hooks are wired" in l),
                    None)
        if line is None:
            rep.add("wiring", UNKNOWN,
                    "the wiring check said nothing about whether hooks are wired",
                    out.strip()[-800:])
            return
        rep.add("wiring", OK, line)
        return
    parts = []
    if unwired:
        parts.append("%d not wired" % len(unwired))
    if badpath:
        parts.append("%d wired to a path that does not exist" % len(badpath))
    rep.add("wiring", FAIL, "declared hook(s): " + ", ".join(parts),
            "\n".join(unwired + badpath),
            fix="python3 %s/vault_init.py install-core-rules --vault <vault>"
                % (vdir / "scripts"))


# A Core rule declares HOW it is enforced; each mechanism is one hook script at one
# event. vault_init.py owns the wiring and compares `h["command"] == str(script)` --
# the same comparison made here, because "some hook is registered on that event" is
# not the same claim as "THIS rule's mechanism runs".
CORE_MECHANISM = {"reminder": ("UserPromptSubmit", "inject_core_rules.sh"),
                  "validated": ("Stop", "validate_response.sh")}


def _wired_commands(settings=SETTINGS):
    """-> {event: [command, ...]} from settings.json. Unreadable settings -> None,
    which is 'could not check', never 'nothing is wired'."""
    try:
        data = json.loads(Path(settings).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for event, blocks in (data.get("hooks") or {}).items():
        cmds = out.setdefault(event, [])
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                if isinstance(h, dict) and h.get("command"):
                    cmds.append(str(h["command"]))
    return out


def _frontmatter(text):
    """Flat key -> value of the YAML frontmatter, nested keys un-nested.

    Deliberately the same shallow read gt_lint does: Core rules carry `level` and
    `enforcement` under `metadata:`, and a real YAML parser is not available in the
    standard library."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()[1:]
    out = {}
    for line in lines:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key and all(c.isalnum() or c in "_-" for c in key):
            out[key] = value.strip().strip("\"'")
    return out


def core_rules_dir(vault):
    """Where the Core rules live: the canonical path, then the legacy one, else the
    folder holding the priority model (which is what names a core-rules folder, not its
    name alone).

    0.17.0: the canonical location is core-rules/ at the vault ROOT. The pre-0.17.0 path
    is still recognised so an un-migrated vault reports healthy rather than looking
    broken — doctor's job is to tell the truth about the vault it is given, not to
    insist on the newest layout.
    """
    model = "core_rule_priority_model.md"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gt_paths import default_core_rules, legacy_core_rules
        candidates = [default_core_rules(vault), legacy_core_rules(vault)]
    except Exception:
        candidates = [vault / "core-rules", vault / "Projects" / "golden-thread" / "core-rules"]
    for cand in candidates:
        if (cand / model).is_file():
            return cand
    for cand in sorted(vault.rglob("core-rules")):
        if (cand / model).is_file():
            return cand
    return next((c for c in candidates if c.is_dir()), None)


def _runs_script(command, path):
    """Does the settings.json `command` run the hook script at `path`?

    On Windows (0.19.2) it is judged as Git Bash will read it: the first word, with "/" and
    "\\" and case treated alike. vault_init now writes "C:/Users/..." there, and the
    0.19.1 form -- the bare C:\\Users\\... -- is NOT a match even though it equals str(path):
    the shell strips its backslashes and the hook never runs. Elsewhere unchanged."""
    try:
        first = (shlex.split(command) or [""])[0]
    except ValueError:
        first = ""
    if os.name == "nt":
        same = lambda a, b: os.path.normcase(os.path.normpath(a)) == \
            os.path.normcase(os.path.normpath(b))                     # noqa: E731
        return bool(first) and same(first, str(path))
    return command == str(path) or first == str(path)


def check_core_rules(rep, vault):
    """Does each rule that CLAIMS enforcement have its own mechanism wired?

    Storing a rule is not enforcing it -- that is the whole Core tier. But until
    0.16.5 nothing in the system verified a specific rule's mechanism: gt_lint's
    `core-unenforced` only asks whether SOME hook is registered on the event, so a
    `Stop` entry belonging to anything at all made every validated rule look enforced.
    This asks the question vault_init.py answers when it wires them: is THIS script,
    at the installed path, on that event -- and does the file exist?

    Scope is the core-rules folder. A rule filed elsewhere is gt_lint's
    `core-misplaced`, a different finding about a different mistake."""
    if not vault:
        rep.add("core-rules", UNKNOWN, "no vault configured",
                fix="run /gt:gt-init, or set GT_VAULT")
        return
    if not vault.is_dir():
        rep.add("core-rules", UNKNOWN, "configured vault does not exist: %s" % vault)
        return
    cdir = core_rules_dir(vault)
    if cdir is None:
        rep.add("core-rules", SKIPPED, "this vault has no core-rules/ — nothing claims "
                                       "enforcement")
        return
    wired = _wired_commands()
    if wired is None:
        rep.add("core-rules", UNKNOWN, "~/.claude/settings.json is missing or unreadable "
                                       "— cannot tell what is wired")
        return
    rules, problems, lines = 0, [], []
    for md in sorted(cdir.rglob("*.md")):
        try:
            fm = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if fm.get("level") != "core":
            continue
        rules += 1
        enf = fm.get("enforcement")
        if not enf:
            problems.append(md.name)
            lines.append("%s — level: core with NO enforcement declared" % md.name)
            continue
        if enf not in CORE_MECHANISM:
            problems.append(md.name)
            lines.append("%s — enforcement: %s is not a mechanism this doctor knows "
                         "(%s)" % (md.name, enf, "/".join(sorted(CORE_MECHANISM))))
            continue
        event, script = CORE_MECHANISM[enf]
        path = INSTALLED_HOOKS / script
        # Tolerant about arguments, strict about the script: vault_init writes the bare
        # path, but an entry that adds a flag still runs the right mechanism.
        found = any(_runs_script(c, path) for c in wired.get(event, []))
        if not found:
            problems.append(md.name)
            others = len(wired.get(event, []))
            lines.append("%s — enforcement: %s, but no %s hook runs %s%s"
                         % (md.name, enf, event, path,
                            " (%d other %s hook(s) are wired — a different mechanism)"
                            % (others, event) if others else ""))
        elif not path.is_file():
            problems.append(md.name)
            lines.append("%s — enforcement: %s, %s is wired to %s which does not exist"
                         % (md.name, enf, event, path))
        else:
            lines.append("%s — enforcement: %s, %s runs %s" % (md.name, enf, event, script))
    if not rules:
        rep.add("core-rules", SKIPPED, "%s holds no rule declaring level: core" % cdir.name,
                "\n".join(lines))
        return
    if problems:
        rep.add("core-rules", FAIL,
                "%d of %d Core rule(s) are stored but not enforced" % (len(problems), rules),
                "\n".join(lines),
                fix="python3 <plugin>/scripts/vault_init.py install-core-rules --vault %s"
                    % vault)
    else:
        # Counted the way the injector and the docs count (0.20.1): the priority MODEL is
        # level: core but describes how rules rank -- it is not itself a rule. "11 Core rules"
        # here beside "10 rules injected" in smoke-rules read as one going missing.
        model = 1 if (cdir / "core_rule_priority_model.md").is_file() else 0
        rep.add("core-rules", OK,
                "%d Core rule(s)%s, each with its mechanism wired"
                % (rules - model, " and the priority model" if model else ""),
                "\n".join(lines))


def _components_module():
    """The gt_components beside this file, else the installed one; None if neither has
    the module reader (an install older than 0.14.0)."""
    import importlib.util
    for path in (HERE / "gt_components.py", INSTALLED_HOOKS / "gt_components.py"):
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("gt_components_doctor", str(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            continue
        if hasattr(mod, "module_detail"):
            return mod
    return None


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def check_model_policy(rep, vdir):
    """The active model profile, and whether every installed skill still carries exactly what
    the policy wrote (0.19.1). A hand edit to a field the policy wrote is drift; the policy's
    own fields are not -- the release source never has them, so this is the only check."""
    cands = [HERE / "gt_model_policy.py"]
    if vdir:
        cands.append(Path(vdir) / "scripts" / "gt_model_policy.py")
    script = next((c for c in cands if c.is_file()), None)
    if script is None:
        rep.add("model-policy", SKIPPED, "gt_model_policy.py (0.19.1+) is not part of this release")
        return
    import subprocess
    def run(*args):
        p = subprocess.run([sys.executable, "-B", str(script)] + list(args),
                           capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    try:
        _rc, prof = run("current")
        rc, out = run("verify")
    except Exception as exc:
        rep.add("model-policy", UNKNOWN, "could not run gt_model_policy.py (%s)"
                % exc.__class__.__name__)
        return
    prof = prof.strip() or "inherit (none recorded)"
    drift = [l.strip()[len("DRIFT "):] for l in out.splitlines() if l.strip().startswith("DRIFT ")]
    if rc == 0:
        rep.add("model-policy", OK, "profile %s; every installed skill as the policy wrote it" % prof)
    else:
        rep.add("model-policy", WARN, "profile %s; %d installed skill field(s) edited by hand"
                % (prof, len(drift)), "\n".join(drift),
                fix="python3 %s apply   (or keep the edit as an override: ... set --skill S --model M)"
                    % script)


def check_modules(rep, root, vdir):
    """Each module: on/off and why, version, requires_gt, and -- when on -- whether its
    plugin is registered in installed_plugins.json and enabled in settings.json.

    OFF is a choice, reported as "not installed by choice", never as drift. Read-only."""
    gc = _components_module()
    if gc is None:
        rep.add("modules", UNKNOWN, "no gt_components.py with the module reader (gt < 0.14.0?)",
                fix="run install.sh")
        return
    if not root:
        rep.add("modules", UNKNOWN, "cannot locate the plugin source",
                fix="run install.sh, then restart Claude Code")
        return
    try:
        det = gc.module_detail(str(root), str(Path.home()),
                               gt_version=vdir.name if vdir else None)
    except Exception as exc:
        rep.add("modules", UNKNOWN, "module states could not be read", str(exc))
        return
    if not det:
        rep.add("modules", OK, "no modules in %s" % root)
        return
    installed = (_read_json(INSTALLED_PLUGINS).get("plugins") or {})
    enabled = (_read_json(SETTINGS).get("enabledPlugins") or {})
    lines, problems, fixes = [], [], set()
    # The cache is not where install.sh lives: name the placeholder, not a wrong path.
    installer = ("<plugin-repo>" if Path(root) == CACHE_ROOT else root)
    gtv = vdir.name if vdir else "?"
    for name, d in sorted(det.items()):
        key = "%s@%s" % (d.get("plugin"), MARKET_NAME)
        present = bool(installed.get(key)) if isinstance(installed, dict) else False
        on_flag = bool(enabled.get(key)) if isinstance(enabled, dict) else False
        req = d.get("requires_gt") or "?"
        admitted = {True: "admits gt %s" % gtv, False: "does NOT admit gt %s" % gtv,
                    None: "not evaluated"}[d.get("admitted")]
        head = "%-10s %-4s %-9s requires_gt %s (%s)" % (name, d["state"], d.get("version") or "?",
                                                         req, admitted)
        if not d.get("valid"):
            problems.append(name)
            lines.append(head + " — invalid module.json: " + "; ".join(d.get("reasons") or []))
            continue
        if d["state"] == "on":
            miss = [w for w, ok in (("not in installed_plugins.json", present),
                                    ("not enabled in settings.json", on_flag)) if not ok]
            if miss:
                problems.append(name)
                fixes.add('bash "%s/install.sh"' % installer)
                lines.append(head + " — plugin %s %s" % (key, " and ".join(miss)))
            else:
                lines.append(head + " — plugin %s installed and enabled" % key)
        else:
            why = d.get("reason") or ""
            if why.startswith("requires gt"):
                lines.append(head + " — off: " + why)
            else:
                lines.append(head + " — not installed by choice (%s)" % why)
            if present or on_flag:
                problems.append(name)
                fixes.add('bash "%s/install.sh"' % installer)
                lines[-1] += "; but plugin %s is still %s" % (
                    key, "installed" if present else "enabled")
    n_on = sum(1 for d in det.values() if d["state"] == "on")
    summary = "%d module(s): %d on, %d off" % (len(det), n_on, len(det) - n_on)
    if problems:
        rep.add("modules", WARN, summary + " — %d need attention" % len(problems),
                "\n".join(lines), fix=" ; ".join(sorted(fixes)) or "fix the module.json")
    else:
        rep.add("modules", OK, summary, "\n".join(lines))


def _load_script(name, *dirs):
    """Import scripts/<name> from the first dir that has it; None when none loads."""
    import importlib.util
    for d in dirs:
        if not d:
            continue
        path = Path(d) / name
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "%s_doctor" % path.stem, str(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except (Exception, SystemExit):
            # SystemExit too: a script that exits at import (a broken or stubbed copy) must
            # read as "could not load", not end the doctor with no report at all.
            continue
    return None


def _upgrade_module(vdir):
    """gt_upgrade from the release, never from the hooks dir.

    It reads the release's templates/ (its merge bases) relative to itself, so a copy loaded
    from anywhere but a release tree would judge the vault against nothing. HERE counts only
    when it IS a release's scripts/ dir -- the tests, and a doctor run from the source."""
    here = HERE if (HERE.parent / "templates").is_dir() else None
    return _load_script("gt_upgrade.py", vdir / "scripts" if vdir else None, here)


def check_vault(rep, vault, vdir=None):
    """Delegates to gt_upgrade (0.17.2). This used to hand-code the 0.11.0 migrations --
    log and decisions spool, nothing later -- so it missed every migration since, never said
    what release the vault was stamped at, and called a project born in the spool format
    unmigrated (audit 2026-09-28: the vault stamped 0.16.5 under 0.17.1 read as clean, and
    `notifications` read as pending). Now the doctor and /gt:gt-upgrade cannot disagree."""
    if not vault:
        rep.add("vault", UNKNOWN, "no vault configured",
                fix="run /gt:gt-init, or set GT_VAULT")
        return
    if not vault.is_dir():
        rep.add("vault", FAIL, "configured vault does not exist: %s" % vault)
        return
    up = _upgrade_module(vdir)
    if up is None or not all(hasattr(up, f) for f in ("plan", "read_stamp",
                                                         "release_version")):
        rep.add("vault", UNKNOWN, "%s — could not load gt_upgrade from the release, so "
                "pending migrations are unknown" % vault.name,
                fix="pass --plugin-root, or re-run install.sh so settings.json names it")
        return
    try:
        stamped = up.read_stamp(vault).get("gt")
        release = up.release_version()
        todo = up.plan(vault)
        attention = up.attention(vault) if hasattr(up, "attention") else []
    except Exception as exc:
        rep.add("vault", UNKNOWN, "%s — gt_upgrade failed (%s)"
                % (vault.name, exc.__class__.__name__), str(exc)[-400:])
        return
    stamp = "stamped %s, release %s" % (stamped or "NEVER", release)
    lines = ["[%s] %s: %s" % (ver, mid, reason) for ver, mid, _what, reason in todo]
    lines += ["needs you: %s" % item for item in attention]
    fix = ("/gt:gt-upgrade migrates and restamps the vault -- or gt_upgrade.py run --vault "
           "%s --dry-run, then without --dry-run" % vault)
    if any(reason.startswith("could not determine") for _v, _m, _w, reason in todo):
        rep.add("vault", UNKNOWN, "%s — %s; a migration check could not run"
                % (vault.name, stamp), "\n".join(lines), fix=fix)
    elif todo or attention:
        rep.add("vault", WARN, "%s — %s; %d pending migration(s), %d item(s) need you"
                % (vault.name, stamp, len(todo), len(attention)), "\n".join(lines), fix=fix)
    elif not stamped or up.vkey(stamped) < up.vkey(release):
        # Nothing to migrate, but the stamp is what the NEXT upgrade reads to decide what
        # has run. Behind is worth one line and a WARN; it is not a broken vault.
        rep.add("vault", WARN, "%s — %s; nothing pending, the stamp is behind"
                % (vault.name, stamp), fix=fix)
    else:
        rep.add("vault", OK, "%s — %s; nothing pending" % (vault.name, stamp))


def _rmtree(path):
    """shutil.rmtree that also removes read-only files. Native Windows will not delete a
    read-only file -- and git writes its objects read-only -- so every smoke row's throwaway
    vault was left behind in TEMP there (0.20.0). Never raises."""
    import shutil
    import stat as _stat

    def _writable_retry(func, p, _exc):
        try:
            os.chmod(p, _stat.S_IWRITE | _stat.S_IREAD)
            func(p)
        except OSError:
            pass
    try:
        if sys.version_info >= (3, 12):                 # onerror is deprecated there
            shutil.rmtree(str(path), onexc=_writable_retry)
        else:
            shutil.rmtree(str(path), onerror=_writable_retry)
    except OSError:
        pass


def check_schedule(rep):
    """Every INSTALLED gt_schedule job: loaded, script present, last exit normal. (0.17.2)

    Nothing reported a failing scheduled job. On 2026-09-28 com.markethaven.gt-lint-weekly
    had died with PermissionError on three Mondays running and nothing surfaced it -- the one
    check that existed (`gt_schedule.py check`) had to be run by hand, and it called the crash
    code normal. This runs gt_schedule's own job_status(), so "normal exit" is judged by the
    BENIGN_EXITS table the scheduler keeps, never a copy of it.

    A job with no plist is a choice, not a finding. Not macOS, or gt_schedule missing, is
    UNKNOWN or SKIPPED -- never clean."""
    import shutil as _sh
    # Windows (0.20.0): the same jobs on Task Scheduler, judged by the same job_status().
    # Linux (0.20.1): systemd --user timers or tagged crontab lines (gt_schedule.platform_kind).
    windows = os.name == "nt"
    sched = _load_script("gt_schedule.py", HERE, INSTALLED_HOOKS)
    linux_ok = sched is not None and hasattr(sched, "platform_kind")
    if sys.platform != "darwin" and not windows and not linux_ok:
        rep.add("schedule", SKIPPED, "scheduled jobs are launchd agents or Windows tasks; "
                "this is neither macOS nor Windows")
        return
    if sched is None or not hasattr(sched, "job_status"):
        rep.add("schedule", UNKNOWN, "gt_schedule.py (0.17.2+) is not installed beside the "
                "doctor", fix="re-run install.sh")
        return
    try:
        jobs = sched.installed_jobs()
    except Exception as exc:
        rep.add("schedule", UNKNOWN, "could not list the installed jobs (%s)"
                % exc.__class__.__name__)
        return
    if not jobs:
        rep.add("schedule", SKIPPED, "no gt job is installed (gt_schedule.py list)")
        return
    if windows:
        tools = ("schtasks",)
    elif sys.platform == "darwin":
        tools = ("launchctl",)
    else:
        tools = ("systemctl", "crontab")
    if not any(_sh.which(t) for t in tools):
        rep.add("schedule", UNKNOWN, "%d job(s) installed but %s is not on PATH, so "
                "their state is unknown" % (len(jobs), " / ".join(tools)))
        return
    bad, lines = [], []
    for job in jobs:
        try:
            code, problems = sched.job_status(job)
        except Exception as exc:
            problems, code = ["could not be checked (%s)" % exc.__class__.__name__], None
        label = getattr(sched, "installed_label", sched.label_for)(job)
        if problems:
            bad.append(job)
            lines += ["%s: %s" % (label, p) for p in problems]
        else:
            lines.append("%s: last exit %s" % (label, code if code is not None
                                                else "(not yet run)"))
        py = getattr(sched, "job_interpreter", lambda j: None)(job)
        if py:
            lines[-1] += " · runs %s" % py          # one recorded interpreter (0.19.1)
    if bad:
        rep.add("schedule", FAIL, "%d of %d installed job(s) failing: %s"
                % (len(bad), len(jobs), ", ".join(bad)), "\n".join(lines),
                fix="read the job's .err log named above; then python3 %s check <job>"
                    % (INSTALLED_HOOKS / "gt_schedule.py"))
    else:
        rep.add("schedule", OK, "%d installed job(s), each loaded and last exiting normally"
                % len(jobs), "\n".join(lines))


def check_workers(rep):
    script = INSTALLED_HOOKS / "gt_workers.py"
    if not script.is_file():
        rep.add("workers", UNKNOWN, "gt_workers.py is not installed")
        return
    rc, out = run([sys.executable, str(script), "check"])
    if rc is None:
        rep.add("workers", UNKNOWN, "worker check did not run", out)
        return
    if not out.strip() or "Traceback" in out or rc not in (0, 1):
        # A worker check that says nothing is the failure gt_workers' own clean line
        # exists to prevent; it is never "no stray workers".
        rep.add("workers", UNKNOWN, "the worker check could not run",
                out.strip()[-800:])
        return
    line = out.strip().splitlines()[0]
    if "NOT CHECKED" in line:
        # 0.20.0: gt_workers could not read the process table (no `ps -eo` on native Windows).
        # Not checked is neither clean nor a stray to reap -- and on native Windows it is the
        # platform, not a failure, so it is SKIPPED and says so (0.20.1: as UNKNOWN it made
        # every Windows doctor run exit 2, usability run M8).
        if os.name == "nt":
            rep.add("workers", SKIPPED, "not supported on Windows (no POSIX process table) — "
                    "check Task Manager for stray bash.exe / python.exe processes")
        else:
            rep.add("workers", UNKNOWN, line.strip(), _whole_lines(out))
        return
    if "clean" in line:
        rep.add("workers", OK, line.strip())
    else:
        rep.add("workers", WARN, line.strip(), _whole_lines(out),
                fix="python3 %s reap --dry-run   (then without --dry-run)" % script)


def _whole_lines(text, limit=12):
    """At most `limit` whole lines of a sub-check's output, then a count -- never a cut
    mid-line, which read as a path or a command that does not exist (0.20.1)."""
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    if len(lines) > limit:
        lines = lines[:limit] + ["… and %d more line(s)" % (len(lines) - limit)]
    return "\n".join(lines)


def check_push(rep):
    script = INSTALLED_HOOKS / "gt_push_check.py"
    if not script.is_file():
        rep.add("push", UNKNOWN, "gt_push_check.py is not installed")
        return
    rc, out = run([sys.executable, str(script), "check"])
    if rc is None:
        rep.add("push", UNKNOWN, "push check did not run", out)
        return
    text = out.strip()
    first = text.splitlines()[0].strip() if text else ""
    if not text or "Traceback" in out or rc not in (0, 1):
        rep.add("push", UNKNOWN, "the push check could not run", text[-800:])
    elif "in sync" in text:
        rep.add("push", OK, first)
    elif "no remote configured" in first:
        # 0.20.1: a vault with no remote is a choice, not a finding (M8).
        rep.add("push", NOTE, first, "\n".join(text.splitlines()[1:]))
    elif "nothing to check" in text:
        # No vault repo, or no vault: the question does not apply here.
        rep.add("push", SKIPPED, first)
    elif "skipped." in text:
        # git missing, git timed out, branch unreadable: it could not answer.
        rep.add("push", UNKNOWN, first)
    else:
        rep.add("push", WARN, first, _whole_lines(text),
                fix=next((l.split(":", 1)[1].strip() for l in text.splitlines()
                          if l.strip().startswith(("push with:", "set one with:", "fetch first:"))),
                         "git -C <vault> push"))


def check_gt_src(rep):
    """Does the publish destination still hold exactly what was published?

    2026-09-11: a flat 0.9.13-era scripts/ and templates/ appeared in gt-src after a
    publish, written by something other than the sync script. The next sync deleted
    them in output that scrolled past. A foreign file in the destination is a file the
    OTHER machine may commit, so it is worth naming between syncs, not at the next one.
    """
    dest = gt_src_path()
    if dest is None:
        rep.add("gt-src", SKIPPED,
                "not configured (no $GT_SRC, no gt_src in vault-config.json) — skipped")
        return
    if not dest.is_dir():
        rep.add("gt-src", WARN, "configured publish destination does not exist: %s" % dest,
                fix="correct gt_src in ~/.claude/vault-config.json (or $GT_SRC), or remove it")
        return
    source_json = dest / "SOURCE.json"
    if not source_json.is_file():
        rep.add("gt-src", WARN, "%s has no SOURCE.json — provenance unknown" % dest.name,
                fix="dev/sync-gt-src.sh --dry-run")
        return
    try:
        meta = json.loads(source_json.read_text(encoding="utf-8"))
    except Exception as exc:
        rep.add("gt-src", UNKNOWN, "SOURCE.json is unreadable", str(exc))
        return
    # Since 0.17.3 the publisher writes SHA256SUMS: the exact list of what it published, hashed
    # from the commit. Checking against it replaces the name-guessing below, which knew only the
    # old plugin-only layout and would have called the repo root (docs/, .github/, LICENSE,
    # golden-thread-plugin/) "unmanaged" the moment gt-src took the repository's layout.
    sums = dest / "SHA256SUMS"
    if sums.is_file():
        import hashlib
        try:
            listed = {}
            for line in sums.read_text(encoding="utf-8").splitlines():
                digest, _, name = line.partition("  ")
                if name:
                    listed[name[2:] if name.startswith("./") else name] = digest
        except (OSError, UnicodeDecodeError) as exc:
            rep.add("gt-src", UNKNOWN, "SHA256SUMS is unreadable", str(exc))
            return
        present, changed, missing = set(), [], []
        for f in dest.rglob("*"):
            if not f.is_file() or f.name == ".DS_Store":
                continue
            rel = f.relative_to(dest).as_posix()
            if rel in ("SHA256SUMS", "SOURCE.json"):
                continue
            present.add(rel)
            want = listed.get(rel)
            if want is not None:
                try:
                    if hashlib.sha256(f.read_bytes()).hexdigest() != want:
                        changed.append(rel)
                except OSError:
                    changed.append(rel)
        foreign = sorted(present - set(listed))
        missing = sorted(set(listed) - present)
        if foreign or changed or missing:
            parts = []
            if changed:
                parts.append("%d changed since publish: %s" % (len(changed), ", ".join(changed[:5])))
            if missing:
                parts.append("%d missing: %s" % (len(missing), ", ".join(missing[:5])))
            if foreign:
                parts.append("%d not written by sync-gt-src.sh: %s"
                             % (len(foreign), ", ".join(foreign[:5])))
            rep.add("gt-src", WARN, "gt-src no longer matches its SHA256SUMS", "; ".join(parts),
                    fix="inspect, then dev/sync-gt-src.sh (it backs the destination up first)")
        else:
            rep.add("gt-src", OK, "published from %s (gt %s): all %d files match SHA256SUMS"
                    % (str(meta.get("commit", "?"))[:9], meta.get("gt", "?"), len(listed)))
        return

    # Top-level entries the publisher never creates are the signal worth reporting.
    expected_top = {"MANIFEST.json", "SOURCE.json", ".gitignore", "dev", "tests",
                    "install.sh", "selftest.sh", "package.sh", "build-docs.py"}

    # A plugin dir is published whatever it is called: the rule dev/plugins.py applies (a
    # directory holding a <version>/.claude-plugin/plugin.json). A literal list predated
    # modules and flagged golden-thread-demo, and would flag every module after a publish.
    def is_plugin_dir(p):
        try:
            return p.is_dir() and any((v / ".claude-plugin" / "plugin.json").is_file()
                                      for v in p.iterdir() if v.is_dir())
        except OSError:
            return False

    foreign = sorted(p.name for p in dest.iterdir()
                     if p.name not in expected_top and not is_plugin_dir(p)
                     and not p.name.endswith(
                         (".md", ".html", ".pdf", ".code-workspace")))
    if foreign:
        rep.add("gt-src", WARN,
                "%d unmanaged top-level entr%s in gt-src"
                % (len(foreign), "y" if len(foreign) == 1 else "ies"),
                "not written by sync-gt-src.sh: " + ", ".join(foreign),
                fix="inspect, then dev/sync-gt-src.sh (it backs the destination up first)")
    else:
        rep.add("gt-src", OK, "published from %s (gt %s), nothing unmanaged"
                % (str(meta.get("commit", "?"))[:9], meta.get("gt", "?")))


def check_lint(rep, vault):
    script = INSTALLED_HOOKS / "gt_lint.py"
    if not script.is_file() or not vault:
        rep.add("lint", UNKNOWN, "gt_lint.py or the vault is missing")
        return
    rc, out = run([sys.executable, str(script), str(vault)], timeout=300)
    if rc is None:
        rep.add("lint", UNKNOWN, "lint did not run", out)
        return
    findings = [l for l in out.splitlines() if l.startswith("[")]
    kinds = {}
    for f in findings:
        kinds[f.split("]")[0][1:]] = kinds.get(f.split("]")[0][1:], 0) + 1
    if not findings:
        # "no [ lines" was read as "no findings", so a linter that died on line one --
        # exit 2, a traceback, an unreadable vault -- reported the vault clean.
        if rc in (0, 1) and "Traceback" not in out and "healthy" in out:
            rep.add("lint", OK, "no findings")
        else:
            rep.add("lint", UNKNOWN, "the linter produced no report",
                    out.strip()[-800:])
    else:
        top = ", ".join("%s×%d" % (k, v) for k, v in
                        sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
        rep.add("lint", WARN, "%d finding(s): %s" % (len(findings), top),
                fix="/gt:gt-lint for the detail")


def check_astgrep(rep):
    """Is the optional structural matcher here, and NEW ENOUGH? (check: astgrep)

    Three states, three different fixes, so they are reported as three different things:

      present and current   ok, with the version
      present but TOO OLD   WARN -- this one IS a problem, because it looks installed. An older
                            ast-grep supports fewer languages, so a rule targeting one it lacks
                            never matches and nothing says the language was missing rather than
                            the code clean. Coverage claimed and not delivered.
      absent                ok. gt is stdlib-default and reports such rules SKIPPED rather than
                            quietly passing, so nothing is broken -- warning about a deliberate,
                            working configuration is how a health check teaches people to
                            ignore it.

    The asymmetry is deliberate: absent is a choice, stale is a trap.
    """
    scripts = os.path.dirname(os.path.abspath(__file__))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import gt_scan_code
    except Exception as exc:
        rep.add("astgrep", UNKNOWN, "could not load gt_scan_code (%s)" % exc.__class__.__name__)
        return
    path, version, problem = gt_scan_code.astgrep_status()
    if not problem:
        rep.add("astgrep", OK, "ast-grep %s (%s)" % (version, path))
    elif version:                       # found, but not usable -- it LOOKS installed
        rep.add("astgrep", WARN, problem)
    else:
        rep.add("astgrep", OK, "not installed — structural rules are SKIPPED, not silently "
                               "passed. %s" % astgrep_install_hint())


def astgrep_install_hint():
    """How to install ast-grep on THIS platform (0.20.1: Homebrew was named on Linux and
    Windows too)."""
    if sys.platform == "darwin":
        return "brew install ast-grep (or npm install -g @ast-grep/cli)"
    return "npm install -g @ast-grep/cli (or cargo install ast-grep --locked)"


# ---- post-install: the release gate -----------------------------------------
#
# A profile over the checks above plus the ones below. Each row is PASS, FAIL, WARN, INFO or
# PENDING; only FAIL fails the gate. "Could not run" is FAIL, never PASS -- the same rule as
# the rest of this file, made stricter because this is the one report whose answer is "ship".

PASS, PFAIL, PWARN, INFO, PENDING = "PASS", "FAIL", "WARN", "INFO", "PENDING"
STAGES = ("install", "session", "final")
RULE1_FILE = "core_concurrent_session_claim.md"
RULE1_PREFIX = "Write vault content only through the write queue"
QUEUE_SCRIPTS = ("gt_write_queue.py", "gt_broker.py", "gt_demote.py", "gt_daily.py")
LOTR_MODULE = "lotr"
# Rows whose answer depends on the VAULT's state rather than on what install.sh put on disk. A
# vault upgrade that failed or could not run during the install leaves them wrong; that is the
# vault waiting for /gt:gt-upgrade, not a broken install, and "failures never fail the install"
# holds. So before the final stage a FAIL here is PENDING, never a reason for install.sh's exit 9.
VAULT_ROWS = ("rule1-vault", "rule1-injected", "vault", "queue")
UPGRADE_STEP = ("run /gt:gt-upgrade in Claude Code (or gt_upgrade.py run --vault V), restart "
                "Claude Code, then gt_doctor.py post-install")


class Gate:
    def __init__(self, stage="final"):
        self.rows = []
        self.stage = stage

    def add(self, row, state, summary, fix=""):
        self.rows.append({"row": row, "state": state, "summary": summary,
                          "fix": fix if state in (PFAIL, PWARN, PENDING) else ""})

    def needs_upgrade(self, row, summary, fix=UPGRADE_STEP):
        """A row that cannot be true until /gt:gt-upgrade has run: PENDING before the
        upgrade is expected to have happened, FAIL at the gate itself."""
        self.add(row, PFAIL if self.stage == "final" else PENDING, summary, fix)

    def defer_vault_rows(self):
        if self.stage == "final":
            return
        for r in self.rows:
            if r["row"] in VAULT_ROWS and r["state"] == PFAIL:
                r["state"] = PENDING
                r["summary"] = "vault not upgraded yet: " + r["summary"]
                r["fix"] = (r["fix"] + " ; then " if r["fix"] and r["fix"] != UPGRADE_STEP
                            else "") + UPGRADE_STEP

    @property
    def failed(self):
        return any(r["state"] == PFAIL for r in self.rows)


def release_dir(explicit, root):
    """The release this gate answers for. Explicit wins; then this file's own release when it
    runs from a directory named as a version (a source tree or the cache, not the hooks dir or
    the marketplace's `gt/`); then the installed release; then the newest under the root."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if (p / ".claude-plugin" / "plugin.json").is_file() else None
    if (HERE.parent / ".claude-plugin" / "plugin.json").is_file():
        # 0.19.1: only a directory NAMED as a version is a release. The marketplace copy sits
        # in `plugins/gt/`, and taking `gt` for the release failed components against it while
        # the cache copy of the same install passed. Ask the install record instead.
        if _VERSION_NAME.match(HERE.parent.name):
            return HERE.parent
        return installed_release() or version_dir(root) or HERE.parent
    # The hooks-dir doctor (0.20.1): the INSTALLED release -- its copy in the source tree when
    # the tree still has it, else the cached copy -- so a rollback is judged against what it
    # installed and a deleted source tree (INSTALL.md option C) still has a release to answer
    # for. Until 0.20.1 this was the newest dir under the root, and None once the root was gone,
    # which failed the whole gate with "no release directory found" (usability run M7).
    _root, rel, _gone = resolve_release(root)
    return rel


_VERSION_NAME = __import__("re").compile(r"\A\d+\.\d+\.\d+\Z")


def installed_release():
    """The release Claude Code has installed for gt, from installed_plugins.json, or None."""
    try:
        data = json.loads(INSTALLED_PLUGINS.read_text(encoding="utf-8"))
    except Exception:
        return None
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    for key, entries in (plugins if isinstance(plugins, dict) else {}).items():
        if not key.startswith("gt@"):
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            p = Path(str((e or {}).get("installPath") or "")).expanduser()
            if _VERSION_NAME.match(p.name) and (p / ".claude-plugin" / "plugin.json").is_file():
                return p
    return None


def _fold(gate, rep, check, row=None, ok_states=(OK,), warn_as=PFAIL):
    """Turn one existing doctor row into a gate row. UNKNOWN is 'could not run' -> FAIL."""
    r = next((x for x in rep.rows if x["check"] == check), None)
    name = row or check
    if r is None:
        gate.add(name, PFAIL, "could not run: the %s check produced no row" % check)
        return None
    if r["state"] in ok_states:
        gate.add(name, PASS, r["summary"])
    elif r["state"] == UNKNOWN:
        gate.add(name, PFAIL, "could not run: " + r["summary"], r["fix"] or "re-run install.sh")
    elif r["state"] == WARN and warn_as != PFAIL:
        gate.add(name, warn_as, r["summary"], r["fix"])
    else:
        detail = [l for l in (r["detail"] or "").splitlines() if l.strip()][:3]
        gate.add(name, PFAIL, r["summary"] + ("; " + "; ".join(detail) if detail else ""),
                 r["fix"] or "re-run install.sh, then restart Claude Code")
    return r


def _installed_record():
    data = _read_json(INSTALLED_PLUGINS)
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else None


def _entry(record, key):
    try:
        e = (record.get(key) or [])[0]
        return e if isinstance(e, dict) else None
    except (IndexError, AttributeError, TypeError):
        return None


def pi_installed(gate, root, rel):
    record = _installed_record()
    if record is None:
        gate.add("installed", PFAIL, "could not run: %s is missing or unreadable"
                 % INSTALLED_PLUGINS, 'bash "<plugin-repo>/install.sh"')
        return
    problems, notes = [], []
    gt = _entry(record, "gt@%s" % MARKET_NAME)
    have = (gt or {}).get("version")
    if have != rel.name:
        problems.append("gt %s installed, release is %s" % (have or "NOT", rel.name))
    elif not Path(str(gt.get("installPath") or "")).is_dir():
        problems.append("gt %s is registered but its installPath does not exist" % have)
    gc = _components_module()
    if gc is None or not root:
        problems.append("could not run: module states unreadable (gt_components or the plugin "
                        "root is missing)")
    else:
        try:
            det = gc.module_detail(str(root), str(Path.home()), gt_version=rel.name)
        except Exception as exc:
            det = None
            problems.append("could not run: module states (%s)" % exc.__class__.__name__)
        enabled = (_read_json(SETTINGS).get("enabledPlugins") or {})
        for name, d in sorted((det or {}).items()):
            if d.get("state") != "on":
                continue
            key = "%s@%s" % (d.get("plugin"), MARKET_NAME)
            e = _entry(record, key)
            iv = (e or {}).get("version")
            if iv != d.get("version"):
                problems.append("module %s: %s installed, newest is %s"
                                % (name, iv or "NOT", d.get("version")))
            elif not (isinstance(enabled, dict) and enabled.get(key)):
                problems.append("module %s: %s not enabled in settings.json" % (name, key))
            else:
                notes.append("%s %s" % (name, iv))
    if problems:
        gate.add("installed", PFAIL, "; ".join(problems),
                 'bash "%s/install.sh"   (then restart Claude Code)' % (root or "<plugin-repo>"))
    else:
        gate.add("installed", PASS, "gt %s; ON modules at newest: %s"
                 % (rel.name, ", ".join(notes) or "none"))


def _imperative(path):
    try:
        return _frontmatter(Path(path).read_text(encoding="utf-8", errors="replace")) \
            .get("imperative")
    except OSError:
        return None


def pi_rule1_vault(gate, vault, rel):
    """-> True when the vault's rule-1 copy is the shipped one."""
    want = _imperative(rel / "templates" / "core-rules" / RULE1_FILE)
    if not want:
        gate.add("rule1-vault", PFAIL, "could not run: the release ships no %s imperative"
                 % RULE1_FILE)
        return False
    cdir = core_rules_dir(vault) if vault and vault.is_dir() else None
    have = _imperative(cdir / RULE1_FILE) if cdir else None
    if have == want:
        gate.add("rule1-vault", PASS, "vault %s matches the shipped template" % RULE1_FILE)
        return True
    if have is None:
        gate.needs_upgrade("rule1-vault", "the vault has no %s (core-rules: %s)"
                           % (RULE1_FILE, cdir or "not found"))
    else:
        # Name the command that FIXES it (0.17.11): the core-rules-refresh upgrade step
        # replaces an unmodified earlier-release copy; an owner-edited copy it keeps, and
        # only the owner can take the shipped text into it.
        tmpl = rel / "templates" / "core-rules" / RULE1_FILE
        gate.needs_upgrade("rule1-vault", "the vault's %s is an older text: %r"
                           % (RULE1_FILE, have[:70]),
                           "run /gt:gt-upgrade, or gt_upgrade.py run --vault %s (step core-rules-refresh replaces an "
                           "unmodified earlier-release copy; install.sh does the same for a git "
                           "vault). If it reports the rule 'edited locally, not refreshed', diff "
                           "%s against %s and take the shipped text. Then restart Claude Code "
                           "and re-run gt_doctor.py post-install" % (vault, cdir / RULE1_FILE, tmpl))
    return False


def _probe_env(vault, tmp):
    """The environment the probes run hooks in: the real vault, a THROWAWAY home holding a copy
    of vault-config.json. inject_core_rules.sh self-heals a stale core_rules_path into
    vault-config.json; doing that to the copy keeps this gate read-only."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "GT_"))}
    home = Path(tmp) / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    if CONFIG.is_file():
        try:
            (home / ".claude" / "vault-config.json").write_bytes(CONFIG.read_bytes())
        except OSError:
            pass
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)   # see _smoke_env: Windows Python reads THIS, not HOME
    env["GT_VAULT"] = str(vault)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_hook(script, payload, env, timeout=60):
    try:
        p = subprocess.run(["bash", str(script)], input=payload, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)


def pi_rule1_injected(gate, vault, vault_ok, tmp):
    script = INSTALLED_HOOKS / "inject_core_rules.sh"
    if not script.is_file():
        gate.add("rule1-injected", PFAIL, "could not run: %s is not installed" % script,
                 "re-run install.sh")
        return
    rc, out, err = _run_hook(script, "{}", _probe_env(vault, tmp))
    try:
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        gate.add("rule1-injected", PFAIL, "could not run: the hook printed no readable JSON "
                 "(exit %s) %s" % (rc, (err or out).strip()[-160:]), "re-run install.sh")
        return
    if "ENFORCEMENT DEGRADED" in ctx:
        reason = next((l for l in ctx.splitlines() if l.startswith("Reason:")), "")
        gate.add("rule1-injected", PFAIL, "the hook ran DEGRADED -- Core rules not loaded. "
                 + reason, "check vault-config.json's vault_path, then re-run install.sh")
        return
    line = next((l for l in ctx.splitlines() if l.startswith("1. ")), None)
    if line and line[3:].startswith(RULE1_PREFIX):
        gate.add("rule1-injected", PASS, "rule 1 is the queue-first text")
    elif not vault_ok:
        gate.needs_upgrade("rule1-injected", "rule 1 is %r -- the vault's rule file is older "
                           "than the release" % ((line or "no rule 1")[:70]))
    else:
        gate.add("rule1-injected", PFAIL, "rule 1 is %r, not the queue-first text"
                 % ((line or "no rule 1")[:90]),
                 "python3 <release>/scripts/vault_init.py install-core-rules --vault V")


def pi_queue_scripts(gate, rel):
    missing, differ = [], []
    for name in QUEUE_SCRIPTS:
        inst, ship = INSTALLED_HOOKS / name, rel / "scripts" / name
        if not ship.is_file():
            missing.append("%s (not in the release!)" % name)
        elif not inst.is_file():
            missing.append(name)
        else:
            try:
                if inst.read_bytes() != ship.read_bytes():
                    differ.append(name)
            except OSError as exc:
                differ.append("%s (unreadable: %s)" % (name, exc.__class__.__name__))
    if missing or differ:
        parts = (["missing: " + ", ".join(missing)] if missing else []) + \
                (["differ from the release: " + ", ".join(differ)] if differ else [])
        gate.add("queue-scripts", PFAIL, "; ".join(parts) + " (in %s)" % INSTALLED_HOOKS,
                 'bash "<plugin-repo>/install.sh"')
    else:
        gate.add("queue-scripts", PASS, "%s present and identical to %s"
                 % (", ".join(QUEUE_SCRIPTS), rel.name))


def pi_guard(gate, vault, tmp):
    script = INSTALLED_HOOKS / "guard_session_claims.sh"
    if not script.is_file():
        gate.add("guard-denies", PFAIL, "could not run: %s is not installed" % script,
                 "re-run install.sh")
        return
    env = _probe_env(vault, tmp)
    inside = Path(vault) / "INBOX.md"
    outside = Path(tmp) / "outside-the-vault" / "probe.md"
    try:
        Path(outside).resolve().relative_to(Path(vault).resolve())
        gate.add("guard-denies", PFAIL, "could not run: the probe directory %s is inside the "
                 "vault" % tmp)
        return
    except ValueError:
        pass

    def payload(path):
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                           "cwd": str(vault),
                           "tool_input": {"file_path": str(path), "content": "probe\n"}})

    rc, out, err = _run_hook(script, payload(inside), env)
    try:
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() \
            else None
    except Exception:
        decision = "unreadable"
    if rc is None:
        gate.add("guard-denies", PFAIL, "could not run: " + err[-160:])
        return
    if decision != "deny":
        gate.add("guard-denies", PFAIL, "a direct Write to %s was NOT denied (decision: %s)"
                 % (inside, decision or "none -- the guard raised no objection"),
                 "re-run install.sh; then vault_init.py install-core-rules --vault V")
        return
    rc2, out2, err2 = _run_hook(script, payload(outside), env)
    if rc2 != 0 or out2.strip():
        gate.add("guard-denies", PFAIL, "the guard objected to a path OUTSIDE the vault "
                 "(exit %s): %s" % (rc2, (out2 or err2).strip()[:160]),
                 "inspect guard_session_claims.py; a guard that blocks wrongly gets switched off")
        return
    gate.add("guard-denies", PASS, "denies a direct Write to INBOX.md; silent outside the vault")


def pi_queue(gate, vault):
    script = INSTALLED_HOOKS / "gt_broker.py"
    if not script.is_file():
        gate.add("queue", PFAIL, "could not run: %s is not installed" % script,
                 "re-run install.sh")
        return
    rc, out = run([sys.executable, str(script), "status", "--vault", str(vault), "--json"])
    try:
        n = int(json.loads(out.strip().splitlines()[-1])["pending"]) if rc == 0 else None
    except Exception:
        n = None
    if n is None:
        gate.add("queue", PFAIL, "could not run: gt_broker.py status exited %s: %s"
                 % (rc, out.strip()[-160:]))
    elif n == 0:
        gate.add("queue", PASS, "write queue empty")
    else:
        gate.add("queue", PWARN, "%d write request(s) waiting" % n,
                 'python3 %s drain --vault "%s"' % (script, vault))


def pi_vault(gate, vault, rel):
    rep = Report()
    check_vault(rep, vault, rel)
    r = rep.rows[0]
    if r["state"] == OK:
        gate.add("vault", PASS, r["summary"])
    elif r["state"] == WARN:
        gate.needs_upgrade("vault", r["summary"])
    else:
        _fold(gate, rep, "vault")


def pi_lotr(gate, root, rel):
    gc = _components_module()
    try:
        det = gc.module_detail(str(root), str(Path.home()), gt_version=rel.name) \
            if gc and root else None
    except Exception as exc:
        gate.add("lotr", PFAIL, "could not run: module states (%s)" % exc.__class__.__name__)
        return
    if det is None:
        gate.add("lotr", PFAIL, "could not run: module states unreadable")
        return
    d = det.get(LOTR_MODULE)
    if d is None:
        gate.add("lotr", INFO, "this tree ships no lotr module")
        return
    if d.get("state") != "on":
        # 0.20.0: on Windows lotr is off by design (gt_components.POSIX_ONLY_MODULES), whatever
        # was chosen; "install with --with lotr" there would send the user round in a circle.
        why = str(d.get("reason") or "")
        gate.add("lotr", INFO, why if why.startswith("POSIX-only") else
                 "off (install with --with lotr)")
        return
    e = _entry(_installed_record() or {}, "%s@%s" % (d.get("plugin"), MARKET_NAME))
    script = Path(str((e or {}).get("installPath") or "")) / "scripts" / "lotr.py"
    if not e or not script.is_file():
        gate.add("lotr", PFAIL, "module on, but the installed plugin has no scripts/lotr.py",
                 'bash "%s/install.sh" --with lotr' % root)
        return
    rc, out = run([sys.executable, str(script), "--help"], timeout=60)
    if rc == 0:
        gate.add("lotr", PASS, "on; %s --help exits 0" % script)
    else:
        gate.add("lotr", PFAIL, "on; lotr.py --help exited %s: %s" % (rc, out.strip()[-160:]),
                 'bash "%s/install.sh" --with lotr' % root)


def pi_daily_job(gate):
    import plistlib
    sched = _load_script("gt_schedule.py", HERE, INSTALLED_HOOKS)
    if sched is None or not hasattr(sched, "plist_path"):
        gate.add("daily-job", PFAIL, "could not run: gt_schedule.py is not installed beside "
                 "the doctor", "re-run install.sh")
        return
    # Windows (0.20.0): the job is a Task Scheduler task whose arguments gt_schedule keeps in a
    # spec file; job_file/job_args read whichever this platform uses.
    plist = sched.job_file("daily") if hasattr(sched, "job_file") else sched.plist_path("daily")
    kind = ("Task Scheduler" if os.name == "nt" else "launchd" if sys.platform == "darwin"
            else "systemd/cron")
    if not plist.is_file():
        gate.add("daily-job", INFO, "not installed (gt_schedule.py install daily --vault V)")
        return
    want = str(INSTALLED_HOOKS / "gt_daily.py")
    try:
        if hasattr(sched, "job_args"):
            args = sched.job_args("daily")
            if args is None:
                raise ValueError("no ProgramArguments")
        else:
            with plist.open("rb") as fh:
                args = [str(a) for a in (plistlib.load(fh).get("ProgramArguments") or [])]
    except Exception as exc:
        gate.add("daily-job", PFAIL, "could not run: %s is unreadable (%s)"
                 % (plist, exc.__class__.__name__))
        return
    # 0.20.1 (usability run M11): a job file on disk is not a job the scheduler has. A refused
    # bootstrap used to leave the plist behind and this row PASSed a job nothing would run.
    # None = the scheduler could not be asked honestly (a sandbox, another HOME): as before.
    reg = getattr(sched, "job_registered", lambda j: None)("daily")
    if reg is False:
        gate.add("daily-job", PFAIL, "%s daily job is on disk (%s) but not registered with the "
                 "scheduler, so it never runs" % (kind, plist),
                 "python3 %s install daily --vault V" % (INSTALLED_HOOKS / "gt_schedule.py"))
        return
    if want in args and Path(want).is_file():
        gate.add("daily-job", PASS, "%s daily job runs %s" % (kind, want))
    else:
        prog = next((a for a in args if a.endswith(".py")), "nothing")
        gate.add("daily-job", PFAIL, "%s daily job runs %s, not %s%s"
                 % (kind, prog, want, "" if Path(want).is_file() else " (which is missing)"),
                 "python3 %s install daily --vault V" % (INSTALLED_HOOKS / "gt_schedule.py"))


# ---- post-install smoke: the installed copies WORK, not only exist ------------------------
#
# Owner, 2026-10-01: the gate must prove things work, stay fast, and never write the real vault
# or settings. Each smoke row runs the INSTALLED scripts against its OWN throwaway vault and HOME
# (one temp dir per row, removed afterwards even on failure), so the rows run in parallel and
# cannot see each other. Every step has a hard timeout; a timeout is FAIL "timed out after Ns".

SMOKE_STEP_TIMEOUT = 45          # seconds, per subprocess step (~0.3s idle; generous under load)
LOTR_TOOLS = {"find", "call_read", "call_write", "call_consent"}


class SmokeTimeout(Exception):
    pass


def _smoke_env(home, vault=None, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "GT_", "LOTR_"))}
    env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_AUTHOR_NAME": "gt-smoke", "GIT_AUTHOR_EMAIL": "gt-smoke@localhost",
                "GIT_COMMITTER_NAME": "gt-smoke", "GIT_COMMITTER_EMAIL": "gt-smoke@localhost"})
    # Native Windows Python's expanduser() reads USERPROFILE and ignores HOME, so a smoke
    # test given only a throwaway HOME read -- and could write -- the REAL user's ~/.claude.
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    if vault is not None:
        env["GT_VAULT"] = str(vault)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _srun(cmd, env, timeout=SMOKE_STEP_TIMEOUT, input=None, cwd=None):
    """-> (rc, stdout+stderr). A timeout raises SmokeTimeout; nothing is left running."""
    try:
        p = subprocess.run([str(c) for c in cmd], input=input, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise SmokeTimeout(timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _smoke_vault(base, rel, tools=False, rules=False, git=False):
    """A minimal throwaway vault: INBOX.md, plus the release's vault tools / Core rules / a git
    repo with one commit when the row needs them. -> (vault, home)."""
    import shutil
    vault, home = Path(base) / "vault", Path(base) / "home"
    (home / ".claude").mkdir(parents=True)
    vault.mkdir()
    (vault / "INBOX.md").write_bytes("# Inbox\n".encode("utf-8"))
    if tools:
        dest = vault / "Projects" / "golden-thread" / "tools"
        dest.mkdir(parents=True)
        for f in (rel / "templates" / "tools").glob("*.py"):
            shutil.copy2(str(f), str(dest / f.name))
    if rules:
        shutil.copytree(str(rel / "templates" / "core-rules"), str(vault / "core-rules"))
    if git:
        (vault / "Daily Notes").mkdir()
        env = _smoke_env(home, vault)
        for cmd in (["git", "init", "-q", str(vault)], ["git", "-C", str(vault), "add", "-A"],
                    ["git", "-C", str(vault), "commit", "-q", "-m", "gt post-install smoke"]):
            rc, out = _srun(cmd, env)
            if rc != 0:
                raise RuntimeError("git %s failed: %s" % (cmd[1 if cmd[1] != "-C" else 3],
                                                          out.strip()[-120:]))
    return vault, home


def smoke_queue(base, rel):
    vault, home = _smoke_vault(base, rel)
    env = _smoke_env(home, vault)
    content = Path(base) / "line.txt"
    text = "- post-install smoke line"
    content.write_bytes((text + "\n").encode("utf-8"))
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_write_queue.py", "--vault", vault,
                     "--path", "INBOX.md", "--op", "append", "--content-file", content], env)
    if rc != 0:
        return PFAIL, "gt_write_queue.py append exited %s: %s" % (rc, out.strip()[-160:])
    if text in (vault / "INBOX.md").read_text(encoding="utf-8"):
        return PFAIL, "queueing wrote the target directly -- the queue must never touch it"
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_broker.py", "drain", "--vault", vault],
                    env)
    if rc != 0:
        return PFAIL, "gt_broker.py drain exited %s: %s" % (rc, out.strip()[-160:])
    if text not in (vault / "INBOX.md").read_text(encoding="utf-8"):
        return PFAIL, "drain exited 0 but INBOX.md does not hold the queued text"
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_broker.py", "status", "--vault", vault,
                     "--json"], env)
    try:
        pending = json.loads(out.strip().splitlines()[-1])["pending"]
    except Exception:
        return PFAIL, "gt_broker.py status unreadable after drain (exit %s)" % rc
    if pending:
        return PFAIL, "%s request(s) still queued after drain" % pending
    return PASS, "append queued, drained, written; queue empty"


def smoke_escalate(base, rel):
    vault, home = _smoke_vault(base, rel, tools=True)
    env = _smoke_env(home, vault)
    note = vault / "note.md"
    note.write_bytes("# Note\nbefore\n".encode("utf-8"))
    new = Path(base) / "new.txt"
    new.write_bytes("# Note\nreplaced by the queue\n".encode("utf-8"))
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_write_queue.py", "--vault", vault,
                     "--path", "note.md", "--op", "replace-file", "--content-file", new], env)
    if rc != 0:
        return PFAIL, "gt_write_queue.py replace-file exited %s: %s" % (rc, out.strip()[-160:])
    with note.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("owner edit\n")
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_broker.py", "drain", "--vault", vault,
                     "--json"], env)
    try:
        decisions = [r.get("decision") for r in json.loads(out[out.index("{"):])["results"]]
    except Exception:
        return PFAIL, "gt_broker.py drain --json unreadable (exit %s): %s" % (rc, out[-160:])
    body = note.read_text(encoding="utf-8")
    if "owner edit" not in body or "replaced by the queue" in body:
        return PFAIL, "the owner's edit was OVERWRITTEN by a stale replace-file"
    if decisions != ["escalate"]:
        return PFAIL, "the stale replace-file was %s, not escalated" % (decisions or "not decided")
    return PASS, "a replace-file whose target changed was escalated; the owner edit survived"


def smoke_guard(base, rel):
    vault, home = _smoke_vault(base, rel)
    env = _smoke_env(home, vault)
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(vault),
                          "tool_input": {"file_path": str(vault / "INBOX.md"), "content": "x"}})
    rc, out = _srun(["bash", INSTALLED_HOOKS / "guard_session_claims.sh"], env, input=payload)
    try:
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        decision = None
    if decision != "deny":
        return PFAIL, "a direct Write to the temp vault's INBOX.md was not denied (exit %s)" % rc
    return PASS, "denied a direct Write to the temp vault's INBOX.md"


def _expected_rules(rules_dir, rel):
    """How many rules the hook should inject, judged by the RELEASE's own rule parser (an
    imperative may come from the frontmatter or the first bold line), never a copy of it."""
    gp = _load_script("gt_paths.py", rel / "scripts")
    if gp is None or not hasattr(gp, "parse_rule"):
        raise RuntimeError("the release's gt_paths.py has no parse_rule")
    n = 0
    for p in sorted(Path(rules_dir).glob("core_*.md")):
        if p.name == getattr(gp, "MODEL_FILE", "core_rule_priority_model.md"):
            continue
        r = gp.parse_rule(p)
        if r.get("level") == "core" and r.get("imperative") \
                and str(r.get("inject", "")).lower() != "false":
            n += 1
    return n


def smoke_rules(base, rel):
    vault, home = _smoke_vault(base, rel, rules=True)
    env = _smoke_env(home, vault)
    want = _expected_rules(vault / "core-rules", rel)
    rc, out = _srun(["bash", INSTALLED_HOOKS / "inject_core_rules.sh"], env, input="{}")
    try:
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return PFAIL, "inject_core_rules.sh printed no readable JSON (exit %s)" % rc
    import re
    numbered = [l for l in ctx.splitlines() if re.match(r"^\d+\. ", l)]
    if "ENFORCEMENT DEGRADED" in ctx:
        return PFAIL, "the hook ran DEGRADED against the temp vault"
    if len(numbered) != want:
        return PFAIL, "injected %d rule(s), the release ships %d" % (len(numbered), want)
    if not numbered[0][3:].startswith(RULE1_PREFIX):
        return PFAIL, "rule 1 is %r, not queue-first" % numbered[0][:80]
    return PASS, "%d rules injected, rule 1 queue-first" % want


def smoke_daily(base, rel):
    vault, home = _smoke_vault(base, rel, git=True)
    env = _smoke_env(home, vault)
    rc, out = _srun([sys.executable, INSTALLED_HOOKS / "gt_daily.py", "--vault", vault,
                     "--dry-run"], env)
    if rc != 0:
        return PFAIL, "gt_daily.py --dry-run exited %s: %s" % (rc, out.strip()[-160:])
    if list((vault / "Daily Notes").iterdir()):
        return PFAIL, "gt_daily.py --dry-run wrote a daily note"
    return PASS, "gt_daily.py --dry-run exits 0 against a temp vault"


def _configured_mcp_command(plugin_root, server):
    """-> (argv, None) for an installed plugin's MCP server as Claude Code would start it, or
    (None, why not)."""
    man = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    try:
        cfg = json.loads(man.read_text(encoding="utf-8"))["mcpServers"][server]
        command, args = cfg["command"], list(cfg.get("args") or [])
    except Exception:                                    # noqa: BLE001
        return None, "%s declares no usable mcpServers.%s" % (man, server)
    root = str(plugin_root).replace("\\", "/") if os.name == "nt" else str(plugin_root)
    argv = [str(x).replace("${CLAUDE_PLUGIN_ROOT}", root) for x in [command] + args]
    if os.name == "nt" and os.path.basename(argv[0]).lower() in ("python3", "python3.exe",
                                                                  "python", "python.exe") \
            and not os.path.isabs(argv[0]):
        return None, ("the configured MCP command is %r: on Windows that is the Microsoft "
                      "Store stub, so Claude Code cannot start the server -- re-run install.sh"
                      % argv[0])
    return argv, None


def smoke_lotr(base, rel, root):
    import shutil
    import tempfile
    import time
    gc = _components_module()
    det = gc.module_detail(str(root), str(Path.home()), gt_version=rel.name) if gc and root \
        else None
    if det is None:
        return PFAIL, "could not run: module states unreadable"
    d = det.get(LOTR_MODULE)
    if d is None or d.get("state") != "on":
        return INFO, "lotr is off -- nothing to smoke-test"
    e = _entry(_installed_record() or {}, "%s@%s" % (d.get("plugin"), MARKET_NAME))
    scripts = Path(str((e or {}).get("installPath") or "")) / "scripts"
    if not (scripts / "lotr.py").is_file():
        return PFAIL, "lotr is on but %s has no lotr.py" % scripts
    # A SHORT home: a unix socket path is limited to ~104 bytes on macOS, and a temp dir under
    # /var/folders or a scratchpad is long enough to make lotrd fail with socket_failed.
    lhome = Path(tempfile.mkdtemp(prefix="gtl-", dir="/tmp" if os.path.isdir("/tmp") else None))
    # gt-lotr 0.3.0 loads gt core's gt_ipc / unlock client from the gt hooks dir; the smoke's
    # throwaway home has none, so point it at the installed one (honoured only while unlock is
    # off, which a throwaway home always is).
    env = _smoke_env(Path(base), None, LOTR_HOME=lhome, GT_HOOKS_DIR=str(INSTALLED_HOOKS))
    daemon = None
    try:
        rc, out = _srun([sys.executable, scripts / "lotr.py", "--home", lhome, "init", "--zone",
                         "personal", "--mode", "local"], env)
        if rc != 0:
            return PFAIL, "lotr.py init exited %s: %s" % (rc, out.strip()[-160:])
        daemon = subprocess.Popen([sys.executable, "-I", str(scripts / "lotrd.py"), "--home", str(lhome)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  text=True)
        sock, t0 = lhome / "lotrd.sock", time.time()

        def up():
            if os.name != "nt":
                return sock.exists()
            # Windows (gt-lotr 0.3.0): the front door is a named pipe, not a file.
            try:
                sys.path.insert(0, str(INSTALLED_HOOKS))
                import gt_ipc
                return gt_ipc.alive_at(gt_ipc.default_address(str(lhome), "lotrd"), 0.3)
            except Exception:                    # noqa: BLE001
                return False
        while not up():
            if daemon.poll() is not None:
                return PFAIL, "lotrd exited %s before its socket appeared: %s" % (
                    daemon.returncode, (daemon.stderr.read() or "").strip()[-160:])
            if time.time() - t0 > SMOKE_STEP_TIMEOUT:
                raise SmokeTimeout(SMOKE_STEP_TIMEOUT)
            time.sleep(0.05)
        rc, out = _srun([sys.executable, scripts / "lotr.py", "--home", lhome, "find", ""], env)
        try:
            ok = json.loads(out).get("ok") is True
        except Exception:
            ok = False
        if rc != 0 or not ok:
            return PFAIL, "lotr.py find \"\" failed (exit %s): %s" % (rc, out.strip()[-160:])
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "gt-doctor", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        # The EXACT command Claude Code is configured to start (0.20.0): the installed
        # manifest's mcpServers entry, ${CLAUDE_PLUGIN_ROOT} substituted the way Claude Code
        # does (forward slashes on Windows). Until 0.20.0 this ran sys.executable, which proved
        # the script and not the command -- on Windows the command was the Store stub.
        cmd, why = _configured_mcp_command(scripts.parent, "gt-lotr")
        if cmd is None:
            return PFAIL, why
        rc, out = _srun(cmd, env, input="".join(json.dumps(m) + "\n" for m in msgs))
        tools = None
        for line in out.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2:
                tools = [t.get("name") for t in (m.get("result") or {}).get("tools") or []]
        if tools is None:
            return PFAIL, "lotr_mcp.py gave no tools/list answer (exit %s)" % rc
        if len(tools) != 4 or set(tools) != LOTR_TOOLS:
            return PFAIL, "lotr_mcp.py tools/list returned %s, not exactly %s" % (
                sorted(tools), sorted(LOTR_TOOLS))
        return PASS, ("init, lotrd up, find \"\" ok, the configured MCP command (%s) answers "
                      "tools/list with the 4 tools" % os.path.basename(cmd[0]))
    finally:
        if daemon is not None and daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
        _rmtree(str(lhome))


SMOKE = (("smoke-queue", smoke_queue, "re-run install.sh; the hooks-dir gt_write_queue.py / "
                                      "gt_broker.py do not round-trip a write"),
         ("smoke-escalate", smoke_escalate, "re-run install.sh; the installed gt_broker.py "
                                            "overwrites a changed file"),
         ("smoke-guard", smoke_guard, "re-run install.sh; guard_session_claims.sh is broken"),
         ("smoke-rules", smoke_rules, "re-run install.sh; inject_core_rules.sh is broken"),
         ("smoke-lotr", None, 'bash "<plugin-repo>/install.sh" --with lotr'),
         ("smoke-daily", smoke_daily, "re-run install.sh; the hooks-dir gt_daily.py is broken"))


def run_smoke(rel, root):
    """Every smoke row in parallel, each in its own temp dir; the parent is always removed."""
    import concurrent.futures
    import shutil
    import tempfile
    parent = Path(tempfile.mkdtemp(prefix="gt-smoke-"))

    def one(i, name, fn):
        base = parent / ("%d-%s" % (i, name))
        base.mkdir()
        try:
            if name == "smoke-lotr":
                return smoke_lotr(base, rel, root)
            return fn(base, rel)
        except SmokeTimeout as t:
            return PFAIL, "timed out after %ss" % t.args[0]
        except (Exception, SystemExit) as exc:
            return PFAIL, "could not run: %s: %s" % (exc.__class__.__name__, str(exc)[:160])

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(SMOKE)) as pool:
            futs = [pool.submit(one, i, name, fn) for i, (name, fn, _fix) in enumerate(SMOKE)]
            results = [f.result() for f in futs]
    finally:
        _rmtree(str(parent))
    return [(name, state, summary, fix)
            for (name, _fn, fix), (state, summary) in zip(SMOKE, results)]


def post_install(vault, root, rel, stage="final"):
    """-> Gate. Every row of the release gate, in the order the docstring lists them."""
    gate = Gate(stage)
    if rel is None:
        gate.add("release", PFAIL, "could not run: no release directory found",
                 "pass --release <plugin-repo>/golden-thread/<version>")
        return gate
    gate.add("release", INFO, "%s (%s)" % (rel.name, rel))
    # The smoke rows touch nothing the placement rows read, so they run beside them.
    import threading
    smoke = {}
    th = threading.Thread(target=lambda: smoke.update(rows=run_smoke(rel, root)), daemon=True)
    th.start()
    try:
        _placement(gate, vault, root, rel)
        gate.defer_vault_rows()
    finally:
        th.join()
        for name, state, summary, fix in smoke.get("rows") or [
                ("smoke", PFAIL, "could not run: the smoke thread produced no rows", "")]:
            gate.add(name, state, summary, fix)
    return gate


def _placement(gate, vault, root, rel):
    import tempfile
    pi_installed(gate, root, rel)
    rep = Report()
    check_components(rep, rel)
    check_wiring(rep, rel)
    _fold(gate, rep, "components")
    _fold(gate, rep, "wiring")
    if not vault or not vault.is_dir():
        gate.add("vault", PFAIL, "could not run: no vault (%s)" % (vault or "none configured"),
                 "pass --vault V")
        return
    rep = Report()
    check_core_rules(rep, vault)
    _fold(gate, rep, "core-rules")
    vault_ok = pi_rule1_vault(gate, vault, rel)
    tmp = tempfile.mkdtemp(prefix="gt-postinstall-")
    try:
        pi_rule1_injected(gate, vault, vault_ok, tmp)
        pi_queue_scripts(gate, rel)
        pi_guard(gate, vault, tmp)
    finally:
        import shutil
        _rmtree(tmp)
    pi_queue(gate, vault)
    pi_vault(gate, vault, rel)
    pi_lotr(gate, root, rel)
    pi_daily_job(gate)


RECEIPT = Path.home() / ".claude" / "golden-thread" / "post-install-validated.json"
WRITERS = ("hook", "manual", "install")


def write_receipt(version, failed, counts, stage, writer):
    """The one writer of post-install-validated.json (0.19.1). gt_version_check reads it to run
    the session gate once per installed version; a person reads it to see what was proven."""
    import time
    try:
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        tmp = RECEIPT.with_name(RECEIPT.name + ".tmp")
        tmp.write_bytes((json.dumps({"version": version, "failed": bool(failed), "counts": counts,
                                   "stage": stage, "writer": writer,
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")
                      .encode("utf-8"))
        os.replace(tmp, RECEIPT)
    except OSError:
        pass


def post_install_main(a):
    stage = getattr(a, "stage", None) or "final"
    rel = release_dir(getattr(a, "release", None), plugin_root(getattr(a, "plugin_root", None)))
    root = plugin_root(getattr(a, "plugin_root", None))
    if not source_present(root) and rel is not None:
        # No source tree (deleted after the install, or never wired): module states are read
        # from where the release itself lives -- its source tree, or the plugin cache (M7).
        root = rel.parent.parent
    vault = vault_path(getattr(a, "vault", None))
    import time
    t0 = time.monotonic()
    crashed = False
    try:
        gate = post_install(vault, root, rel, stage)
    except (Exception, SystemExit) as exc:   # fail closed: a crashed gate is a failed gate
        crashed = True
        gate = Gate(stage)
        gate.add("post-install", PFAIL, "could not run: %s: %s" % (exc.__class__.__name__, exc))
    elapsed = round(time.monotonic() - t0, 1)
    counts = {}
    for r in gate.rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    # 0.19.1: every COMPLETED session/final run records its verdict, whoever ran it. Not a
    # crashed gate (could not run is not a verdict), not the install stage (vault rows are
    # PENDING there by design), not --dry-run.
    if (not crashed and rel is not None and stage in ("session", "final")
            and not getattr(a, "dry_run", False)):
        write_receipt(rel.name, gate.failed, counts, stage, getattr(a, "writer", None) or "manual")
    if getattr(a, "json", False):
        print(json.dumps({"release": rel.name if rel else None,
                          "vault": str(vault) if vault else None, "stage": stage,
                          "failed": gate.failed, "counts": counts, "elapsed_s": elapsed,
                          "rows": gate.rows},
                         indent=2))
    else:
        print("GOLDEN THREAD post-install — release %s, vault %s, stage %s"
              % (rel.name if rel else "UNKNOWN", vault or "UNKNOWN", stage))
        for r in gate.rows:
            print("%-7s %-14s %s" % (r["state"], r["row"], r["summary"]))
            if r["fix"]:
                print("        %-14s fix: %s" % ("", r["fix"]))
        order = (PASS, PFAIL, PWARN, PENDING, INFO)
        print("\n%s — %s — %.1fs" % ("FAILED" if gate.failed else "passed",
                                    ", ".join("%d %s" % (counts[s], s) for s in order
                                              if s in counts), elapsed))
    return 1 if gate.failed else 0

# --- repo-target (0.18.1) -------------------------------------------------------------------
#
# gt makes the vault the working directory, and the vault is a git repo. So every repo-scoped
# tool that is not gt's -- Claude Code's /security-review and /code-review, a test runner, a CI
# helper -- resolves "the current branch" to the VAULT and finds a plausible, non-empty answer
# there. On 2026-09-25 /security-review collected a 624 KB diff of vault markdown for a 174-line
# code change and reported `On branch main`. Nothing errored. This row makes "which repo would
# that answer for?" a question already answered. It is a NOTE in every outcome: cwd == vault is
# the normal, correct gt configuration, and an alarm on it would train people to ignore it.
def _same_path(a, b):
    try:
        return os.path.samefile(a, b)       # symlinks and case-insensitive volumes both bite
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def check_repo_target(rep, vault, cwd=None):
    cwd = os.path.realpath(cwd or os.getcwd())
    rc, out = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=30)
    if rc is None:
        rep.add("repo-target", UNKNOWN, "could not ask git which repo %s is in" % cwd,
                out.strip()[-300:])
        return
    vtxt = str(vault) if vault else "none configured"
    if rc != 0:
        rep.add("repo-target", NOTE,
                "working directory %s is NOT inside any git repo: repo-scoped commands "
                "(/security-review, /code-review) have no repo to review here" % cwd,
                "cwd:   %s\nrepo:  none\nvault: %s" % (cwd, vtxt))
        return
    top = os.path.realpath(out.strip().splitlines()[-1]) if out.strip() else cwd
    detail = "cwd:   %s\nrepo:  %s\nvault: %s" % (cwd, top, vtxt)
    if vault and _same_path(top, str(vault)):
        rep.add("repo-target", NOTE,
                "working directory resolves to the VAULT's git repo (%s): repo-scoped commands "
                "such as /security-review and /code-review will review the vault here, not "
                "your code -- point them at the code repo explicitly, or use "
                "gt_code_review.py plan <repo>, which takes the root as an argument" % top,
                detail)
        return
    rep.add("repo-target", NOTE,
            "working directory resolves to git repo %s, which is NOT the vault (%s): "
            "repo-scoped commands review %s" % (top, vtxt, top), detail)


# --- hooks-schema (0.18.1) ------------------------------------------------------------------
#
# `wiring` asks whether every hook the release declares is in settings.json. It cannot see an
# entry that is present, correctly pointed, and NEVER FIRES because the event or tool it names
# is one Claude Code does not have (renamed, removed, or a typo). That entry is reported
# "installed" -- the 2026-08-30 shape: the check ran correctly against the wrong question.
# The allowlists are conservative and updated per release; an unknown name prompts review,
# it is not proof of breakage, so findings are WARN and nothing is removed.
SCHEMA_FILES = ("known_events.json", "known_tools.json")


def _schema_file(name):
    for d in (HERE, HERE.parent / "hooks", INSTALLED_HOOKS):
        if (d / name).is_file():
            return d / name
    return None


def _hook_paths(cmd):
    """Paths in a hook command that point into the gt hooks dir."""
    home = str(Path.home())
    hooks = os.path.realpath(str(INSTALLED_HOOKS))
    out = []
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    for t in toks:
        t = t.replace("$HOME", home).replace("${HOME}", home)
        t = os.path.expanduser(t)
        if os.name == "nt":
            # 0.20.0: gt writes hook commands "C:/Users/..." and "~" expands to "C:\Users\x/...",
            # so neither ever started with the native hooks dir: a missing file went unseen.
            t = os.path.normpath(t)
            c = os.path.normcase(t)
            if c.startswith(os.path.normcase(str(INSTALLED_HOOKS))) \
                    or c.startswith(os.path.normcase(hooks)):
                out.append(t)
            continue
        if t.startswith(str(INSTALLED_HOOKS)) or t.startswith(hooks):
            out.append(t)
    return out


def check_hooks_schema(rep, settings=None):
    settings = Path(settings or SETTINGS)
    files = {n: _schema_file(n) for n in SCHEMA_FILES}
    if not all(files.values()):
        rep.add("hooks-schema", UNKNOWN, "the hook allowlist(s) are missing: %s"
                % ", ".join(n for n, f in files.items() if not f),
                fix="re-run install.sh: they ship in the release's hooks/")
        return
    try:
        ev = json.loads(files["known_events.json"].read_text(encoding="utf-8"))
        tl = json.loads(files["known_tools.json"].read_text(encoding="utf-8"))
        events, tool_events = set(ev["events"]), set(ev["tool_events"])
        tools = set(tl["tools"])
        if not events or not tools:
            raise ValueError("empty allowlist")
    except Exception as exc:
        rep.add("hooks-schema", UNKNOWN, "the hook allowlists are unreadable (%s)"
                % exc.__class__.__name__)
        return
    if not settings.is_file():
        rep.add("hooks-schema", UNKNOWN, "no %s to check" % settings)
        return
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks") or {}
        if not isinstance(hooks, dict):
            raise ValueError("hooks is not an object")
    except Exception as exc:
        rep.add("hooks-schema", UNKNOWN, "settings.json is unreadable (%s)"
                % exc.__class__.__name__)
        return
    findings, n = [], 0
    for event, blocks in sorted(hooks.items()):
        if event not in events:
            findings.append("hooks-unknown-event %s -- Claude Code fires no such event, so its "
                            "hooks never run" % event)
        for b in blocks if isinstance(blocks, list) else []:
            if not isinstance(b, dict):
                continue
            m = b.get("matcher")
            if event in tool_events and isinstance(m, str):
                for t in (x.strip() for x in m.split("|")):
                    # Only plain names are judged: "*", "", mcp__ names and regex patterns
                    # cannot be checked against a list and are left alone.
                    if not t or t == "*" or t.startswith("mcp__") or not t.isidentifier():
                        continue
                    if t not in tools:
                        findings.append("hooks-unknown-tool %s matcher %r -- no known tool is "
                                        "called %s" % (event, m, t))
            for h in b.get("hooks") or []:
                if not (isinstance(h, dict) and h.get("command")):
                    continue
                n += 1
                for path in _hook_paths(str(h["command"])):
                    if not os.path.exists(path):
                        findings.append("hooks-missing-file %s -> %s" % (event, path))
    ref = ev.get("claude_code_reference", "?")
    if findings:
        rep.add("hooks-schema", WARN,
                "%d hook entr%s Claude Code may never fire (allowlist as of %s)"
                % (len(findings), "y" if len(findings) == 1 else "ies", ref),
                "\n".join(findings),
                fix="review ~/.claude/settings.json; if Claude Code added the name, it belongs "
                    "in hooks/known_events.json or known_tools.json for the next release")
        return
    rep.add("hooks-schema", OK, "%d hook command(s) on %d event(s) name events and tools "
            "Claude Code fires (allowlist as of %s)" % (n, len(hooks), ref))


def check_checkers(rep, root, vdir):
    """The validation host (0.18.1): how many module checkers are installed, and which of
    them cannot run because a tool they require is not on PATH. Read-only."""
    chk = _load_script("gt_check.py", HERE, vdir / "scripts" if vdir else None)
    if chk is None:
        rep.add("checkers", UNKNOWN, "gt_check.py could not be loaded", fix="run install.sh")
        return
    if not root:
        rep.add("checkers", UNKNOWN, "cannot locate the plugin source",
                fix="run install.sh, then restart Claude Code")
        return
    try:
        found = chk.installed_checkers(str(root))
    except Exception as exc:                                  # noqa: BLE001
        rep.add("checkers", UNKNOWN, "checkers could not be listed", str(exc))
        return
    missing = [c for c in found if c["missing_tools"]]
    lines = ["%s — tool(s) missing: %s" % (c["key"], ", ".join(c["missing_tools"]))
             for c in missing]
    summary = "%d checker(s) installed" % len(found)
    if missing:
        rep.add("checkers", WARN, summary + ", %d cannot run (required tool missing)"
                % len(missing), "\n".join(lines),
                fix="install the named tool(s); until then those checkers report cannot-check")
    else:
        rep.add("checkers", OK, summary,
                "\n".join(c["key"] for c in found))


CHECKS = ("version", "components", "wiring", "core-rules", "modules", "model-policy", "vault",
          "schedule", "workers", "push", "gt-src", "lint", "astgrep", "checkers")
CHECKS += ("repo-target",)      # 0.18.1: which repo would a repo-scoped command answer for?
CHECKS += ("hooks-schema",)     # 0.18.1: settings.json hooks against Claude Code's events
CHECKS = CHECKS + ("execution",)        # 0.18.1: gt_bench.health (measured profile, Rosetta)
CHECKS += ("unlock", "security")        # 0.20.0: gt unlock state + level; its self-check
CHECKS += ("sandbox",)                  # 0.20.0: gt sandbox mode -- settings drift, platform
CHECKS += ("scratch",)                  # 0.20.0: stage agents' scratch left by finished runs
CHECKS += ("write-probe",)              # 0.20.0: can each Python write where gt writes?
CHECKS += ("mcp",)                      # 0.20.1: MCP servers outside LOTR / gt-vault


def _unlock_cli():
    return INSTALLED_HOOKS / "gt_unlock.py"


def check_unlock(rep):
    """gt unlock (0.20.0): off is a NOTE that says what it does and how to turn it on -- shipped
    off, and the user is told, without nagging every session. On: the level the machine runs at,
    or WARN when it is locked by a problem or the authority is down (gated scopes refused)."""
    cli = _unlock_cli()
    if not cli.is_file():
        rep.add("unlock", UNKNOWN, "gt_unlock.py is not installed")
        return
    rc, out = run([sys.executable, str(cli), "status", "--json"])
    try:
        st = json.loads(out)
    except (TypeError, ValueError):
        rep.add("unlock", UNKNOWN, "gt_unlock.py status did not answer", (out or "")[-800:])
        return
    lv = (st.get("assurance") or {})
    if not st.get("enabled"):
        # LOTR is named only when its module is installed (0.20.1: "agents use LOTR" was said
        # with LOTR off). The presence factors are this platform's.
        lotr = bool(_entry(_installed_record() or {}, "gt-lotr@%s" % MARKET_NAME))
        factor = ("Touch ID" if sys.platform == "darwin" else
                  "Windows Hello" if os.name == "nt" else None)
        rep.add("unlock", NOTE, "unlock: off -- agents use %sgt's settings without asking "
                "you. It can require your presence (TOTP%s): python3 %s enroll, then python3 %s "
                "policy enable; SECURITY.md"
                % ("LOTR, secrets and " if lotr else "secrets and ",
                   " + " + factor if factor else "", cli, cli))
        return
    if st.get("reachable") is False:
        rep.add("unlock", WARN, "unlock: ON, authority not running -- every gated scope is "
                "refused (fail closed)", fix="python3 %s daemon start" % cli)
        return
    if st.get("problems"):
        rep.add("unlock", WARN, "unlock: ON, LOCKED -- " + "; ".join(st["problems"]),
                fix="python3 %s status" % cli)
        return
    rep.add("unlock", OK, "unlock: ON, level %s -- %s" % (lv.get("level"), lv.get("why")))


def check_security(rep):
    """`gt_unlock.py verify`, summarised: FAIL rows fail; NOT-CHECKED is never "ok"."""
    cli = _unlock_cli()
    if not cli.is_file():
        rep.add("security", UNKNOWN, "gt_unlock.py is not installed")
        return
    rc, out = run([sys.executable, str(cli), "verify", "--json"])
    try:
        v = json.loads(out)
    except (TypeError, ValueError):
        rep.add("security", UNKNOWN, "gt_unlock.py verify did not answer", (out or "")[-800:])
        return
    detail = "\n".join("%s %s: %s" % (x["state"], x["check"], x["why"]) for x in v["rows"])
    head = "level %s · %d FAIL, %d NOT-CHECKED" % (v["level"].get("level"), v["fail"],
                                                    v["not_checked"])
    if v["fail"]:
        rep.add("security", FAIL, head, detail, fix="python3 %s verify" % cli)
    elif v["level"].get("level") == "off":
        rep.add("security", NOTE, head + " (unlock is off)", detail)
    elif v["not_checked"]:
        rep.add("security", UNKNOWN, head, detail, fix="python3 %s verify" % cli)
    else:
        rep.add("security", OK, head, detail)


def check_write_probe(rep, vault):
    """Can each candidate Python write where gt writes -- the vault and ~/.claude/golden-thread
    (gt_write_probe.py, 0.20.0)? On macOS a com.apple.provenance file can refuse one interpreter
    and not another (Homebrew's python3.9 got EPERM on the vault's log.md, 2026-10-03). FAIL
    when the one gt uses is refused; WARN when only another candidate is; PASS/FAIL per
    interpreter in the detail, and which one gt uses."""
    wp = None
    for d in (HERE, INSTALLED_HOOKS):
        if (d / "gt_write_probe.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("gt_write_probe",
                                                          str(d / "gt_write_probe.py"))
            wp = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(wp)
            except Exception:                       # noqa: BLE001
                wp = None
            break
    if wp is None:
        rep.add("write-probe", UNKNOWN, "gt_write_probe.py is not installed in the hooks dir")
        return
    try:
        r = wp.probe(str(vault) if vault else None)
    except Exception as e:                          # noqa: BLE001
        rep.add("write-probe", UNKNOWN, "the write probe could not run (%s)" % type(e).__name__)
        return
    if not r["places"]:
        rep.add("write-probe", UNKNOWN, "nothing to probe: no vault and no ~/.claude/golden-thread")
        return
    detail = "\n".join("%s %-16s %s  (%s)" % ("PASS" if x["ok"] else "FAIL", x["label"],
                                               x["python"], ", ".join(
                                                   "%s %s" % kv for kv in x["results"].items()))
                       for x in r["rows"])
    uses = r["uses"]
    used = next((x for x in r["rows"]
                 if os.path.realpath(x["python"]) == os.path.realpath(uses or "")), None)
    if used is None:
        rep.add("write-probe", UNKNOWN, "gt uses %s, which could not be probed" % uses, detail)
    elif not used["ok"]:
        rep.add("write-probe", FAIL, "gt uses %s and it cannot write where gt writes" % uses,
                detail, fix="re-run install.sh (it records an interpreter that passes), or give "
                            "%s Full Disk Access (System Settings > Privacy & Security)" % uses)
    elif not all(x["ok"] for x in r["rows"]):
        rep.add("write-probe", WARN, "gt uses %s: PASS; another python here is refused -- run gt "
                "tools with %s, not bare python3" % (uses, uses), detail)
    else:
        rep.add("write-probe", OK, "gt uses %s: every candidate writes the %s"
                % (uses, " and ".join(p["name"] for p in r["places"])), detail)


def check_scratch(rep, vault):
    """Stage agents' private scratch folders (gt_scratch.py, 0.20.0): a run that finished or
    vanished must not leave its scratch -- extracted raw material -- behind. WARN names each,
    with the command that removes it; nothing is removed here."""
    gsc = None
    for d in (HERE, INSTALLED_HOOKS):
        if (d / "gt_scratch.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("gt_scratch", str(d / "gt_scratch.py"))
            gsc = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(gsc)
            except Exception:                       # noqa: BLE001
                gsc = None
            break
    if gsc is None:
        rep.add("scratch", UNKNOWN, "gt_scratch.py is not installed in the hooks dir")
        return
    if not vault:
        rep.add("scratch", UNKNOWN, "no vault configured, so finished runs cannot be told apart")
        return
    try:
        runs, bad = gsc.runs(), gsc.leaks(str(vault))
    except Exception as e:                          # noqa: BLE001
        rep.add("scratch", UNKNOWN, "the scratch check could not run (%s)" % type(e).__name__)
        return
    if bad:
        rep.add("scratch", WARN, "%d finished or vanished run(s) left scratch under %s"
                % (len(bad), gsc.root()),
                "\n".join("%s: %d unit folder(s), %d bytes" % (r["run"], len(r["units"]),
                                                              r["bytes"]) for r in bad),
                fix="python3 <plugin>/scripts/gt_ingest_pipeline.py cleanup <run>   (each run "
                    "above), or rm -rf the folder")
        return
    # Only a stage whose agent can write gets a folder (gt_agent_spec.WRITE_TOOLS), so most
    # runs -- every ingest -- never have one: zero is the normal count, not a missing check.
    rep.add("scratch", OK, "no scratch left by a finished run (%d run(s) in progress hold "
            "scratch; only stages whose agents can write get any)" % len(runs))


def check_sandbox(rep):
    """gt sandbox mode (0.20.0). Off is a NOTE naming what it would fence. On: OK only when every
    entry gt needs is in settings.json and nothing overrides it, and the platform has an OS
    sandbox; native Windows says "friction" (permission rules only) and is never OK-as-boundary.
    Drift (an entry missing, a managed or project setting overriding gt) is a FAIL."""
    gs = None
    for d in (HERE, INSTALLED_HOOKS):              # beside this doctor first, then the install
        if (d / "gt_sandbox.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("gt_sandbox", str(d / "gt_sandbox.py"))
            gs = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(d))
            try:
                spec.loader.exec_module(gs)
            except Exception:                       # noqa: BLE001
                gs = None
            break
    if gs is None:
        rep.add("sandbox", UNKNOWN, "gt_sandbox.py is not installed in the hooks dir")
        return
    gt_sandbox = gs
    try:
        c = gt_sandbox.check()
    except Exception as e:                          # noqa: BLE001
        rep.add("sandbox", UNKNOWN, "the sandbox check could not run (%s)" % type(e).__name__)
        return
    mode = c.get("mode")
    fix = "python3 %s apply   (from a terminal)" % (INSTALLED_HOOKS / "gt_sandbox.py")
    if c["state"] == "off":
        rep.add("sandbox", NOTE, "sandbox mode: off -- Claude's shell and file tools can read and "
                "write the vault and gt's state. gt_settings.py set sandbox_mode on fences them "
                "(SECURITY.md, 'gt sandbox mode')")
        return
    if c["state"] == "stale-off":
        rep.add("sandbox", WARN, "sandbox mode: off, but %d of gt's entries are still in "
                "settings.json" % len(c["stale"]), "\n".join(c["stale"][:20]),
                fix="python3 %s remove" % (INSTALLED_HOOKS / "gt_sandbox.py"))
        return
    detail = "\n".join(c["problems"] + c["missing"][:20] + c["stale"][:10] + c["notes"])
    if c["state"] != "ok":
        rep.add("sandbox", FAIL, "sandbox mode: ON (%s), settings drifted -- %d missing, %d "
                "stale, %d override(s)" % (mode, len(c["missing"]), len(c["stale"]),
                                          len(c["problems"])), detail, fix=fix)
        return
    if not gt_sandbox.os_sandbox_supported(mode):
        rep.add("sandbox", WARN, "sandbox mode: ON (%s) -- permission rules only: friction, not "
                "a boundary (no Claude Code sandbox on this platform)" % mode, detail)
        return
    rep.add("sandbox", OK, "sandbox mode: ON (%s) -- %s" % (mode, gt_sandbox.ENFORCED[mode]),
            detail)


def check_mcp(rep):
    """MCP servers Claude Code may load that do NOT go through LOTR or gt-vault (0.20.1,
    gt_mcp_inventory.py). They are outside gt unlock and sandbox mode, so they are listed, never
    "ok": a NOTE (orientation -- nothing is broken), OK only when every server is gt's, UNKNOWN
    when a config file could not be read. No header, env or URL value is ever shown."""
    inv_mod = None
    for d in (HERE, INSTALLED_HOOKS):
        if (d / "gt_mcp_inventory.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("gt_mcp_inventory",
                                                          str(d / "gt_mcp_inventory.py"))
            inv_mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(inv_mod)
            except Exception:                       # noqa: BLE001
                inv_mod = None
            break
    if inv_mod is None:
        rep.add("mcp", UNKNOWN, "gt_mcp_inventory.py is not installed in the hooks dir")
        return
    try:
        inv = inv_mod.inventory()
        out = inv_mod.ungated(inv)
    except Exception as e:                          # noqa: BLE001
        rep.add("mcp", UNKNOWN, "the MCP inventory could not run (%s)" % type(e).__name__)
        return
    detail = "\n".join(inv_mod.describe(s) for s in out)
    if inv["problems"]:
        rep.add("mcp", UNKNOWN, "MCP inventory incomplete: %d config file(s) unreadable"
                % len(inv["problems"]), "\n".join(inv["problems"] + ([detail] if detail else [])))
        return
    if out:
        rep.add("mcp", NOTE, "%d MCP server(s) outside LOTR -- not gated by gt unlock, not "
                "contained by sandbox mode (%s)" % (len(out), inv_mod.SECURITY_REF), detail)
        return
    rep.add("mcp", OK, "every MCP server Claude Code may load goes through LOTR or gt-vault "
            "(%d)" % inv["summary"]["gated"])


def check_execution(rep):
    """How work executes here (check: execution): measured vs default parallel profile and its
    age, a Rosetta-translated shell, TMPDIR in a synced folder. One row; WARN only for the two
    that cost every run (translation, a synced TMPDIR) -- an unmeasured profile is the default
    working as designed, so it is reported, not warned about."""
    scripts = os.path.dirname(os.path.abspath(__file__))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import gt_bench
        rows = gt_bench.health()
    except Exception as exc:
        rep.add("execution", UNKNOWN, "could not read the execution profile (%s)"
                % exc.__class__.__name__)
        return
    warn = [r for r in rows if r[0] == "warn"]
    rep.add("execution", WARN if warn else OK, rows[0][1],
            "\n".join(m for _, m, _ in rows[1:]),
            "; ".join(f for _, _, f in warn) if warn else "")


def _windows_utf8():
    """Native Windows (0.19.2): UTF-8 for this process's output and every python it starts.

    Piped, Windows Python writes cp1252: this report's own dashes reached the reader as
    mojibake, and the smoke run of gt_daily.py -- which prints "→" -- died on a
    UnicodeEncodeError, so a healthy install FAILED its gate when the doctor was run by hand
    with no PYTHONUTF8 set (install.sh sets it; a person typing `python gt_doctor.py` does
    not). No-op elsewhere."""
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _windows_utf8()
    ap = argparse.ArgumentParser(description="Golden Thread health check")
    ap.add_argument("--vault", help="vault to check (default: $GT_VAULT, then config)")
    ap.add_argument("--plugin-root", help="plugin source (default: read from settings.json)")
    ap.add_argument("--only", action="append", choices=CHECKS,
                    help="run only this check (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fix", action="store_true",
                    help="apply only repairs that cannot lose work (re-wire hooks)")
    sub = ap.add_subparsers(dest="cmd", metavar="{post-install}")
    pi = sub.add_parser("post-install", help="the release gate: prove this machine is correct "
                                             "for the release (read-only; exit 1 on any FAIL)")
    # SUPPRESS: a subparser default would overwrite a value given before the verb.
    pi.add_argument("--vault", default=argparse.SUPPRESS,
                    help="vault to check (Core rule 2: state it; default $GT_VAULT, then config)")
    pi.add_argument("--plugin-root", default=argparse.SUPPRESS,
                    help="plugin source (default: read from settings.json)")
    pi.add_argument("--release", default=argparse.SUPPRESS,
                    help="the release dir to validate against (default: this file's release "
                         "when its dir is a version, else the installed release, else the "
                         "newest under the plugin root)")
    pi.add_argument("--stage", choices=STAGES, default=argparse.SUPPRESS,
                    help="install/session: rows that need /gt:gt-upgrade are PENDING; "
                         "final (default): they FAIL")
    pi.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                    help="machine-readable output")
    pi.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                    help="write nothing, not even the receipt")
    pi.add_argument("--writer", choices=WRITERS, default=argparse.SUPPRESS,
                    help="who ran this, recorded in the receipt (default: manual)")
    a = ap.parse_args(argv)
    if getattr(a, "cmd", None) == "post-install":
        return post_install_main(a)

    wanted = set(a.only or CHECKS)
    src_root = plugin_root(a.plugin_root)
    root, vdir, source_gone = resolve_release(src_root)
    vault = vault_path(a.vault)

    rep = Report()
    if "version" in wanted:
        if source_gone and vdir is not None:
            # Not `?`: the install is fine and checkable from its cached copy; only "is a
            # newer release available?" has nothing to compare against.
            rep.add("version", NOTE, "gt %s installed; the plugin source it was installed from "
                    "is gone (%s), so whether a newer release exists cannot be checked — the "
                    "other rows use the installed copy" % (vdir.name, src_root),
                    fix='to re-point it, re-run install.sh from wherever the source now lives')
        else:
            check_version(rep, src_root)
    if "components" in wanted:
        check_components(rep, vdir)
    if "wiring" in wanted:
        check_wiring(rep, vdir)
    if "core-rules" in wanted:
        check_core_rules(rep, vault)
    if "modules" in wanted:
        check_modules(rep, root, vdir)
    if "model-policy" in wanted:
        check_model_policy(rep, vdir)
    if "vault" in wanted:
        check_vault(rep, vault, vdir)
    if "schedule" in wanted:
        check_schedule(rep)
    if "workers" in wanted:
        check_workers(rep)
    if "push" in wanted:
        check_push(rep)
    if "gt-src" in wanted:
        check_gt_src(rep)
    if "lint" in wanted:
        check_lint(rep, vault)
    if "astgrep" in wanted:
        check_astgrep(rep)
    if "repo-target" in wanted:
        check_repo_target(rep, vault)
    if "hooks-schema" in wanted:
        check_hooks_schema(rep)
    if "checkers" in wanted:
        check_checkers(rep, root, vdir)
    if "execution" in wanted:
        check_execution(rep)
    if "unlock" in wanted:
        check_unlock(rep)
    if "security" in wanted:
        check_security(rep)
    if "sandbox" in wanted:
        check_sandbox(rep)
    if "scratch" in wanted:
        check_scratch(rep, vault)
    if "write-probe" in wanted:
        check_write_probe(rep, vault)
    if "mcp" in wanted:
        check_mcp(rep)

    if a.fix:
        fix_wiring(rep, vdir, vault)

    # Named separately from `worst` because FAIL outranks UNKNOWN: a run with both
    # exits 1, and without this the unrunnable check would leave no trace in the
    # summary line or the JSON. "Could not check" is never allowed to go quiet.
    unrunnable = [r["check"] for r in rep.rows if r["state"] == UNKNOWN]

    if a.json:
        print(json.dumps({"version_dir": str(vdir) if vdir else None,
                          "vault": str(vault) if vault else None,
                          "worst": rep.worst, "unrunnable": unrunnable,
                          "checks": rep.rows}, indent=2))
    else:
        print("GOLDEN THREAD doctor — release %s, vault %s"
              % (vdir.name if vdir else "UNKNOWN", vault or "UNKNOWN"))
        for r in rep.rows:
            print("%s %-11s %s" % (MARK[r["state"]], r["check"], r["summary"]))
            # Skip a detail line that merely repeats the summary: several of the
            # underlying scripts lead with the same sentence they end with.
            for line in (r["detail"] or "").splitlines():
                if line.strip() and line.strip() != r["summary"].strip():
                    print("        %s" % line)
            if r["fix"] and r["state"] not in (OK, SKIPPED):
                print("        fix: %s" % r["fix"])
        footer = {OK: "all clear",
                  WARN: "needs attention",
                  FAIL: "broken",
                  UNKNOWN: "a check could not run — that is not the same as clean"
                  }[rep.worst]
        if unrunnable and rep.worst != UNKNOWN:
            footer += (" — and %d check(s) could not run at all (%s), so this report "
                       "does not cover them" % (len(unrunnable), ", ".join(unrunnable)))
        print("\n%s" % footer)
    return {OK: 0, WARN: 1, FAIL: 1, UNKNOWN: 2}[rep.worst]


def fix_wiring(rep, vdir, vault):
    """The only repair offered: re-register hooks. It adds entries and removes none,
    so nothing a user wrote can be lost. Every other finding needs a human."""
    row = next((r for r in rep.rows if r["check"] == "wiring"), None)
    if not row or row["state"] == OK or not vdir or not vault:
        return
    rc, out = run([sys.executable, str(vdir / "scripts" / "vault_init.py"),
                   "install-core-rules", "--vault", str(vault)])
    row["detail"] = (row["detail"] + "\n--fix: " +
                     ("re-wired" if rc == 0 else "could not re-wire: " + out[-200:]))
    if rc == 0:
        row["state"] = OK
        row["summary"] += " (re-wired by --fix)"


if __name__ == "__main__":
    raise SystemExit(main())
