#!/usr/bin/env python3
"""gt_uninstall.py -- remove Golden Thread from this machine, checked and then proven (0.20.1).

    gt_uninstall.py --check               list what WOULD be removed or edited; change nothing
    gt_uninstall.py [--yes]               remove it, print a summary, re-scan to prove it is gone
    options:  --keep-vault-config   leave ~/.claude/vault-config.json
              --discard-queued      remove ~/.gt-inbox even when it holds queued vault writes
              --purge-backups       also remove ~/.claude/golden-thread/backups
              --purge-lotr          also remove the LOTR gateway's home (~/.config/gt-lotr)
              --json                machine-readable result

Also reached as `install.sh --uninstall [--check]` and `install.cmd /uninstall [/check]`.

Exit codes: 0 done and the re-scan is clean (or --check ran); 1 a step FAILED, the re-scan still
finds something of gt's, or (--check) something could not be inspected; 2 refused -- not
confirmed, queued vault writes in ~/.gt-inbox, or no ~/.claude here.

WHY A SCRIPT (owner rule: any human step becomes a script with --check, self-proof and
rollback). Until 0.20.1 uninstalling was a paragraph in INSTALL.md that named the plugin dirs
and the registries and forgot the rest: the scheduled jobs (launchd, Task Scheduler, and on
Linux the plists 0.20.1 wrongly wrote), the sandbox entries in settings.json, the unlock daemon
and its home, ~/.gt-inbox and ~/.gt-scratch. Following it left jobs running gt files that no
longer existed and a settings.json that still denied the vault.

WHAT IT REMOVES, in this order (each step independent; one failing never stops the next):

  scheduled jobs      every job gt_schedule.py knows, under the current io.goldenthread.gt-<job>
                      and the legacy com.markethaven.gt-<job> labels (and the gt-probe label),
                      through gt_schedule.py `remove`, then any leftover itself: plists in
                      ~/Library/LaunchAgents, systemd --user units, crontab lines, Windows tasks
  sandbox entries     gt_sandbox.py `remove` -- its own record of what it added, so a user's own
                      settings survive -- BEFORE ~/.claude/golden-thread (where that record is)
  unlock              `gt_unlock.py daemon stop`; the unlock home goes with ~/.claude/golden-thread.
                      Enrolled factors, recovery codes and sealed secrets are DESTROYED.
  git credential      a global credential.*.helper that runs gt_unlock.py
  settings.json       hook entries whose command runs something in ~/.claude/golden-thread/hooks
                      (a user's own hooks are untouched; emptied blocks and events dropped),
                      every *@golden-thread-plugin key in enabledPlugins
  registries          *@golden-thread-plugin in installed_plugins.json, golden-thread-plugin in
                      known_marketplaces.json
  ~/.claude/CLAUDE.md the "## Golden Thread" section vault_init wrote -- only when it is still
                      exactly the text gt wrote; an edited one is kept and named
  plugin dirs         ~/.claude/plugins/cache/golden-thread-plugin, .../marketplaces/golden-thread-plugin
  gt's home           ~/.claude/golden-thread (hooks, bin and the python3 shim, interpreter.json,
                      jobs, logs, unlock, sandbox state) -- backups/ kept unless --purge-backups
  ~/bin/python3       the Windows shim, only when it carries the gt-python3-shim marker
  ~/.gt-inbox         refused while it holds queued vault writes, unless --discard-queued
  ~/.gt-scratch       the stage agents' scratch (Windows: %LOCALAPPDATA%\\gt-scratch)
  module cron lines   lines tagged "# gt-<module>" that run something under THIS home's ~/.claude
  IPC dirs            /tmp/gt-<uid>-* of this user with no live socket in them
  vault-config.json   last, unless --keep-vault-config

NEVER TOUCHED: the vault itself (its .githooks/ and git core.hooksPath stay; the summary says how
to unset them), the LOTR home unless --purge-lotr.

BACKUP AND ROLLBACK. Every file it edits or removes that holds configuration (settings.json,
installed_plugins.json, known_marketplaces.json, vault-config.json, ~/.claude/CLAUDE.md) is copied
first to ~/.claude/gt-uninstall-backup-<stamp>/ -- outside ~/.claude/golden-thread, which this
removes -- and the summary prints the copy-back command. Reinstalling is `install.sh`.
"""
import argparse
import ast
import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

