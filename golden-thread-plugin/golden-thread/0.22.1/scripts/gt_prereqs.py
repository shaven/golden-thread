#!/usr/bin/env python3
"""gt_prereqs.py -- what an Obsidian-first person needs on this machine, and how to get it (0.22.0, phase 89b).

    gt_prereqs.py check   [--vault V] [--os mac|linux|windows] [--json | --brief]
    gt_prereqs.py plan    [--vault V] [--os ...]            the exact command for everything missing
    gt_prereqs.py install <name> [--yes] [--vault V] [--os ...]
    gt_prereqs.py plugin  <terminal|realclaudian> [--yes] [--vault V]   a pinned Obsidian plugin, by sha256 (asks first)

THE SEVEN ROWS: git, python, obsidian, claude (the Claude Code CLI), claude-login (signed in), terminal-plugin
(Obsidian's Terminal) and claudian-plugin (Claudian, folder `realclaudian` in this vault). A row is `ok`,
`missing`, `untested` (a plugin version this release was not tested against: said, never failed), or
`blocked` (it needs an earlier row first: sign-in needs the CLI, the plugins need a vault).

ASK FIRST, SHOW THE COMMAND. `install` prints the exact command and runs nothing unless `--yes` is given (or,
at a terminal, a typed yes). Not at a terminal and no `--yes`: exit 5 with the command printed, the same
"needs an answer" exit the install questions use. gt itself runs only a package manager (brew, winget) as an
argv list, no shell, no sudo. A step that needs an administrator, pipes a download into a shell, opens a
system dialog or is a sign-in is the person's: it is printed ("you run this") and gt exits 6; it is never run.

PLUGINS are never bundled. `plugin` fetches nothing without `--yes` (or a typed yes at a terminal; else exit 5), then downloads each file from its pinned https URL and writes it only when
every file's sha256 matches its pin, all or nothing, into <vault>/.obsidian/plugins/<id>/. PLUGIN_PINS holds the
pins; a plugin with no pin shipped is refused (exit 7), never fetched unchecked. TESTED is the version of each
plugin this release's GT Panel work was tried against.

The sign-in check looks only for the presence of Claude Code's account entry in ~/.claude.json; it never
returns or prints what it read. Stdlib only; GT_PREREQS_FAKE (a JSON probe, honoured only under
GT_TEST_SANDBOX) lets tests run the CLI on a pretend machine.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

EXIT_NEEDS_ANSWER, EXIT_USER_STEP, EXIT_NO_PIN, EXIT_BAD_PIN = 5, 6, 7, 8
NAMES = ("git", "python", "obsidian", "claude", "claude-login", "terminal-plugin", "claudian-plugin")
PLUGIN_IDS = {"terminal-plugin": "terminal", "claudian-plugin": "realclaudian"}
# the versions the panel work was tried against (phase 89; the item plan names both)
TESTED = {"terminal": "3.28.0", "realclaudian": "2.3.16"}
# id -> {version, files: {name: {url, sha256}}}. The hashes are those of the GitHub release assets as
# downloaded 2026-10-25 (an installed main.js is the asset plus Obsidian's trailing `/* nosourcemap */` line).
PLUGIN_PINS = {
    "terminal": {"version": "3.28.0", "files": {
        "main.js": {"url": "https://github.com/polyipseity/obsidian-terminal/releases/download/3.28.0/main.js",
                    "sha256": "60a01a191fd9eafb7af775a7e8c4cc63c54d882344876707ba59d9b5b6dceae1"},
        "manifest.json": {"url": "https://github.com/polyipseity/obsidian-terminal/releases/download/3.28.0/manifest.json",
                          "sha256": "01aae391be0037b4b978967b6ccdccc11de59628771c3b594878bf7480415a4c"},
        "styles.css": {"url": "https://github.com/polyipseity/obsidian-terminal/releases/download/3.28.0/styles.css",
                       "sha256": "39ab74656d85ed316e92d94b891eb9800a6244bb2a4375916065e51703f572bb"}}},
    "realclaudian": {"version": "2.3.16", "files": {
        "main.js": {"url": "https://github.com/YishenTu/claudian/releases/download/2.3.16/main.js",
                    "sha256": "3aeee613148c279a7d7fe54d9008d4a228acdf1fb4f34b23aa455b3bdcbafabd"},
        "manifest.json": {"url": "https://github.com/YishenTu/claudian/releases/download/2.3.16/manifest.json",
                          "sha256": "b606d456e3e5a7e3002eb48d79038e5a046220926d5ff89883662fe92fd1893f"},
        "styles.css": {"url": "https://github.com/YishenTu/claudian/releases/download/2.3.16/styles.css",
                       "sha256": "810220ceabb22a4648141c1db1bb4d9b9b46e5a6f1cac79378979e267890983c"}}},
}
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
TOOLS = {"git": "git", "python": "python3", "obsidian": "obsidian", "claude": "claude"}
WHAT = {
    "git": "git, to keep your vault's history",
    "python": "Python 3, which gt's scripts run on",
    "obsidian": "Obsidian, the notes app your vault opens in",
    "claude": "the Claude Code command-line tool",
    "claude-login": "a Claude sign-in",
    "terminal-plugin": "Obsidian's Terminal plugin (a Claude Code terminal inside Obsidian)",
    "claudian-plugin": "the Claudian plugin (Claude in an Obsidian side panel)",
}


def _cmd(argv=None, who="user", shown="", manual=""):
    return {"argv": argv or [], "who": who, "shown": shown or " ".join(argv or []), "manual": manual}


# who: "gt" = a package manager gt may run after a yes; "user" = admin, piped, windowed or manual.
COMMANDS = {
    "mac": {
        "git": _cmd(["xcode-select", "--install"], "user", manual="a macOS window asks you to confirm"),
        "python": _cmd(["brew", "install", "python@3.12"], "gt"),
        "obsidian": _cmd(["brew", "install", "--cask", "obsidian"], "gt"),
        "claude": _cmd([], "user", "curl -fsSL https://claude.ai/install.sh | bash",
                       "the official installer pipes a download into a shell: you run it"),
    },
    "linux": {
        "git": _cmd(["sudo", "apt-get", "install", "git"], "user", manual="needs an administrator"),
        "python": _cmd(["sudo", "apt-get", "install", "python3"], "user", manual="needs an administrator"),
        "obsidian": _cmd([], "user", manual="download it from https://obsidian.md/download"),
        "claude": _cmd([], "user", "curl -fsSL https://claude.ai/install.sh | bash",
                       "the official installer pipes a download into a shell: you run it"),
    },
    "windows": {
        "git": _cmd(["winget", "install", "--id", "Git.Git", "-e"], "gt"),
        "python": _cmd(["winget", "install", "--id", "Python.Python.3.12", "-e"], "gt"),
        "obsidian": _cmd(["winget", "install", "--id", "Obsidian.Obsidian", "-e"], "gt"),
        "claude": _cmd([], "user", "irm https://claude.ai/install.ps1 | iex",
                       "the official installer pipes a download into a shell: you run it"),
    },
}
LOGIN_HOW = "run `claude` once and follow its sign-in prompt"


def os_of(platform=None):
    p = platform or sys.platform
    return "mac" if p == "darwin" else "windows" if p.startswith(("win", "cygwin")) else "linux"


class RealProbe:
    def which(self, name):
        return shutil.which(name)

    def exists(self, path):
        return os.path.exists(path)

    def read(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def run(self, argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True)
            return p.returncode, (p.stdout + p.stderr)[-2000:]
        except OSError as exc:
            return 127, str(exc)

    def has_login(self):
        """True when ~/.claude.json carries an account entry. Presence only: nothing read is returned."""
        raw = self.read(os.path.join(os.path.expanduser("~"), ".claude.json"))
        try:
            return bool(json.loads(raw or "{}").get("oauthAccount"))
        except (ValueError, AttributeError):
            return False


class FakeProbe(RealProbe):
    """A pretend machine from GT_PREREQS_FAKE, for tests of the CLI only."""
    def __init__(self, spec):
        self.spec = spec
        self.ran = []

    def which(self, name):
        return "/fake/bin/" + name if name in self.spec.get("tools", []) else None

    def exists(self, path):
        return str(path) in self.spec.get("files", {})

    def read(self, path):
        return self.spec.get("files", {}).get(str(path))

    def run(self, argv):
        self.ran.append(argv)
        return 0, ""

    def has_login(self):
        return bool(self.spec.get("signed_in"))


def _plugin_row(name, probe, vault):
    pid = PLUGIN_IDS[name]
    if not vault:
        return {"state": "blocked", "detail": "no vault to look in yet", "how": "gt"}
    mf = os.path.join(vault, ".obsidian", "plugins", pid, "manifest.json")
    raw = probe.read(mf)
    if raw is None:
        return {"state": "missing", "detail": "not installed in this vault", "how": "gt"}
    try:
        ver = str(json.loads(raw).get("version") or "")
    except (ValueError, AttributeError):
        ver = ""
    if ver and ver != TESTED[pid]:
        return {"state": "untested", "detail": "version %s; gt's panel was tried against %s" % (ver, TESTED[pid]),
                "how": "gt"}
    return {"state": "ok", "detail": "version %s" % (ver or "unknown"), "how": "gt"}


def check(probe, vault, os_name):
    """-> one row per NAMES entry: {name, what, state, detail, how}. how: who acts (gt after a yes, or user)."""
    rows = []
    for n in NAMES:
        if n in TOOLS:
            found = probe.which(TOOLS[n])
            if n == "obsidian" and not found and os_name == "mac":
                found = "/Applications/Obsidian.app" if probe.exists("/Applications/Obsidian.app") else None
            how = COMMANDS[os_name][n]["who"]
            row = {"state": "ok" if found else "missing", "detail": found or "not found", "how": how}
        elif n == "claude-login":
            if not probe.which("claude"):
                row = {"state": "blocked", "detail": "install the Claude Code tool first", "how": "user"}
            else:
                ok = probe.has_login()
                row = {"state": "ok" if ok else "missing", "detail": "signed in" if ok else "not signed in",
                       "how": "user"}
        else:
            row = _plugin_row(n, probe, vault)
        rows.append(dict(row, name=n, what=WHAT[n]))
    return rows


def _command_text(name, os_name):
    c = COMMANDS[os_name].get(name)
    if name == "claude-login":
        return LOGIN_HOW
    return (c["shown"] if c and c["shown"] else (c or {}).get("manual", "")) if c else ""


def plan_text(rows, os_name):
    out = []
    for r in rows:
        if r["state"] != "missing":
            continue
        n = r["name"]
        if n in PLUGIN_IDS:
            out.append("%s -- %s\n    gt_prereqs.py plugin %s   (downloads the pinned files; asks first)"
                       % (n, r["what"], PLUGIN_IDS[n]))
            continue
        c = COMMANDS[os_name].get(n)
        cmd = _command_text(n, os_name)
        who = "gt runs this after your yes" if c and c["who"] == "gt" else "you run this yourself"
        extra = "" if not c or not c["manual"] else " (%s)" % c["manual"]
        out.append("%s -- %s\n    %s    [%s]%s" % (n, r["what"], cmd, who, extra))
    return "\n".join(out) + ("\n" if out else "")


def brief_text(rows):
    gaps = [r for r in rows if r["state"] == "missing"]
    if not gaps:
        return ""
    lines = ["Still to set up for the Obsidian way of working: %s." % ", ".join(r["name"] for r in gaps),
             "  See each exact command with: gt_prereqs.py plan   (nothing is installed without your yes)"]
    return "\n".join(lines) + "\n"


def install(name, probe, vault, os_name, yes=False):
    """-> (exit code, text). Shows the command; runs it only for a gt-class step after a yes."""
    if name not in NAMES:
        return 2, "unknown item %r; one of: %s\n" % (name, ", ".join(NAMES))
    if name in PLUGIN_IDS:
        return install_plugin(PLUGIN_IDS[name], vault, yes=yes)
    row = next(r for r in check(probe, vault, os_name) if r["name"] == name)
    if row["state"] == "ok":
        return 0, "%s: already there (%s); nothing to do.\n" % (name, row["detail"])
    c = COMMANDS[os_name].get(name)
    if name == "claude-login" or c is None or c["who"] != "gt":
        how = _command_text(name, os_name)
        return EXIT_USER_STEP, "%s: this step is yours, gt does not run it: %s\n" % (name, how)
    shown = c["shown"]
    if not yes:
        return EXIT_NEEDS_ANSWER, ("%s: gt would run, once you say yes:\n    %s\n"
                                   "  Run it again with --yes to go ahead.\n" % (name, shown))
    rc, out = probe.run(c["argv"])
    return (0 if rc == 0 else 1), "%s: ran `%s` (exit %d)\n%s" % (name, shown, rc, out)


def _pin_problems(pin):
    probs = []
    for fname, f in (pin.get("files") or {}).items():
        if fname not in PLUGIN_FILES:
            probs.append("%s is not a plugin file name" % fname)
        elif not (isinstance(f, dict) and str(f.get("url", "")).startswith("https://")):
            probs.append("%s: the pinned address must be https" % fname)
        elif not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
            probs.append("%s: no valid sha256 pin" % fname)
    return probs


def _fetch(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as r:             # noqa: S310 - https only, checked above
        return r.read(20 * 1024 * 1024)


def install_plugin(pid, vault, fetch=None, pins=None, yes=True):
    """-> (exit code, text). All files verified against their pinned sha256 BEFORE anything is written."""
    pins = PLUGIN_PINS if pins is None else pins
    pin = pins.get(pid)
    if not pin or not pin.get("files"):
        return EXIT_NO_PIN, ("%s: this release ships no pinned download, so gt fetches nothing. "
                             "Install it from Obsidian's Community plugins (version %s was tried).\n"
                             % (pid, TESTED.get(pid, "?")))
    if not vault:
        return 2, "%s: no vault given (--vault) to install into.\n" % pid
    probs = _pin_problems(pin)
    got = {}
    if not probs:
        for fname, f in pin["files"].items():
            try:
                data = (fetch or _fetch)(f["url"])
            except Exception as exc:                               # noqa: BLE001 - reported, nothing written
                probs.append("%s: could not be fetched (%s)" % (fname, type(exc).__name__))
                continue
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                probs.append("%s: sha256 does not match the pin" % fname)
            got[fname] = data
    if probs:
        return EXIT_BAD_PIN, "%s: nothing written. %s\n" % (pid, "; ".join(probs))
    dest = os.path.join(vault, ".obsidian", "plugins", pid)
    os.makedirs(dest, exist_ok=True)
    for fname, data in got.items():
        with open(os.path.join(dest, fname), "wb") as fh:
            fh.write(data)
    return 0, "%s %s: %d file(s) written to %s, each matching its pin.\n" % (
        pid, pin.get("version", ""), len(got), dest)


def _probe():
    if os.environ.get("GT_TEST_SANDBOX") and os.environ.get("GT_PREREQS_FAKE"):
        return FakeProbe(json.loads(os.environ["GT_PREREQS_FAKE"]))
    return RealProbe()


def _vault(explicit):
    if explicit:
        return explicit
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("vault_path")
    except (OSError, ValueError, AttributeError):
        return None


def _at_terminal():
    """a person at both ends (gt_tty.has_terminal: NUL passes isatty() on Windows)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gt_tty
    return gt_tty.has_terminal(sys.stdin) and gt_tty.has_terminal(sys.stdout)


