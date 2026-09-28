#!/usr/bin/env python3
"""One command for the whole health picture: `gt_doctor.py`.

    gt_doctor.py                 # every check, human-readable
    gt_doctor.py --json          # the same, machine-readable
    gt_doctor.py --fix           # apply only the repairs that cannot lose work
    gt_doctor.py --only wiring   # one check

Exit code: 0 all clear, 1 something needs attention, 2 a check could not run.

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
  vault       is the vault reachable, and are its generated files migrated?
  workers     are there background processes nobody declared?
  push        do this machine's commits exist anywhere else?
  gt-src      does the publish destination still match what was published?
              (only where one is configured: $GT_SRC or `gt_src` in vault-config.json)
  lint        what does the vault linter say, in one line?

A check that cannot run says so and exits 2. "Could not check" is never "clean" --
that distinction is the whole reason this file exists.
"""
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
    """Where the Core rules live: the conventional path, else the folder holding the
    priority model (which is what names a core-rules folder, not its name alone)."""
    conventional = vault / "Projects" / "golden-thread" / "core-rules"
    if (conventional / "core_rule_priority_model.md").is_file():
        return conventional
    for cand in sorted(vault.rglob("core-rules")):
        if (cand / "core_rule_priority_model.md").is_file():
            return cand
    return conventional if conventional.is_dir() else None


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
        found = any(c == str(path) or (shlex.split(c) or [""])[0] == str(path)
                    for c in wired.get(event, []))
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
        rep.add("core-rules", OK,
                "%d Core rule(s), each with its mechanism wired" % rules, "\n".join(lines))


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
                fixes.add('bash "%s/install.sh"' % root)
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
                fixes.add('bash "%s/install.sh"' % root)
                lines[-1] += "; but plugin %s is still %s" % (
                    key, "installed" if present else "enabled")
    n_on = sum(1 for d in det.values() if d["state"] == "on")
    summary = "%d module(s): %d on, %d off" % (len(det), n_on, len(det) - n_on)
    if problems:
        rep.add("modules", WARN, summary + " — %d need attention" % len(problems),
                "\n".join(lines), fix=" ; ".join(sorted(fixes)) or "fix the module.json")
    else:
        rep.add("modules", OK, summary, "\n".join(lines))


def check_vault(rep, vault):
    if not vault:
        rep.add("vault", UNKNOWN, "no vault configured",
                fix="run /gt:gt-init, or set GT_VAULT")
        return
    if not vault.is_dir():
        rep.add("vault", FAIL, "configured vault does not exist: %s" % vault)
        return
    pending = []
    spool = vault / "Projects" / "golden-thread" / "spool"
    log = vault / "log.md"
    if log.is_file() and not (spool / "log" / "0000-baseline.md").is_file():
        pending.append("log.md is not migrated to the spool model")
    unmigrated = []
    for dec in sorted(list((vault / "Projects").glob("*/decisions.md"))
                      + list((vault / "Projects").glob("*/*/decisions.md"))):
        slug = str(dec.parent.relative_to(vault / "Projects"))
        if not (spool / "decisions" / slug / "0000-baseline.md").is_file():
            unmigrated.append(slug)
    if unmigrated:
        pending.append("%d project(s) with an unmigrated decisions.md: %s"
                       % (len(unmigrated), ", ".join(unmigrated[:5])
                          + (" …" if len(unmigrated) > 5 else "")))
    if pending:
        rep.add("vault", WARN, "%s — %d pending migration(s)" % (vault.name, len(pending)),
                "\n".join(pending),
                fix="tools/gt_log.py migrate --dry-run, then tools/gt_adr.py migrate "
                    "<project> --dry-run (drop --dry-run to apply)")
    else:
        rep.add("vault", OK, "%s — generated files migrated" % vault.name)


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
    if "clean" in out:
        rep.add("workers", OK, line.strip())
    else:
        rep.add("workers", WARN, line.strip(), out.strip()[:800],
                fix="python3 %s reap --dry-run   (then without --dry-run)" % script)


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
    elif "nothing to check" in text:
        # No vault repo, or no vault: the question does not apply here.
        rep.add("push", SKIPPED, first)
    elif "skipped." in text:
        # git missing, git timed out, branch unreadable: it could not answer.
        rep.add("push", UNKNOWN, first)
    else:
        rep.add("push", WARN, first, text[-800:], fix="git -C <vault> push")


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


CHECKS = ("version", "components", "wiring", "core-rules", "modules", "vault",
          "workers", "push", "gt-src", "lint")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Golden Thread health check")
    ap.add_argument("--vault", help="vault to check (default: $GT_VAULT, then config)")
    ap.add_argument("--plugin-root", help="plugin source (default: read from settings.json)")
    ap.add_argument("--only", action="append", choices=CHECKS,
                    help="run only this check (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fix", action="store_true",
                    help="apply only repairs that cannot lose work (re-wire hooks)")
    a = ap.parse_args(argv)

    wanted = set(a.only or CHECKS)
    root = plugin_root(a.plugin_root)
    vdir = version_dir(root)
    vault = vault_path(a.vault)

    rep = Report()
    if "version" in wanted:
        check_version(rep, root)
    if "components" in wanted:
        check_components(rep, vdir)
    if "wiring" in wanted:
        check_wiring(rep, vdir)
    if "core-rules" in wanted:
        check_core_rules(rep, vault)
    if "modules" in wanted:
        check_modules(rep, root, vdir)
    if "vault" in wanted:
        check_vault(rep, vault)
    if "workers" in wanted:
        check_workers(rep)
    if "push" in wanted:
        check_push(rep)
    if "gt-src" in wanted:
        check_gt_src(rep)
    if "lint" in wanted:
        check_lint(rep, vault)

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