MARKET = "golden-thread-plugin"
LABEL_PREFIXES = ("io.goldenthread.gt-", "com.markethaven.gt-")
KNOWN_JOBS = ("daily", "lint-weekly", "sweep", "reminder")
SHIM_MARK = "gt-python3-shim"
IS_WINDOWS = os.name == "nt"
OK, PROBLEM, REFUSED = 0, 1, 2
SANDBOX_MARKERS = ("GT_TEST_SANDBOX",)
# The scheduler and git verbs that CHANGE something; refused from a test/sandbox, exactly as
# gt_schedule refuses them: launchd's domain and the crontab are the user's whatever HOME says.
WRITE_VERBS = {"launchctl": {"bootout", "bootstrap", "remove", "unload"},
               "schtasks": {"/delete"},
               "systemctl": {"disable", "stop", "daemon-reload"},
               "crontab": {"-", "-r"},
               "git": {"--unset", "--unset-all"}}


def sandboxed():
    return any(os.environ.get(k) for k in SANDBOX_MARKERS)


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def run(args, env=None, input=None, timeout=120):
    """Every external call goes through here (tests replace it). Never raises."""
    args = [str(a) for a in args]
    if sandboxed() and any(v in WRITE_VERBS.get(os.path.basename(args[0]), ())
                           for v in [a.lower() for a in args[1:]]):
        return _R(1, "", "refused: %s from a test/sandbox" % args[0])
    try:
        p = subprocess.run(args, capture_output=True, text=True, env=env, input=input,
                           timeout=timeout)
        return _R(p.returncode, p.stdout or "", p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return _R(127, "", str(exc))


def real_home(home):
    """True only when `home` is this account's own home and no sandbox marker is set: launchd,
    systemd --user, Task Scheduler and the crontab belong to the account, not to HOME."""
    if sandboxed():
        return False
    try:
        if IS_WINDOWS:
            mine = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        else:
            import pwd
            mine = pwd.getpwuid(os.getuid()).pw_dir
        return os.path.realpath(mine) == os.path.realpath(str(home))
    except Exception:
        return False


# ---- one item = one thing that may be present ------------------------------------------------

class Item:
    """state: present | absent | unknown | kept; after a run: removed | failed."""

    def __init__(self, step, what, state, detail="", action=None, gate=None):
        self.step, self.what, self.state, self.detail = step, what, state, detail
        self.action, self.gate = action, gate

    def as_dict(self):
        return {"step": self.step, "what": self.what, "state": self.state, "detail": self.detail}


class Ctx:
    def __init__(self, home, a):
        self.home = Path(home)
        self.claude = self.home / ".claude"
        self.gt = self.claude / "golden-thread"
        self.hooks = self.gt / "hooks"
        self.a = a
        self.real = real_home(self.home)
        self.backup = None              # created on the first edit
        self.backed_up = []             # (original path, copy)
        self.failed_steps = set()
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        if IS_WINDOWS:
            self.env["USERPROFILE"] = str(self.home)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def p(self, *parts):
        return self.home.joinpath(*parts)

    def tool(self, name):
        """The installed copy of a gt script, else the one beside this file."""
        for d in (self.hooks, Path(__file__).resolve().parent):
            f = d / name
            if f.is_file():
                return f
        return None

    def _dest(self, name):
        if self.backup is None:
            self.backup = self.claude / ("gt-uninstall-backup-%s" % time.strftime("%Y%m%d_%H%M%S"))
            self.backup.mkdir(parents=True, exist_ok=True)
        dest, n = self.backup / name, 1
        while dest.exists():
            n += 1
            dest = self.backup / ("%s.%d" % (name, n))
        return dest

    def back_up(self, path):
        path = Path(path)
        if not path.is_file():
            return
        dest = self._dest(path.name)
        shutil.copy2(str(path), str(dest))
        self.backed_up.append((path, dest))

    def back_up_text(self, name, text):
        """A copy of something that is not a file (the crontab); restored with `crontab FILE`."""
        dest = self._dest(name)
        dest.write_bytes(text.encode("utf-8"))
        self.backed_up.append(("crontab", dest))


def _load_json(path):
    with open(str(path), encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path, data):
    path = Path(path)
    tmp = path.with_name(".%s.gt-uninstall.tmp" % path.name)
    with open(str(tmp), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(str(tmp), str(path))


def _rm(path):
    """Remove a file, a symlink (never its target) or a tree. Raises OSError."""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        def _retry(func, p, _exc):
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        shutil.rmtree(str(path), onerror=_retry)


def _why(exc):
    return getattr(exc, "strerror", None) or "%s: %s" % (exc.__class__.__name__, exc)


# ---- scheduled jobs ----------------------------------------------------------------------------

def job_names(ctx):
    """JOBS from gt_schedule.py, read (not imported: importing binds Path.home() at import)."""
    f = ctx.tool("gt_schedule.py")
    names = set(KNOWN_JOBS)
    try:
        tree = ast.parse(f.read_text(encoding="utf-8")) if f else None
        for node in (tree.body if tree else []):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "JOBS":
                names |= set(ast.literal_eval(node.value).keys())
    except (OSError, SyntaxError, ValueError, AttributeError):
        pass
    return sorted(names)


def _labelled(name):
    return any(name.startswith(p) for p in LABEL_PREFIXES)


def job_leftovers(ctx):
    """-> [(kind, label, path-or-None)] for every gt job artefact this home or account has."""
    out = []
    agents = ctx.p("Library", "LaunchAgents")
    # Every platform: 0.20.1 wrote launchd plists on Linux too (M11).
    for f in sorted(agents.glob("*.plist")) if agents.is_dir() else []:
        if _labelled(f.name):
            out.append(("plist", f.name[:-len(".plist")], f))
    units = unit_dir(ctx)
    for f in sorted(units.glob("*")) if units.is_dir() else []:
        if _labelled(f.name) and f.suffix in (".service", ".timer"):
            out.append(("unit", f.stem, f))
    for f in sorted(ctx.gt.joinpath("jobs").glob("gt-*.json")) if ctx.gt.joinpath("jobs").is_dir() else []:
        out.append(("taskfile", f.stem, f))
    if ctx.real and IS_WINDOWS:
        r = run(["schtasks", "/Query", "/FO", "CSV", "/NH"], env=ctx.env)
        for line in r.stdout.splitlines() if r.returncode == 0 else []:
            name = line.split(",")[0].strip().strip('"').lstrip("\\")
            if _labelled(name):
                out.append(("task", name, None))
    if ctx.real and not IS_WINDOWS:
        for line in _crontab(ctx)[1]:
            if any(p in line for p in LABEL_PREFIXES):
                out.append(("cron", line, None))
    return out


def unit_dir(ctx):
    """systemd --user units: $XDG_CONFIG_HOME only for the real account, else <home>/.config."""
    base = os.environ.get("XDG_CONFIG_HOME") if ctx.real else None
    return Path(base or ctx.p(".config")) / "systemd" / "user"


def _crontab(ctx):
    if shutil.which("crontab") is None:
        return False, []
    r = run(["crontab", "-l"], env=ctx.env, timeout=30)
    return r.returncode == 0, (r.stdout.splitlines() if r.returncode == 0 else [])


def _mine_cron(ctx, line):
    mine = (os.path.join(os.path.realpath(str(ctx.home)), ".claude") + os.sep,
            os.path.join(str(ctx.home), ".claude") + os.sep)
    return any(m in line for m in mine)


def scan_jobs(ctx):
    left = job_leftovers(ctx)
    if not left:
        return [Item("jobs", "scheduled jobs", "absent", "no gt job installed")]
    names = sorted({l for k, l, _p in left if k != "cron"})
    n_cron = sum(1 for k, _l, _p in left if k == "cron")
    desc = ", ".join(names + (["%d crontab line(s)" % n_cron] if n_cron else []))
    return [Item("jobs", "scheduled jobs", "present", desc, action=remove_jobs)]


def remove_jobs(ctx):
    sched = ctx.tool("gt_schedule.py")
    notes = []
    if sched is not None:
        for job in job_names(ctx):
            r = run([sys.executable, str(sched), "remove", job], env=ctx.env, timeout=120)
            line = (r.stdout + r.stderr).strip().splitlines()
            if line and "nothing to remove" not in line[-1]:
                notes.append(line[-1])
    # Whatever gt_schedule (this one, or an older release's) did not take: the legacy label,
    # Linux plists from 0.20.1, a job installed by hand. Removed here, by label prefix only.
    problems = []
    uid = os.getuid() if hasattr(os, "getuid") else 0
    for kind, label, path in job_leftovers(ctx):
        try:
            if kind == "plist":
                if ctx.real and sys.platform == "darwin":
                    run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], env=ctx.env)
                path.unlink()
            elif kind == "unit":
                if ctx.real and path.suffix == ".timer":
                    run(["systemctl", "--user", "disable", "--now", path.name], env=ctx.env)
                path.unlink()
            elif kind == "taskfile":
                if ctx.real and IS_WINDOWS:
                    data = {}
                    try:
                        data = _load_json(path)
                    except (OSError, ValueError):
                        pass
                    if data.get("Label"):
                        run(["schtasks", "/Delete", "/TN", data["Label"], "/F"], env=ctx.env)
                path.unlink()
                wrapper = path.with_suffix(".cmd")
                if wrapper.is_file():
                    wrapper.unlink()
            elif kind == "task":
                r = run(["schtasks", "/Delete", "/TN", label, "/F"], env=ctx.env)
                if r.returncode != 0:
                    problems.append("schtasks /Delete %s: %s" % (label, (r.stderr or r.stdout).strip()))
        except OSError as exc:
            problems.append("%s: %s" % (path, _why(exc)))
    if ctx.real and not IS_WINDOWS:
        ok, lines = _crontab(ctx)
        keep = [l for l in lines if not any(p in l for p in LABEL_PREFIXES)]
        if ok and len(keep) != len(lines):
            ctx.back_up_text("crontab", "\n".join(lines) + "\n")
            r = run(["crontab", "-"], env=ctx.env, input="\n".join(keep) + ("\n" if keep else ""))
            if r.returncode != 0:
                problems.append("crontab refused the update")
        if unit_dir(ctx).is_dir():
            run(["systemctl", "--user", "daemon-reload"], env=ctx.env)
    if problems:
        raise RuntimeError("; ".join(problems))
    return "; ".join(notes)