def main(argv=None):
    ap = argparse.ArgumentParser(description="what an Obsidian-first machine needs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("check", "plan", "install", "plugin"):
        p = sub.add_parser(n)
        p.add_argument("--vault")
        p.add_argument("--os", dest="os_name", choices=("mac", "linux", "windows"))
        if n in ("install", "plugin"):
            p.add_argument("name")
        if n in ("install", "plugin"):
            p.add_argument("--yes", action="store_true")
        if n == "check":
            p.add_argument("--json", action="store_true")
            p.add_argument("--brief", action="store_true")
    a = ap.parse_args(argv)
    os_name = a.os_name or os_of()
    vault, probe = _vault(a.vault), _probe()
    if a.cmd == "check":
        rows = check(probe, vault, os_name)
        if a.json:
            print(json.dumps(rows, indent=1))
        elif a.brief:
            sys.stdout.write(brief_text(rows))
        else:
            for r in rows:
                print("%-16s %-9s %s" % (r["name"], r["state"], r["detail"]))
        return 0
    if a.cmd == "plan":
        sys.stdout.write(plan_text(check(probe, vault, os_name), os_name) or "Nothing is missing.\n")
        return 0
    yes = getattr(a, "yes", False)
    if a.cmd == "install" and not yes and _at_terminal():
        row = next((r for r in check(probe, vault, os_name) if r["name"] == a.name), None)
        c = COMMANDS[os_name].get(a.name)
        if row and row["state"] != "ok" and c and c["who"] == "gt":
            yes = input("Run `%s`? [y/N] " % c["shown"]).strip().lower() in ("y", "yes")
    if a.cmd == "plugin":
        pin = PLUGIN_PINS.get(a.name)
        if pin and not yes:
            if _at_terminal():
                yes = input("Download %s %s (%d files, each checked against its pinned sha256) into the vault? [y/N] "
                            % (a.name, pin.get("version", ""), len(pin.get("files") or {}))).strip().lower() in ("y", "yes")
            if not yes:
                sys.stdout.write("%s %s would be downloaded from its pinned address and checked against its sha256; "
                                 "nothing was fetched. Run again with --yes to do it.\n" % (a.name, pin.get("version", "")))
                return EXIT_NEEDS_ANSWER
        rc, text = install_plugin(a.name, vault)
    else:
        rc, text = install(a.name, probe, vault, os_name, yes=yes)
    sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
