#!/usr/bin/env python3
"""gt_setup -- finish a Golden Thread install from inside the Claude prompt (0.21.3).

    gt_setup.py status [--json]      exit 0 when this machine is set up, 1 when it is not
    gt_setup.py hook                 the SessionStart one-liner (plugin hooks.json runs it only
                                     while status says "not set up")
    gt_setup.py plan [--json]        everything /gt:gt-setup needs to ask: the installer's path,
                                     the module question, the installer's own questions, the vault
    gt_setup.py run  --modules everything|core|choose [--with NAME ...] [--set KEY=VALUE ...]
                     [--vault PATH | --no-vault] [--dry-run]
    gt_setup.py run  --recommended | --starter [--set lockdown=L] [--vault PATH | --no-vault]
                                     the express choices (0.22.0): --recommended is every module with the
                                     installer's own defaults (--accept-defaults); --starter is the small
                                     install (install.sh --starter)
                                     run the installer non-interactively with those answers; no
                                     vault given: the configured one, else <home>/GoldenThread
    gt_setup.py only NAME [--check]   add or update ONE module (install.sh --only NAME): no questions,
                                     the vault untouched, ends with a self-proof of that module
    gt_setup.py check-vault PATH [--json]
                                     is PATH a safe place for a vault? exit 1 with a plain-words
                                     warning when it is in Documents or on a synced or shared drive

WHY THIS EXISTS. Until 0.21.3 the only install was `bash install.sh` in a terminal. A person who
added the marketplace and installed gt from the Claude prompt (`/plugin marketplace add
<github repo>/golden-thread.git`, `/plugin install gt-all@golden-thread-plugin`) had the plugins in Claude's
cache and NOTHING else: no hooks wired, no scripts in ~/.claude/golden-thread/hooks, no security
level chosen, no vault. Claude Code installs plugins; it cannot run a machine-level installer. This
script is the bridge: the first session says so in one line, and /gt:gt-setup asks (with
AskUserQuestion) and then runs install.sh with the answers, so the person never needs a terminal.

WHAT IT NEVER DOES. It does not answer a question for the person: `run` passes exactly the
`--set` answers it is given, and the installer itself refuses to start on a new machine until the
security questions have one (exit 5). It does not write settings, hooks or the vault itself; the
installer does, with its backups and its post-install validation. `run` is a plain argument list,
never a shell string.

Standard library only; writes no bytecode (the shipped tree must keep matching its SHA256SUMS).
"""
import sys as _sys
import os as _os
_sys.dont_write_bytecode = True
_os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gt_vault_location  # noqa: E402  (beside this file: one helper for every vault-path chooser)
MARKET = "golden-thread-plugin"
BUNDLE = "gt-all"


def home():
    return os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")


def gt_dir():
    return os.path.join(home(), ".claude", "golden-thread")


# --------------------------------------------------------------------------------- is it set up?
def machine_state():
    """-> (done, reasons). Done means the installer has run on this machine: its hook scripts are in
    ~/.claude/golden-thread/hooks (gt_paths.py is the file every hook locates the rules with) and
    it wired the plugin hooks. Reading two paths, because the hook that asks runs every session."""
    reasons = []
    if not os.path.isfile(os.path.join(gt_dir(), "hooks", "gt_paths.py")):
        reasons.append("the hook scripts are not installed (~/.claude/golden-thread/hooks)")
    wired = os.path.join(gt_dir(), "wired")
    try:
        if not any(not n.startswith(".") for n in os.listdir(wired)):
            raise OSError
    except OSError:
        reasons.append("no hooks are wired (~/.claude/golden-thread/wired)")
    return (not reasons), reasons


def vault_state():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            p = json.load(fh).get("vault_path") or ""
    except (OSError, ValueError, AttributeError):
        p = ""
    return {"configured": bool(p) and os.path.isdir(p), "path": p}


# ------------------------------------------------------------------------------ finding install.sh
def _is_installer(path):
    root = os.path.dirname(path)
    return (os.path.isfile(path) and os.path.isdir(os.path.join(root, "golden-thread")))