# ---- sandbox, unlock, git credential -------------------------------------------------------

def scan_sandbox(ctx):
    state = ctx.gt / "sandbox" / "state.json"
    if not state.is_file():
        return [Item("sandbox", "sandbox entries in settings.json", "absent")]
    return [Item("sandbox", "sandbox entries in settings.json",
                 "present", "recorded in %s" % state, action=remove_sandbox)]


def remove_sandbox(ctx):
    tool = ctx.tool("gt_sandbox.py")
    if tool is None:
        raise RuntimeError("gt_sandbox.py is not installed, so its recorded entries cannot be "
                           "taken out; ~/.claude/golden-thread is kept so the record survives")
    ctx.back_up(ctx.claude / "settings.json")
    r = run([sys.executable, str(tool), "remove"], env=ctx.env)
    if r.returncode != 0:
        raise RuntimeError("gt_sandbox.py remove exited %d: %s"
                           % (r.returncode, (r.stderr or r.stdout).strip()[-300:]))
    try:
        (ctx.gt / "sandbox" / "state.json").unlink()
    except OSError:
        pass
    return (r.stdout.strip().splitlines() or [""])[0]


def scan_unlock(ctx):
    home = ctx.gt / "unlock"
    if not home.is_dir():
        return [Item("unlock", "gt unlock (daemon and home)", "absent")]
    has = any(home.iterdir())
    return [Item("unlock", "gt unlock (daemon and home)", "present",
                 "%s%s" % (home, " -- enrolled factors, recovery codes and sealed secrets in it "
                                 "are DESTROYED" if has else ""),
                 action=stop_unlock, gate="unlock")]


def stop_unlock(ctx):
    tool = ctx.tool("gt_unlock.py")
    if tool is not None:
        run([sys.executable, str(tool), "daemon", "stop"], env=ctx.env, timeout=60)
    return "daemon stopped; its home goes with ~/.claude/golden-thread"


def _git_helpers(ctx):
    if shutil.which("git") is None:
        return []
    r = run(["git", "config", "--global", "--get-regexp", r"^credential\..*helper$"], env=ctx.env)
    return [l.split(None, 1)[0] for l in r.stdout.splitlines()
            if r.returncode == 0 and "gt_unlock.py" in l]


def scan_git(ctx):
    keys = _git_helpers(ctx)
    if not keys:
        return [Item("git-credential", "git credential helper running gt_unlock.py", "absent")]
    return [Item("git-credential", "git credential helper running gt_unlock.py", "present",
                 ", ".join(keys), action=remove_git)]


def remove_git(ctx):
    bad = []
    for k in _git_helpers(ctx):
        r = run(["git", "config", "--global", "--unset-all", k], env=ctx.env)
        if r.returncode != 0:
            bad.append(k)
    if bad:
        raise RuntimeError("git config --global --unset-all failed for %s" % ", ".join(bad))
    return ""


# ---- settings.json and the registries ---------------------------------------------------------

def _hook_prefixes(ctx):
    fwd = lambda s: s.replace("\\", "/")
    out = {fwd(str(ctx.hooks)) + "/", fwd(os.path.realpath(str(ctx.hooks))) + "/",
           "~/.claude/golden-thread/hooks/", "$HOME/.claude/golden-thread/hooks/",
           "${HOME}/.claude/golden-thread/hooks/"}
    return out


def gt_hook(ctx, command):
    c = (command or "").replace("\\", "/")
    return any(p in c for p in _hook_prefixes(ctx))