def find_installer():
    """-> (path or None, how). The checkout that holds install.sh next to the plugin directories:
    GT_INSTALLER, the folder Claude Code registered for the marketplace, or the tree this script
    was run from."""
    cands = []
    env = os.environ.get("GT_INSTALLER")
    if env:
        cands.append((env, "GT_INSTALLER"))
    try:
        with open(os.path.join(home(), ".claude", "plugins", "known_marketplaces.json"),
                  encoding="utf-8") as fh:
            ent = json.load(fh).get(MARKET) or {}
        loc = ent.get("installLocation") or (ent.get("source") or {}).get("path") or ""
        if loc:
            cands.append((os.path.join(loc, "golden-thread-plugin", "install.sh"), "marketplace"))
            cands.append((os.path.join(loc, "install.sh"), "marketplace"))
    except (OSError, ValueError, AttributeError):
        pass
    cands.append((os.path.join(home(), ".claude", "plugins", "marketplaces", MARKET,
                               "golden-thread-plugin", "install.sh"), "marketplace clone"))
    cands.append((os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
                               "install.sh"), "this tree"))
    for path, how in cands:
        if _is_installer(path):
            return os.path.abspath(path), how
    return None, ""


# ------------------------------------------------------------------------------------ the modules
def modules_for(installer):
    """-> [{name, plugin, default, in_bundle}] from the tree beside install.sh."""
    root = os.path.dirname(installer)
    out, bundle = [], []
    try:
        with open(os.path.join(root, "bundles", BUNDLE, ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as fh:
            bundle = json.load(fh).get("dependencies") or []
    except (OSError, ValueError, AttributeError):
        pass
    for d in sorted(os.listdir(root)):
        dd = os.path.join(root, d)
        if not d.startswith("golden-thread-") or not os.path.isdir(dd):
            continue
        vers = [v for v in os.listdir(dd) if re.fullmatch(r"\d+\.\d+\.\d+", v)
                and os.path.isfile(os.path.join(dd, v, "module.json"))]
        if not vers:
            continue
        v = max(vers, key=lambda x: tuple(int(n) for n in x.split(".")))
        try:
            with open(os.path.join(dd, v, "module.json"), encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"name": m.get("name"), "plugin": m.get("plugin"), "default": m.get("default"),
                    "summary": m.get("summary", ""), "in_bundle": m.get("plugin") in bundle})
    return sorted(out, key=lambda m: m["name"] or "")


MODULE_QUESTION = {
    "key": "modules",
    "prompt": "Which Golden Thread modules do you want?",
    "options": [
        {"value": "everything", "label": "Install everything (recommended)",
         "explain": "gt and every module in the gt-all bundle: {bundle}. Nothing more to install later."},
        {"value": "choose", "label": "Choose modules",
         "explain": "gt plus the modules you pick; ask about each one."},
        {"value": "core", "label": "Core only",
         "explain": "gt on its own. Add a module later with /gt:gt-setup."},
    ],
}


def claude_code():
    """0.22.0: the Claude Code version on this machine and whether it is below the minimum gt wants
    (2.1.300: the auto-compact busy check). A version that cannot be read claims nothing."""
    try:
        import gt_cc_version
        ver, _text, _path = gt_cc_version.installed()
        minimum = gt_cc_version.MIN
        need = bool(ver) and ver < gt_cc_version.parse(minimum)
    except Exception:                               # noqa: BLE001 -- a version note never breaks setup
        return {"version": None, "minimum": "2.1.300", "needs_update": False, "say": ""}
    version = ".".join(str(x) for x in ver) if ver else None
    say = ("Claude Code %s is older than %s. Update it first: in a terminal run `claude update`, then "
           "`claude --version` to check, then restart Claude Code and run /gt:gt-setup again. gt installs "
           "from 2.1.292, but the in-session cut and auto_compact need %s." % (version, minimum, minimum)) if need else ""
    return {"version": version, "minimum": minimum, "needs_update": need, "say": say}


def plan():
    installer, how = find_installer()
    mods = modules_for(installer) if installer else []
    q = json.loads(json.dumps(MODULE_QUESTION))
    bundle = ", ".join(m["name"] for m in mods if m["in_bundle"]) or "none found"
    for o in q["options"]:
        o["explain"] = o["explain"].format(bundle=bundle)
    done, reasons = machine_state()
    return {"set_up": done, "not_set_up_because": reasons, "installer": installer,
            "installer_found_via": how, "modules": mods, "module_question": q,
            "claude_code": claude_code(),
            "vault": vault_state(), "default_vault": default_vault(),
            # 0.21.4: a configured vault in Documents or on a synced or shared drive is said once,
            # here, as a warning the skill shows; the person may keep it.
            "vault_location": location(vault_state()["path"]) if vault_state()["configured"] else [],
            "questions_command": ([bash_path(), installer, "--questions", "--json"] if installer else None)}


def default_vault():
    """<home>/GoldenThread (0.21.4): local, unsynced, outside Documents. It was
    ~/Documents/Obsidian/GoldenThreadVault, which macOS and Windows both guard."""
    return gt_vault_location.recommended(env={"HOME": home(), "USERPROFILE": home()})


def location(path):
    """-> the location findings for a vault path ([] = no concern)."""
    try:
        return gt_vault_location.assess(path, home=home())
    except Exception:                               # noqa: BLE001 -- a warning helper never breaks setup
        return []


# ---------------------------------------------------------------------------------------- the run
KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def bash_path():
    """-> the bash that runs install.sh. On Windows, Git for Windows puts only Git\\cmd on PATH, not
    Git\\bin, so a bare "bash" is not found from PowerShell or cmd (a Windows test machine, 2026-10-13: WinError 2).
    Look in Git's usual places; elsewhere "bash" from PATH is right."""
    if sys.platform != "win32":
        return "bash"
    import shutil
    found = shutil.which("bash")
    if found and "system32" not in found.lower():         # System32\\bash.exe is WSL's, not Git's
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs")):
        if base:
            cand = os.path.join(base, "Git", "bin", "bash.exe")
            if os.path.isfile(cand):
                return cand
    return "bash"


def build_command(installer, modules, withs, sets, vault, no_vault, answers_path,
                  accept_defaults=False, starter=False):
    """-> the installer argument list for these answers. Pure, so tests can pin it.

    `accept_defaults` is the recommended express choice (phase 134): the installer's own defaults, as
    `install.sh --accept-defaults`. `starter` is the small install (phase 116); it takes no module
    choice, because the installer makes it core only."""
    cmd = [bash_path(), installer]
    if starter:
        if modules or withs:
            raise ValueError("--starter installs core only; give no --modules or --with")
        cmd.append("--starter")
    elif modules == "everything":
        cmd.append("--all-modules")
    elif modules == "core":
        cmd.append("--core-only")
    elif modules == "choose":
        cmd.append("--core-only")                 # only the modules named with --with are on
        for n in withs:
            cmd += ["--with", n]
    else:
        raise ValueError("--modules must be everything, core or choose")
    if modules == "core" and withs:
        raise ValueError("--with is for --modules choose")
    if vault and no_vault:
        raise ValueError("--vault and --no-vault contradict each other")
    if vault:
        cmd += ["--vault", vault]
    elif no_vault:
        cmd.append("--no-vault")
    else:
        raise ValueError("give --vault PATH, or --no-vault")
    if answers_path:
        cmd += ["--answers", answers_path]
    if accept_defaults:
        cmd.append("--accept-defaults")
    return cmd


def cmd_run(argv):
    modules, withs, sets, vault, no_vault, dry = None, [], {}, "", False, False
    recommended = starter = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--modules", "--with", "--set", "--vault") and i + 1 < len(argv):
            v = argv[i + 1]
            if a == "--modules":
                modules = v
            elif a == "--with":
                withs.append(v)
            elif a == "--vault":
                vault = v
            else:
                k, _, val = v.partition("=")
                if not KEY.match(k) or not _:
                    print("--set needs KEY=VALUE with a question key, got %r" % v, file=sys.stderr)
                    return 2
                sets[k] = val
            i += 2
        elif a == "--no-vault":
            no_vault = True
            i += 1
        elif a == "--dry-run":
            dry = True
            i += 1
        elif a == "--recommended":
            recommended = True
            i += 1
        elif a == "--starter":
            starter = True
            i += 1
        else:
            print("unknown argument: %s" % a, file=sys.stderr)
            return 2
    # the express choices (phase 134): the installer's defaults for everything, or the small install
    if recommended and starter:
        print("--recommended and --starter contradict each other; give one", file=sys.stderr)
        return 2
    if recommended:
        if sets or withs or modules not in (None, "everything"):
            print("--recommended is every module with the installer's defaults; it takes no --set, "
                  "--with or other --modules (use customise for those)", file=sys.stderr)
            return 2
        modules = "everything"
    if not vault and not no_vault:
        # Headless with no vault named (0.21.4): the vault this machine already has, else the
        # recommended <home>/GoldenThread -- never a stop for a question nobody can answer here.
        vs = vault_state()
        vault = vs["path"] if vs["configured"] else default_vault()
        print("No vault path given: using %s%s" % (vault, " (the configured vault)"
                                                    if vs["configured"] else " (the default)"),
              file=sys.stderr)
    if vault:
        found = location(vault)
        if found:
            print(gt_vault_location.warning(vault, found, default_vault()), file=sys.stderr)
    installer, _how = find_installer()
    if not installer:
        print("Cannot find install.sh. It lives in the golden-thread-plugin folder of the checkout "
              "Claude Code registered for the marketplace (try: claude plugin marketplace list), "
              "or point GT_INSTALLER at it.", file=sys.stderr)
        return 3
    answers = None
    if sets:
        fd, answers = tempfile.mkstemp(prefix="gt-setup-answers.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(sets, fh)
    try:
        try:
            cmd = build_command(installer, modules, withs, sets, vault, no_vault, answers,
                                accept_defaults=recommended, starter=starter)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        shown = list(cmd)
        if answers:
            shown[shown.index("--answers") + 1] = "<answers: %s>" % json.dumps(sets, sort_keys=True)
        print("$ " + " ".join(shown))
        if dry:
            return 0
        # Not a terminal, on purpose: nothing in the installer may block on a prompt here. A new
        # machine with a security question unanswered stops with exit 5 and names the flag.
        return subprocess.call(cmd, stdin=subprocess.DEVNULL)
    finally:
        if answers:
            try:
                os.unlink(answers)
            except OSError:
                pass


# -------------------------------------------------------------------------------------- the hook
NUDGE = ("Golden Thread is installed but not set up on this machine: no hooks are wired and no "
         "security level is chosen. Run /gt:gt-setup (about a minute, asked here in the prompt).")


def cmd_hook():
    done, _reasons = machine_state()
    if done:
        return 0
    json.dump({"systemMessage": "gt: " + NUDGE,
               "hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": NUDGE + " If the user asks to set up "
                                      "or install gt, use the gt-setup skill."}}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "status":
        done, reasons = machine_state()
        if "--json" in rest:
            print(json.dumps({"set_up": done, "reasons": reasons}))
        else:
            print("gt is set up on this machine." if done else
                  "gt is NOT set up on this machine: " + "; ".join(reasons))
        return 0 if done else 1
    if cmd == "hook":
        return cmd_hook()
    if cmd == "plan":
        p = plan()
        if "--json" in rest:
            print(json.dumps(p, indent=2))
        else:
            print("set up: %s" % ("yes" if p["set_up"] else "no"))
            print("installer: %s (%s)" % (p["installer"] or "NOT FOUND", p["installer_found_via"] or "-"))
            print("modules: %s" % ", ".join("%s%s" % (m["name"], "*" if m["in_bundle"] else "")
                                            for m in p["modules"]))
            print("vault: %s" % (p["vault"]["path"] if p["vault"]["configured"] else "none configured"))
        return 0
    if cmd == "run":
        return cmd_run(rest)
    if cmd == "only" and rest and not rest[0].startswith("-"):
        # One module without the whole install (0.21.4). install.sh refuses anything else
        # itself (gt not installed, another gt release, an unknown name) and rolls back a failure.
        extra = [a for a in rest[1:] if a in ("--check", "--dry-run")]
        if len(extra) != len(rest) - 1:
            print("usage: gt_setup.py only NAME [--check]", file=sys.stderr)
            return 2
        installer, _how = find_installer()
        if not installer:
            print("Cannot find install.sh; point GT_INSTALLER at it.", file=sys.stderr)
            return 3
        cmd_ = [bash_path(), installer, "--only", rest[0]] + extra
        print("$ " + " ".join(cmd_))
        sys.stdout.flush()
        return subprocess.call(cmd_, stdin=subprocess.DEVNULL)
    if cmd == "check-vault" and rest and not rest[0].startswith("--"):
        found = location(rest[0])
        if "--json" in rest:
            print(json.dumps({"path": rest[0], "risky": bool(found), "findings": found,
                              "recommended": default_vault()}, indent=1))
        elif found:
            print(gt_vault_location.warning(rest[0], found, default_vault()))
        else:
            print("%s: no concern (not in Documents, not on a synced or shared drive)" % rest[0])
        return 1 if found else 0
    print(__doc__.split("WHY THIS EXISTS")[0].strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