def settings_changes(ctx, d):
    """-> (hook entries removed, plugin keys removed) applied to `d` in place."""
    n_hooks, keys = 0, []
    hooks = d.get("hooks") if isinstance(d, dict) else None
    if isinstance(hooks, dict):
        for event in list(hooks):
            blocks = hooks[event]
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if isinstance(b, dict) and isinstance(b.get("hooks"), list):
                    keep = [h for h in b["hooks"]
                            if not (isinstance(h, dict) and gt_hook(ctx, h.get("command")))]
                    n_hooks += len(b["hooks"]) - len(keep)
                    b["hooks"] = keep
            blocks[:] = [b for b in blocks if not (isinstance(b, dict)
                                                   and isinstance(b.get("hooks"), list)
                                                   and not b["hooks"])]
            if not blocks:
                del hooks[event]
        if not hooks:
            del d["hooks"]
    ep = d.get("enabledPlugins") if isinstance(d, dict) else None
    if isinstance(ep, dict):
        for k in [k for k in ep if k.endswith("@" + MARKET)]:
            del ep[k]
            keys.append(k)
    return n_hooks, keys


def scan_settings(ctx):
    p = ctx.claude / "settings.json"
    if not p.is_file():
        return [Item("settings", "settings.json gt entries", "absent")]
    try:
        d = _load_json(p)
    except (OSError, ValueError) as exc:
        return [Item("settings", "settings.json gt entries", "unknown",
                     "unreadable (%s)" % _why(exc))]
    n, keys = settings_changes(ctx, d)
    if not n and not keys:
        return [Item("settings", "settings.json gt entries", "absent")]
    return [Item("settings", "settings.json gt entries", "present",
                 "%d hook entr%s into %s, %d enabledPlugins key(s)"
                 % (n, "y" if n == 1 else "ies", ctx.hooks, len(keys)), action=edit_settings)]


def edit_settings(ctx):
    p = ctx.claude / "settings.json"
    d = _load_json(p)
    settings_changes(ctx, d)
    ctx.back_up(p)
    _save_json(p, d)
    return ""


def _registry(ctx, path, what, keys_of):
    try:
        d = _load_json(path)
    except FileNotFoundError:
        return [Item("registry", what, "absent")]
    except (OSError, ValueError) as exc:
        return [Item("registry", what, "unknown", "unreadable (%s)" % _why(exc))]
    keys = keys_of(d)
    if not keys:
        return [Item("registry", what, "absent")]

    def act(ctx, path=path, keys_of=keys_of):
        d = _load_json(path)
        container = d.get("plugins") if "plugins" in d and isinstance(d.get("plugins"), dict) \
            and path.name == "installed_plugins.json" else d
        for k in keys_of(d):
            container.pop(k, None)
        ctx.back_up(path)
        _save_json(path, d)
        return ""
    return [Item("registry", what, "present", ", ".join(keys), action=act)]


def scan_registries(ctx):
    inst = ctx.claude / "plugins" / "installed_plugins.json"
    known = ctx.claude / "plugins" / "known_marketplaces.json"
    out = _registry(ctx, inst, "installed_plugins.json entries",
                    lambda d: sorted(k for k in ((d.get("plugins") if isinstance(d, dict) else None)
                                                 or {}) if k.endswith("@" + MARKET)))
    out += _registry(ctx, known, "known_marketplaces.json entry",
                     lambda d: [MARKET] if isinstance(d, dict) and MARKET in d else [])
    return out


def _claude_md_section(text):
    """-> (start, end) of the '## Golden Thread' section, or None."""
    m = re.search(r"(?m)^## Golden Thread[ \t]*\n", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^## ", text[m.end():])
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)


def _gt_section_text(vault):
    """The exact section vault_init writes (vault_init.py, cmd_fresh / cmd_create_project)."""
    return ("## Golden Thread\n\nTimestamps: Begin every response with the current date and time "
            "from the system context (injected as \"Current date and time: ...\" at the start of "
            "each prompt).\n\nScope rule: `global-memory/` contains only facts needed in ALL "
            "projects. Project-specific facts belong in `Projects/<slug>/memory/`, not here.\n\n"
            "Load the memory index only when explicitly asked, or when a `/gt:*` skill is "
            "invoked:\n`%s/global-memory/MEMORY.md`\n\nPlatform wiki:\n`%s/index.md` → follow "
            "links into `Knowledge/`\n" % (vault, vault))


def scan_claude_md(ctx):
    p = ctx.claude / "CLAUDE.md"
    what = "~/.claude/CLAUDE.md Golden Thread section"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return [Item("claude-md", what, "absent")]
    span = _claude_md_section(text)
    if span is None:
        return [Item("claude-md", what, "absent")]
    try:
        vault = _load_json(ctx.claude / "vault-config.json").get("vault_path") or ""
    except (OSError, ValueError, AttributeError):
        vault = ""
    body = text[span[0]:span[1]].strip()
    if not vault or body != _gt_section_text(vault).strip():
        return [Item("claude-md", what, "kept",
                     "it differs from the text gt wrote (edited, or another vault); remove it "
                     "by hand if you want it gone: %s" % p)]

    def act(ctx, p=p, span=span, text=text):
        ctx.back_up(p)
        new = (text[:span[0]].rstrip() + "\n\n" + text[span[1]:].lstrip()).strip()
        if new:
            p.write_bytes((new + "\n").encode("utf-8"))
        else:
            p.unlink()
        return ""
    return [Item("claude-md", what, "present", str(p), action=act)]


# ---- directories ------------------------------------------------------------------------------

def _dir_item(step, what, path, gate=None):
    path = Path(path)
    if not (path.exists() or path.is_symlink()):
        return Item(step, what, "absent", str(path))
    return Item(step, what, "present", str(path), action=lambda ctx, p=path: (_rm(p), "")[1],
                gate=gate)


def scan_dirs(ctx):
    out = [_dir_item("plugins", "plugin cache", ctx.claude / "plugins" / "cache" / MARKET),
           _dir_item("plugins", "plugin marketplace", ctx.claude / "plugins" / "marketplaces" / MARKET)]
    gt = ctx.gt
    keep_backups = not ctx.a.purge_backups and (gt / "backups").is_dir() \
        and any((gt / "backups").iterdir())
    if gt.is_dir() and not gt.is_symlink():
        rest = [e for e in gt.iterdir() if not (keep_backups and e.name == "backups")]
        if rest:
            def act(ctx, gt=gt, rest=rest):
                for e in rest:
                    _rm(e)
                if not any(gt.iterdir()):
                    gt.rmdir()
                return ""
            out.append(Item("gt-home", "~/.claude/golden-thread", "present",
                            "%s (%d entr%s%s)" % (gt, len(rest), "y" if len(rest) == 1 else "ies",
                                                  "; backups/ kept" if keep_backups else ""),
                            action=act, gate="gt-home"))
        else:
            out.append(Item("gt-home", "~/.claude/golden-thread", "absent"))
        if keep_backups:
            out.append(Item("gt-home", "~/.claude/golden-thread/backups", "kept",
                            "%s (remove with --purge-backups)" % (gt / "backups")))
    else:
        out.append(_dir_item("gt-home", "~/.claude/golden-thread", gt))
    shim = ctx.p("bin", "python3")
    if shim.is_file() or shim.is_symlink():
        try:
            ours = SHIM_MARK in shim.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = False
        out.append(_dir_item("shim", "~/bin/python3 shim", shim) if ours else
                   Item("shim", "~/bin/python3", "kept", "not gt's (no %s marker)" % SHIM_MARK))
    else:
        out.append(Item("shim", "~/bin/python3 shim", "absent"))
    inbox = ctx.p(".gt-inbox")
    item = _dir_item("inbox", "~/.gt-inbox", inbox)
    queued = sorted((inbox / "queue").glob("*.json")) if (inbox / "queue").is_dir() else []
    if queued:
        item.detail += " -- %d queued vault write(s) not yet applied" % len(queued)
        item.gate = "queued"
    out.append(item)
    if IS_WINDOWS and not os.environ.get("GT_SCRATCH_ROOT"):
        scratch = Path(os.environ.get("LOCALAPPDATA") or ctx.p("AppData", "Local")) / "gt-scratch"
    elif os.environ.get("GT_SCRATCH_ROOT"):
        scratch = Path(os.environ["GT_SCRATCH_ROOT"]).expanduser()
    else:
        scratch = ctx.p(".gt-scratch")
    out.append(_dir_item("scratch", "stage agents' scratch", scratch))
    lotr = Path(os.environ.get("LOTR_HOME") or ctx.p(".config", "gt-lotr"))
    if lotr.exists():
        out.append(_dir_item("lotr", "LOTR gateway home", lotr) if ctx.a.purge_lotr else
                   Item("lotr", "LOTR gateway home", "kept",
                        "%s (its connections and enrolment; remove with --purge-lotr)" % lotr))
    return out


def scan_cron(ctx):
    """Module crontab lines: tagged '# gt-<module>', running something under this home's ~/.claude."""
    if IS_WINDOWS or not ctx.real:
        return []
    ok, lines = _crontab(ctx)
    mine = [l for l in lines if re.search(r"#\s*gt-[a-z0-9-]+\s*$", l) and _mine_cron(ctx, l)]
    if not mine:
        return [Item("cron", "module crontab lines", "absent")]

    def act(ctx):
        ok, lines = _crontab(ctx)
        keep = [l for l in lines if l not in mine]
        ctx.back_up_text("crontab", "\n".join(lines) + "\n")
        r = run(["crontab", "-"], env=ctx.env, input="\n".join(keep) + ("\n" if keep else ""))
        if r.returncode != 0:
            raise RuntimeError("crontab refused the update")
        return ""
    return [Item("cron", "module crontab lines", "present", "%d line(s)" % len(mine), action=act)]


def _socket_live(path):
    if not hasattr(socket, "AF_UNIX"):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(str(path))
        return True
    except socket.timeout:
        return True          # a busy server (full backlog) is still a server
    except OSError as exc:
        # Only "nobody is listening" is dead; anything else (EAGAIN on a full backlog) is not.
        return exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK, errno.EPROTOTYPE)
    finally:
        s.close()


def ipc_dirs(ctx):
    if IS_WINDOWS or not hasattr(os, "getuid"):
        return []
    # From a test/sandbox -- or for a HOME that is not this account's own -- only an explicitly
    # named root is ever swept: /tmp is the machine's, shared by every home.
    if not os.environ.get("GT_UNINSTALL_IPC_ROOT") and (sandboxed() or not real_home(ctx.home)):
        return []
    root = Path(os.environ.get("GT_UNINSTALL_IPC_ROOT") or "/tmp")
    out = []
    for d in sorted(root.glob("gt-%d-*" % os.getuid())) if root.is_dir() else []:
        try:
            st = os.lstat(str(d))
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            continue
        if any(_socket_live(s) for s in d.glob("*.sock")):
            continue
        out.append(d)
    return out


def scan_ipc(ctx):
    dirs = ipc_dirs(ctx)
    if not dirs:
        return [Item("ipc", "stale gt IPC dirs", "absent")]

    def act(ctx, dirs=dirs):
        for d in dirs:
            _rm(d)
        return ""
    return [Item("ipc", "stale gt IPC dirs", "present",
                 "%d under %s" % (len(dirs), dirs[0].parent), action=act)]


def scan_config(ctx):
    p = ctx.claude / "vault-config.json"
    if not p.is_file():
        return [Item("config", "vault-config.json", "absent")]
    if ctx.a.keep_vault_config:
        return [Item("config", "vault-config.json", "kept", "%s (--keep-vault-config)" % p)]

    def act(ctx, p=p):
        ctx.back_up(p)
        p.unlink()
        return ""
    return [Item("config", "vault-config.json", "present", str(p), action=act)]


SCANS = (scan_jobs, scan_sandbox, scan_unlock, scan_git, scan_settings, scan_registries,
         scan_claude_md, scan_dirs, scan_cron, scan_ipc, scan_config)


def scan(ctx):
    items = []
    for fn in SCANS:
        try:
            items += fn(ctx)
        except Exception as exc:                      # one scan never stops the others
            items.append(Item(fn.__name__[5:], fn.__name__[5:], "unknown",
                              "could not inspect (%s)" % _why(exc)))
    return items


def vault_note(ctx):
    try:
        v = _load_json(ctx.claude / "vault-config.json").get("vault_path")
    except (OSError, ValueError, AttributeError):
        v = None
    if not v:
        return "The vault (if any) is never touched."
    return ("The vault %s is never touched. Its .githooks/ and git core.hooksPath stay; to "
            "unhook it: git -C \"%s\" config --unset core.hooksPath" % (v, v))


# ---- the run ---------------------------------------------------------------------------------

def confirm(ctx, items):
    if ctx.a.yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("gt uninstall: not confirmed. Re-run with --yes (nothing was changed). See "
              "--check for what would be removed.")
        return False
    destroys = any(i.gate == "unlock" and "DESTROYED" in i.detail for i in items)
    word = "yes" if destroys else "y"
    try:
        ans = input("Remove all of the above%s? Type %s to continue: "
                    % (" -- INCLUDING your unlock factors and sealed secrets" if destroys else "",
                       word)).strip().lower()
    except EOFError:
        ans = ""
    if ans in (word, "yes"):
        return True
    print("gt uninstall: nothing changed.")
    return False


def render(items, header, as_json, extra=None):
    if as_json:
        return
    print(header)
    width = max([len(i.what) for i in items] + [10])
    for i in items:
        print("  %-12s %-*s %s" % (i.state.upper() if i.state in ("failed",) else i.state,
                                    width, i.what, i.detail))
    for line in extra or []:
        print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_uninstall.py",
                                 description="Remove Golden Thread from this machine "
                                             "(--check first to see what would go).")
    ap.add_argument("--check", "--dry-run", dest="check", action="store_true",
                    help="list what would be removed or edited; change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask (required without a TTY)")
    ap.add_argument("--keep-vault-config", action="store_true")
    ap.add_argument("--discard-queued", action="store_true",
                    help="remove ~/.gt-inbox even when it holds queued vault writes")
    ap.add_argument("--purge-backups", action="store_true",
                    help="also remove ~/.claude/golden-thread/backups")
    ap.add_argument("--purge-lotr", action="store_true",
                    help="also remove the LOTR gateway home")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--home", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    home = Path(a.home or os.environ.get("HOME") or os.path.expanduser("~")).expanduser()
    if IS_WINDOWS and not a.home:
        home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    if not (home / ".claude").is_dir():
        print("gt uninstall: refused -- %s has no .claude directory, so this is not a home gt "
              "was installed into (HOME=%s)." % (home, home), file=sys.stderr)
        return REFUSED
    ctx = Ctx(home, a)
    items = scan(ctx)
    notes = [vault_note(ctx)]
    queued = [i for i in items if i.gate == "queued" and i.state == "present"]
    blockers = []
    if queued and not a.discard_queued:
        blockers.append("~/.gt-inbox holds vault writes that were never applied. Apply them "
                        "first: python3 \"%s\" drain --vault <vault>   -- or pass "
                        "--discard-queued to throw them away."
                        % (ctx.tool("gt_broker.py") or ctx.hooks / "gt_broker.py"))

    if a.check:
        if a.json:
            print(json.dumps({"mode": "check", "home": str(home), "blockers": blockers,
                              "items": [i.as_dict() for i in items]}, indent=2))
        else:
            render([i for i in items if i.state != "absent"] or items,
                   "gt uninstall --check: nothing changed (HOME %s). Would remove:" % home, False,
                   ["  " + n for n in notes] + ["  BLOCKER: " + b for b in blockers])
        return PROBLEM if any(i.state == "unknown" for i in items) else OK

    if blockers:
        for b in blockers:
            print("gt uninstall: refused -- " + b, file=sys.stderr)
        return REFUSED
    todo = [i for i in items if i.state == "present"]
    if not todo:
        render(items, "gt uninstall: nothing of gt's is installed here (HOME %s)." % home, a.json,
               ["  " + n for n in notes])
        if a.json:
            print(json.dumps({"mode": "run", "clean": True, "items": [i.as_dict() for i in items]},
                             indent=2))
        return OK
    if not a.json:
        render(todo, "gt uninstall will remove (HOME %s):" % home, False)
    if not confirm(ctx, todo):
        return REFUSED

    for i in todo:
        # ~/.claude/golden-thread holds the sandbox record: if taking the entries out failed,
        # keep it so a later `gt_sandbox.py remove` (or this, re-run) can still do it.
        if i.gate == "gt-home" and "sandbox" in ctx.failed_steps:
            i.state, i.detail = "failed", "kept: the sandbox step failed and its record lives here"
            continue
        try:
            note = i.action(ctx)
            i.state = "removed"
            if note:
                i.detail = (i.detail + " -- " if i.detail else "") + note
        except Exception as exc:
            i.state = "failed"
            i.detail = (i.detail + " -- " if i.detail else "") + _why(exc)
            ctx.failed_steps.add(i.step)

    after = scan(Ctx(home, a))
    left = [x for x in after if x.state in ("present", "unknown")]
    clean = not left and not ctx.failed_steps
    extra = ["  " + n for n in notes]
    if ctx.backup:
        extra.append("  Backups of every file edited: %s" % ctx.backup)
        extra.append("  Roll back those edits with:")
        for orig, copy in ctx.backed_up:
            extra.append("    crontab \"%s\"" % copy if orig == "crontab"
                         else "    cp -p \"%s\" \"%s\"" % (copy, orig))
        extra.append("  Reinstall with install.sh (or install.cmd on Windows).")
    if left:
        extra.append("  RE-SCAN: still present -- " + "; ".join(
            "%s (%s)" % (x.what, x.detail) for x in left))
    else:
        extra.append("  Re-scan: nothing of gt's remains%s."
                     % ("" if not ctx.failed_steps else " except what FAILED above"))
    if a.json:
        print(json.dumps({"mode": "run", "home": str(home), "clean": clean,
                          "backup": str(ctx.backup) if ctx.backup else None,
                          "items": [i.as_dict() for i in items],
                          "remaining": [x.as_dict() for x in left]}, indent=2))
    else:
        render(items, "\ngt uninstall: %s" % ("done, re-scan clean." if clean
                                               else "INCOMPLETE -- see FAILED / RE-SCAN below."),
               False, extra)
        print("Restart Claude Code (or quit it) so it stops loading the removed plugins.")
    return OK if clean else PROBLEM


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ngt uninstall: interrupted", file=sys.stderr)
        sys.exit(REFUSED)
