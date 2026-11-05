#!/usr/bin/env python3
"""gt_testing_profile -- a set-and-forget testing profile: stop pressing enter during a test run (0.21.0).

    gt_testing_profile.py on  [--scope project|local|user] [--project-dir DIR] [--runners "a,b,..."]
                              [--sandbox DIR] [--until 4h] [--dry-run]
    gt_testing_profile.py off [--scope project|local|user] [--project-dir DIR]
    gt_testing_profile.py status
    gt_testing_profile.py sweep [--hook]      end every profile whose --until has passed

WHY. An unattended test run stops every few minutes for a yes on the same harmless commands:
pytest, node --test, npm audit, and gt's own tests/run.sh and dev/remote-test.sh (owner,
2026-10-09: "I want testing to run without having to sit there the whole time"). Those prompts
are a convenience gate, not a security control. The Core-rule gates ARE the security model, and
this profile never touches them: the vault write guard, the explicit-vault-target check and the
secrets Stop hook run exactly as before, and nothing here is a hook, a mode or an environment
variable.

WHAT IT WRITES. Claude Code allow rules (permissions.allow) for a reviewed set of commands, and
Edit/Read rules for ONE folder you name with --sandbox. Nothing else:

    default runners  pytest, python3 -m pytest/unittest, npm test, node --test, vitest run, bats,
                     npm audit, pip-audit, semgrep, osv-scanner, and gt's own dev commands
                     (bash dev/remote-test.sh, bash tests/run.sh, GT_TEST_VERSION=* bash
                     tests/run.sh, the same from inside tests/, python3 tests/prun.py)
    --runners        replaces that set with your own comma list of command prefixes
    --sandbox DIR    Edit and Read rules for DIR only; without it no write rule is added

Never written, and refused by name: a bare tool (Bash, Edit), ssh/scp/rsync, rm, git push, curl,
wget, sudo, a shell or interpreter with no script to scope it, a reader of arbitrary files (cat,
tail, grep), --dangerously-skip-permissions, defaultMode bypassPermissions, GT_TEST_GATE, any
hook change, and a sandbox that is the vault, inside it, containing it, your home folder, ~/.claude
or any other credential, login or start-up folder (gt_io_policy's write never-list, the same one
the disk broker uses). No read-only log commands are added: `tail`/`grep` rules cannot be scoped
to a folder, so each would also read a credential file.

HOW IT MERGES (gt_lockdown's pattern; settings.json cannot hold comments, so no BEGIN/END
markers). Only rules the file did not already hold become gt's; they are recorded in
~/.claude/golden-thread/testing/state.json, one record per settings file. The settings file is
copied to ~/.claude/golden-thread/backups/settings.json.<stamp>.testing before any change.
`off` removes exactly the recorded rules and nothing hand-edited; when the file is still exactly
as `on` left it, the original bytes are put back (a file that did not exist is deleted), so
on then off is byte-identical.

SCOPE. The project's .claude/settings.json is the default. `--scope local` writes the project's
.claude/settings.local.json instead -- the personal, uncommitted file (Claude Code reads it from the
git root) -- which is where `gt_testing_doctor.py fix ...` puts every change it makes. `--scope user`
writes ~/.claude/settings.json, which reaches every project on this machine -- broader, and said so.
`off` with no --scope ends this project's records in BOTH project files at once, and prints the diff.
`--until 4h` (minutes or hours, at most 7 days) ends the profile by itself: the session-start
sweep (gt_grant.py sweep), gt_settings.py show and `status` remove an expired one and say so.

THE DOCTOR'S FIXES (0.21.4). `gt_doctor.py testing` names the gate that blocked a run, and
`gt_testing_doctor.py fix ...` then calls apply_fix() here, so this file stays the one owner of
"what gt loosened". A fix writes ONE thing into the project's settings.local.json and records it in
the same state record, after the same backup: a folder in permissions.additionalDirectories
(add-dir), one runner rule in permissions.allow (allow), or permissions.defaultMode acceptEdits
with the value it replaced (accept-edits; `on` itself still never writes a mode). `off` and the
--until sweep take those out too, put defaultMode back, and print the diff. A fix refuses a folder
or project that is, is inside or contains the vault, and the credential, login and start-up
folders --sandbox refuses; it never touches blockReadsOutsideWorkingDirectories, a hook, or any
Core-rule gate.

WHO. Like a grant, turning it on is yours to do: it needs a terminal (an agent's shell has none)
and a fresh gt unlock confirmation when unlock is on. `off`, `status` and `--dry-run` need
neither: they only tighten or read. `--dry-run` prints the exact diff and writes nothing.
Restart Claude Code (or let it reload settings) after a change.

Stdlib only. Exit: 0 ok, 1 refused, 2 usage.
"""
import base64
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_tty  # noqa: E402
import gt_sandbox as sb  # noqa: E402  (settings paths, JSON helpers, atomic write)

STATE_SCHEMA = 1
SCOPES = ("project", "local", "user")
FIX_KINDS = ("add-dir", "allow", "accept-edits")

# --------------------------------------------------------------------- what is allowed
# Command prefixes. A prefix becomes Bash(<prefix>:*), Claude Code's prefix rule.
STATIC_RUNNERS = (
    "pytest", "python3 -m pytest", "python3 -m unittest", "npm test", "node --test",
    "vitest run", "bats", "npm audit", "pip-audit", "semgrep", "osv-scanner",
)
# gt's own dev test commands (dev/remote-test.sh, tests/run.sh, tests/prun.py): where the owner
# presses enter today. `GT_TEST_VERSION=*` is how a release's tests are selected; the `run.sh`
# forms are the same command run from inside tests/.
GT_RUNNERS = (
    "bash dev/remote-test.sh", "bash tests/run.sh", "GT_TEST_VERSION=* bash tests/run.sh",
    "bash run.sh", "GT_TEST_VERSION=* bash run.sh", "python3 tests/prun.py",
)
DEFAULT_RUNNERS = STATIC_RUNNERS + GT_RUNNERS

# Environment variables a runner prefix may set. Nothing that loads code (LD_PRELOAD, PYTHONPATH,
# NODE_OPTIONS) and, by name, never the test gate.
ENV_OK = ("GT_TEST_VERSION", "GT_TEST_JOBS", "GT_REMOTE_TEST_HOST", "PYTHONDONTWRITEBYTECODE",
          "CI", "NODE_ENV", "PYTHONUTF8")

# First words that are never a runner, each with the reason the refusal gives.
NEVER_CMD = {
    "ssh": "it reaches other machines", "scp": "it copies to other machines",
    "sftp": "it reaches other machines", "rsync": "it copies to other machines",
    "rm": "it deletes", "rmdir": "it deletes", "mv": "it moves files", "cp": "it copies files",
    "dd": "it overwrites devices", "ln": "it makes links", "tee": "it writes files",
    "curl": "it fetches and sends over the network", "wget": "it fetches over the network",
    "nc": "it opens network connections", "ncat": "it opens network connections",
    "sudo": "it runs as root", "su": "it runs as another user", "doas": "it runs as root",
    "chmod": "it changes permissions", "chown": "it changes owners",
    "kill": "it stops processes", "pkill": "it stops processes by pattern",
    "killall": "it stops processes by pattern", "eval": "it runs any text", "exec": "it runs any text",
    "xargs": "it runs any command", "find": "it can delete and run commands",
    "env": "it runs any command", "open": "it opens anything", "osascript": "it scripts the Mac",
    "cat": "it reads any file, credentials included", "head": "it reads any file, credentials included",
    "tail": "it reads any file, credentials included", "less": "it reads any file",
    "more": "it reads any file", "grep": "it reads any file, credentials included",
    "rg": "it reads any file, credentials included", "sed": "it reads and rewrites any file",
    "awk": "it runs any program", "strings": "it reads any file", "base64": "it reads any file",
    "printenv": "it prints the environment, secrets included", "set": "it prints the environment",
    "export": "it changes the environment", "source": "it runs any file", ".": "it runs any file",
    "npx": "it downloads and runs a package", "pip": "it installs packages",
    "pip3": "it installs packages", "brew": "it installs software",
}
# git: only reading subcommands are ever a runner prefix.
GIT_READ = ("status", "log", "diff", "show")
SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "fish")
INTERPRETERS = ("python", "python3", "node", "perl", "ruby", "deno", "bun", "php", "pwsh",
                "powershell", "cmd", "lua", "Rscript")
PY_MODULES = ("pytest", "unittest", "coverage", "nose2")
SCRIPT_EXT = (".sh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
# anywhere in a runner: credential, login, hook, settings and gate names
FORBIDDEN_TEXT = ("gt_test_gate", "skip-permissions", "bypasspermissions", "dangerously",
                  "defaultmode", "disableallhooks", "golden-thread", "settings.json",
                  "settings.local", "id_rsa", "id_ed25519", ".credentials", ".npmrc", ".pypirc",
                  ".git-credentials", "authorized_keys", "vault-config")
FORBIDDEN_DIRS = (".ssh", ".aws", ".gnupg", ".claude", ".config", ".git")
_META = re.compile(r"[;&|<>$`(){}\\\n\r\"']")
_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class ProfileError(Exception):
    """A refusal: the message names the rule. Exit 1."""


class UsageError(Exception):
    """Exit 2."""


# --------------------------------------------------------------------- helpers
def _now(now=None):
    if now is not None:
        return float(now)
    env = os.environ.get("GT_GRANT_NOW")           # gt_grant's own injectable clock
    try:
        return float(env) if env else time.time()
    except ValueError:
        return time.time()


def _dir(h=None):
    return os.path.join(sb.gt_home(h), "testing")


def state_path(h=None):
    return os.path.join(_dir(h), "state.json")


def log_path(h=None):
    return os.path.join(_dir(h), "log.jsonl")


def load_state(h=None):
    st = sb._read_json(state_path(h), None)
    if not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA \
            or not isinstance(st.get("profiles"), dict):
        return {"schema": STATE_SCHEMA, "profiles": {}}
    return st


def _save_state(st, h=None):
    if not st["profiles"]:
        try:
            os.unlink(state_path(h))
        except OSError:
            pass
        return
    os.makedirs(_dir(h), exist_ok=True)
    sb._atomic_write(state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")


def _log(entry, h=None):
    os.makedirs(_dir(h), exist_ok=True)
    entry = dict(entry, ts=time.strftime("%Y-%m-%d %H:%M:%S %z"))
    with open(log_path(h), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _backup(path, h=None):
    if not os.path.exists(path):
        return None
    d = os.path.join(sb.gt_home(h), "backups")
    os.makedirs(d, exist_ok=True)
    bak = os.path.join(d, "settings.json.%s.testing" % time.strftime("%Y%m%d_%H%M%S"))
    if not os.path.exists(bak):
        shutil.copyfile(path, bak)
    return bak


def project_root(project_dir=None):
    """The folder a project-scoped change is about: --project-dir, else Claude Code's own
    $CLAUDE_PROJECT_DIR, else the working directory."""
    base = project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.abspath(os.path.expanduser(base))


def settings_for(scope, project_dir=None, h=None):
    if scope == "user":
        return sb.settings_path(h)
    base = project_root(project_dir)
    if scope == "local":
        # Claude Code (2.1.211 and later) reads settings.local.json from the git repository root,
        # and from the working folder outside a repository: write where it will be read.
        return os.path.join(sb._git_root(base) or base, ".claude", "settings.local.json")
    return os.path.join(base, ".claude", "settings.json")


def _load_doc(path):
    raw = _read_bytes(path)
    if raw is None:
        return {}, None
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise ProfileError("refused: %s is not valid JSON (%s); fix it first -- gt never "
                           "overwrites a file it cannot read" % (path, e))
    if not isinstance(d, dict):
        raise ProfileError("refused: %s is not a JSON object" % path)
    return d, raw


# --------------------------------------------------------------------- the runner checks
def check_runner(text, h=None):
    """-> the normalised command prefix, or raises ProfileError naming the rule it broke."""
    t = (text or "").strip()

    def no(why):
        raise ProfileError("refused: runner %r: %s" % (t, why))
    if not t:
        raise ProfileError("refused: an empty runner")
    low = t.lower()
    for bad in FORBIDDEN_TEXT:
        if bad in low:
            no("it names %s, which this profile never touches (gates, hooks, settings and "
               "credentials are outside it)" % bad)
    vault = sb.vault_path(h)
    if vault and (vault in t or os.path.realpath(vault) in t):
        no("it names the vault, which a test profile never reaches")
    if t.startswith("-"):
        no("it is an option, not a command")
    if _META.search(t):
        no("shell syntax (; & | < > $ ` ( ) quotes) would chain a second command behind the "
           "approved one")
    try:
        toks = shlex.split(t)
    except ValueError as e:
        no("it does not parse (%s)" % e)
    i = 0
    while i < len(toks) and _ENV.match(toks[i]):
        name = _ENV.match(toks[i]).group(1)
        if name not in ENV_OK:
            no("the variable %s may not be set by a runner (allowed: %s)" % (name, ", ".join(ENV_OK)))
        i += 1
    cmd = toks[i:]
    if not cmd:
        no("only variables, no command")
    first = cmd[0]
    for tok in cmd:
        if "*" in tok or tok.startswith(("/", "~")) or ".." in tok.split("/"):
            no("%r is an absolute, home or parent path or a wildcard; name the command relative to "
               "the project" % tok)
        parts = [x.lower() for x in tok.replace("\\", "/").split("/")]
        base = parts[-1]
        if any(p in FORBIDDEN_DIRS for p in parts) or base == ".env" or base.startswith(".env."):
            no("%r reaches a credential, login or hook location" % tok)
    if first in NEVER_CMD:
        no("%s is never a runner: %s" % (first, NEVER_CMD[first]))
    if first == "git":
        if len(cmd) < 2 or cmd[1] not in GIT_READ:
            no("git is only allowed as one of: %s (never push, config, reset or clean)"
               % ", ".join("git " + g for g in GIT_READ))
    elif first in SHELLS:
        if len(cmd) < 2 or cmd[1].startswith("-") or not cmd[1].endswith(SCRIPT_EXT):
            no("%s with no script file runs any text; name the script (bash tests/run.sh)" % first)
    elif first in INTERPRETERS:
        ok = (len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] in PY_MODULES) \
            or (first == "node" and len(cmd) >= 2 and cmd[1] == "--test") \
            or (len(cmd) >= 2 and not cmd[1].startswith("-") and cmd[1].endswith(SCRIPT_EXT))
        if not ok:
            no("%s with no script file runs any code; name a test module (-m pytest, --test) or "
               "a script" % first)
    elif first == "npm":
        if len(cmd) < 2 or cmd[1] not in ("test", "audit"):
            no("npm is only allowed as npm test or npm audit (npm run, install and exec run "
               "package scripts)")
    return " ".join(toks)


def runner_rule(prefix):
    return "Bash(%s:*)" % prefix


def parse_runners(spec, h=None):
    """A comma list -> the checked prefixes; None -> the default set (each default is checked too)."""
    items = list(DEFAULT_RUNNERS) if spec is None else [x for x in spec.split(",") if x.strip()]
    if not items:
        raise UsageError("--runners needs at least one command")
    out = []
    for x in items:
        p = check_runner(x, h)
        if p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------- the sandbox check
def check_sandbox(path, h=None, flag="--sandbox"):
    """-> the real path of the one folder writes are allowed in, or raises ProfileError.
    `flag` only words the refusal: the doctor's add-dir judges its folder by the same rules."""
    import gt_io_policy as iop
    raw = (path or "").strip()
    if not raw:
        raise ProfileError("refused: %s needs a folder" % flag)
    p = os.path.abspath(os.path.expanduser(raw))
    real = os.path.realpath(p)

    def no(rule, why):
        raise ProfileError("refused (%s): %s %s %s" % (rule, flag, raw, why))
    if os.path.dirname(real) == real:
        no("root", "is the whole file system, not a sandbox")
    home = os.path.realpath(sb.home(h))
    if real == home:
        no("home", "is your home folder itself; name a folder inside it")
    vault = iop.vault_root(h)
    if vault and (iop._inside(real, vault) or iop._inside(vault, real)):
        no("vault", "is, or contains, the vault (vault writes keep the write queue)")
    if iop._inside(home, real):
        no("home", "contains your home folder")
    nv = iop._write_never(real, h) or iop._write_never(os.path.join(real, "x"), h)
    if nv:
        no(nv[0], "is not writable by design: %s" % nv[1])
    # a never-folder that lives INSIDE the sandbox: ~/.config would reach ~/.config/gh
    for pre in iop.HOME_NEVER:
        if iop._inside(os.path.join(home, *pre), real):
            no("credential", "contains ~/%s, which is never written" % "/".join(pre))
    if iop.never_reason(real, h):
        no("credential", "is a credential location (%s)" % iop.never_reason(real, h))
    return real


def sandbox_rules(real):
    return [_rule_path(real, "Edit"), _rule_path(real, "Read")]


def _rule_path(real, tool):
    return "%s(%s)" % (tool, sb.rule_path(real, True))


# --------------------------------------------------------------------- on
def parse_until(text):
    import gt_grant
    try:
        kind, secs = gt_grant.parse_duration(text)
    except ValueError as e:
        raise UsageError(str(e))
    if kind != "timed":
        raise UsageError("--until %r: use <N>m or <N>h (at most 7 days); leave it out for no "
                         "expiry" % text)
    return secs


def build_rules(runners, sandbox_real, h=None):
    """-> the rules to add, each judged by gt_lockdown's own rule checks."""
    import gt_lockdown as ld
    rules = [runner_rule(r) for r in runners]
    if sandbox_real:
        rules += sandbox_rules(sandbox_real)
    for r in rules:
        why = ld._judge_rule(r, "allow", "insecure") or (
            "%s would allow a consent-tier op" % r if ld._is_consent_allow(r) else None)
        if why:
            raise ProfileError("refused: " + why)
    return rules


def _require_terminal(rerun=None, has_terminal=None):
    """A change to your settings is yours to confirm: refuse with no person at a terminal
    (gt_tty.has_terminal: on Windows NUL is not one). No environment variable stands in for a
    person (phase 108): a test passes `has_terminal` or uses a pseudo-terminal."""
    if not (has_terminal or gt_tty.has_terminal)(sys.stdin):
        raise ProfileError("refused: a testing profile is yours to turn on, and there is no "
                           "terminal here. Run this in your own terminal%s."
                           % ((": " + rerun) if rerun else " (or gt_settings.py testing on ...)"))


def _unlock_confirm(reason):
    import gt_grant
    refused = gt_grant._confirm(reason)
    if refused is not None:
        raise ProfileError("refused: turning it on needs a fresh confirmation (gt unlock exit "
                           "%s); nothing changed." % refused)


def _confirm_terminal(reason):
    _require_terminal()
    _unlock_confirm(reason)


def _ask_yes(question, assume_yes=False):
    """The one explicit yes a fix needs. A person at the terminal types it (or passed --yes)."""
    if assume_yes:
        return True
    sys.stdout.write("%s [y/N] " % question)
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        line = ""
    return line.strip().lower() in ("y", "yes")


def _dump(d):
    return json.dumps(d, indent=2) + "\n"


# A settings file can hold secrets (an `env` block, an apiKeyHelper, a header). A diff must never
# be the way one reaches a terminal -- or a session that ran the command -- so every diff gt
# prints is taken between two NORMALISED VIEWS of the file in which the value under such a key
# is hidden, with no context lines: only the entries gt added or removed are shown.
_SECRETISH = re.compile(r"(?i)(key|token|secret|passw|auth|credential|cookie|bearer|signature)")


def _masked(node, key=None):
    if isinstance(node, dict):
        if key in ("env", "headers"):
            return {k: "<hidden>" for k in node}
        return {k: _masked(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_masked(v, key) for v in node]
    if isinstance(node, str) and key and _SECRETISH.search(key):
        return "<hidden>"
    return node


def _view(raw):
    """The masked, normalised text of a settings file's bytes ('' when it does not exist)."""
    if raw is None:
        return ""
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "(not valid JSON; not shown)\n"
    return _dump(_masked(d)) if isinstance(d, dict) else "(not a JSON object; not shown)\n"


def _diff(path, old_text, new_text, old_name=None, new_name=None):
    return "".join(difflib.unified_diff(
        old_text.splitlines(True), new_text.splitlines(True),
        fromfile=old_name or (path if old_text else "/dev/null (the file does not exist yet)"),
        tofile=new_name or path, n=0))


def _bytes_diff(path, before, after):
    """The change between two states of a settings file (bytes or None), secrets hidden."""
    return _diff(path, _view(before), _view(after),
                 old_name=path if before is not None else "/dev/null (the file did not exist)",
                 new_name=path if after is not None else "/dev/null (the file is gone)")


def _commit(scope, path, raw, rec, new_text, changed, fields, st, h=None):
    """Write `new_text` to `path` and record the change in the profile record for that file.
    -> the backup path, or None. `fields` are the record fields this change sets; everything the
    record already holds (rules, folders, a mode, the exact-restore bytes) is kept."""
    if changed:
        bak = _backup(path, h)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sb._atomic_write(path, new_text)
        written = _sha(new_text.encode("utf-8"))
    else:
        bak, written = None, (rec or {}).get("written_sha")
    if rec:
        restore = rec.get("restore")
        if _sha(raw or b"") != rec.get("written_sha"):
            restore = None                             # edited since the last change: no exact restore
    else:
        restore = {"existed": raw is not None,
                   "b64": base64.b64encode(raw).decode("ascii") if raw is not None else None}
    new = dict(rec or {})
    new.update({"scope": scope,
                "project_dir": os.path.dirname(os.path.dirname(path))
                if scope in ("project", "local") else None,
                "settings_path": path, "written_sha": written, "restore": restore})
    new.update(fields)
    st["profiles"][path] = new
    _save_state(st, h)
    return bak


def _note_if_not_ignored(path, out):
    """settings.local.json is personal (absolute paths, your own rules). Claude Code has git ignore
    it when it makes the file; a file gt made is not covered, so say so when git would pick it up."""
    root = sb._git_root(os.path.dirname(path))
    if not root or os.path.basename(path) != "settings.local.json":
        return
    try:
        import subprocess
        rc = subprocess.run(["git", "-C", root, "check-ignore", "-q", path],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10).returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    if rc == 1:                                      # 0 ignored, 1 not ignored, else not a repo
        out("  note: git does not ignore %s yet: add .claude/settings.local.json to .gitignore (or "
            ".git/info/exclude) so this personal file is not committed." % path)


def turn_on(scope="project", project_dir=None, runners=None, sandbox=None, until=None,
            dry_run=False, h=None, now=None, out=print, dry_label=None):
    if scope not in SCOPES:
        raise UsageError("--scope must be one of: %s" % ", ".join(SCOPES))
    secs = parse_until(until) if until else None
    prefixes = parse_runners(runners, h)
    real = check_sandbox(sandbox, h) if sandbox else None
    rules = build_rules(prefixes, real, h)
    path = settings_for(scope, project_dir, h)
    doc, raw = _load_doc(path)
    t = _now(now)
    st = load_state(h)
    rec = st["profiles"].get(path)
    owned = list(rec["added"]) if rec else []
    new_doc = json.loads(json.dumps(doc))
    nl = sb._ensure_list(new_doc, "permissions.allow")
    for e in owned:                                    # a re-run that wants less drops the surplus
        if e not in rules and e in nl:
            nl.remove(e)
    owned = [e for e in owned if e in rules]
    added = []
    for r in rules:
        if r not in nl:
            nl.append(r)
            added.append(r)
    owned += [r for r in added if r not in owned]
    sb._prune_empty(new_doc, "permissions.allow")
    if isinstance(new_doc.get("permissions"), dict) and not new_doc["permissions"]:
        del new_doc["permissions"]
    new_text = _dump(new_doc)
    changed = new_doc != doc or raw is None
    if scope == "user":
        out("note: --scope user is broader: the rules reach EVERY project on this machine, not "
            "one repo. The project scope is the default.")
    if dry_run:
        out(dry_label or "dry run: nothing is written. This is the change `on` would make to %s:"
            % path)
        out(_diff(path, _view(raw), _view(new_text.encode("utf-8")),
                  old_name=path if raw is not None else
                  "/dev/null (the file does not exist yet)") if changed
            else "(no change: every rule is already there)")
        if secs:
            out("it would expire at %s" % _stamp(t + secs))
        return 0
    _confirm_terminal("testing profile on (%s, %d rules)" % (scope, len(rules)))
    expires = t + secs if secs else None
    bak = _commit(scope, path, raw, rec, new_text, changed,
                  {"added": owned, "sandbox": real, "runners": prefixes,
                   "created_at": (rec or {}).get("created_at", t), "expires_at": expires}, st, h)
    _log({"on": path, "scope": scope, "rules": len(owned), "added": len(added),
          "expires_at": expires}, h)
    out("testing profile: ON (%s, %s), %d rules  (%d added to %s)" % (
        scope, _expiry_text(expires), len(owned), len(added), path))
    if bak:
        out("  settings backed up first: %s" % bak)
    if scope == "local":
        _note_if_not_ignored(path, out)
    out("  Claude Code applies the rules when it reloads settings; restart it if it still asks.")
    out("  The Core-rule gates are untouched. Turn it off with: gt_testing_profile.py off%s"
        % ((" --scope %s" % scope) if scope != "project" else ""))
    return 0


# --------------------------------------------------------------------- the doctor's fixes
def check_directory(path, h=None):
    """-> the real path of a folder that may become a working folder, or raises ProfileError.
    The same refusals as --sandbox (the vault, home itself, credential, login and start-up
    folders), because a working folder is somewhere Claude may read and, in a mode that accepts
    edits, write."""
    real = check_sandbox(path, h, flag="add-dir")
    if not os.path.isdir(real):
        raise ProfileError("refused: add-dir %s is not a folder that exists" % path)
    return real


def _vault_conflict(root, h=None, strict=False):
    """Why a project folder cannot take a fix, or None: its settings file would sit in the vault
    (whose writes keep the write queue), or -- `strict` -- it holds the vault."""
    import gt_io_policy as iop
    vault = iop.vault_root(h)
    if not vault:
        return None
    real = os.path.realpath(root)
    if iop._inside(real, vault):
        return "is, or is inside, your vault"
    if strict and iop._inside(vault, real):
        return "contains your vault"
    return None


def _same_dir(entry, real, h=None):
    if not isinstance(entry, str) or not entry.strip():
        return False
    e = entry.strip()
    if e == "~" or e.startswith("~/"):
        e = os.path.join(sb.home(h), e[2:])
    return os.path.isabs(e) and os.path.normcase(os.path.realpath(e)) == os.path.normcase(real)


def merge_expiry(old, new):
    """A fix never lengthens or removes a limit that is already running."""
    vals = [v for v in (old, new) if v is not None]
    return min(vals) if vals else None


def apply_fix(kind, arg=None, project_dir=None, until=None, dry_run=False, assume_yes=False,
              rerun=None, h=None, now=None, out=print):
    """One of the doctor's fixes, into the project's settings.local.json (scope `local`).

    kind add-dir       `arg` is a folder: permissions.additionalDirectories gains it
         allow         `arg` is a runner prefix: permissions.allow gains Bash(<prefix>:*)
         accept-edits  permissions.defaultMode becomes acceptEdits (only from default / unset)

    Judged by the profile's own checks (check_sandbox, check_runner, gt_lockdown's rule judge),
    shown as a diff, confirmed once by a person at a terminal, backed up, recorded in the same
    state record `on` uses, and taken out again by `off` / the --until sweep. -> exit code."""
    if kind not in FIX_KINDS:
        raise UsageError("unknown fix %r (one of: %s)" % (kind, ", ".join(FIX_KINDS)))
    secs = parse_until(until) if until else None
    path = settings_for("local", project_dir, h)
    root = os.path.dirname(os.path.dirname(path))
    why = _vault_conflict(root, h, strict=(kind == "accept-edits"))
    if why:
        raise ProfileError("refused (vault): this project folder %s (%s). The vault's writes keep "
                           "the write queue and its guards, so a fix is never made there; run it "
                           "for the code repository with --project-dir." % (why, root))
    doc, raw = _load_doc(path)
    st = load_state(h)
    rec = st["profiles"].get(path)
    new_doc = json.loads(json.dumps(doc))
    patch = {}
    if kind == "add-dir":
        real = check_directory(arg, h)
        perms_dirs = sb._get_list(doc, "permissions.additionalDirectories") or []
        if any(_same_dir(e, real, h) for e in perms_dirs):
            out("nothing to change: %s is already a working folder in %s" % (real, path))
            return 0
        sb._ensure_list(new_doc, "permissions.additionalDirectories").append(real)
        patch["dirs"] = list((rec or {}).get("dirs") or []) + [real]
        what = "add %s as a working folder (permissions.additionalDirectories)" % real
        logged = {"dir": real}
    elif kind == "allow":
        rule = build_rules([check_runner(arg, h)], None, h)[0]
        if rule in (sb._get_list(doc, "permissions.allow") or []):
            out("nothing to change: %s is already allowed in %s" % (rule, path))
            return 0
        sb._ensure_list(new_doc, "permissions.allow").append(rule)
        patch["extra_allow"] = list((rec or {}).get("extra_allow") or []) + [rule]
        what = "allow %s (permissions.allow)" % rule
        logged = {"rule": rule}
    else:
        perms = doc.get("permissions")
        was = perms.get("defaultMode") if isinstance(perms, dict) else None
        if was == "acceptEdits":
            out("nothing to change: defaultMode is already acceptEdits in %s" % path)
            return 0
        if was not in (None, "default"):
            raise ProfileError("refused: defaultMode is %r in %s; accept-edits only changes the "
                               "default mode (a mode you chose is not ours to replace)"
                               % (was, path))
        if not isinstance(new_doc.get("permissions"), dict):
            new_doc["permissions"] = {}
        new_doc["permissions"]["defaultMode"] = "acceptEdits"
        patch["mode"] = {"set": "acceptEdits", "was": was}
        what = "set defaultMode to acceptEdits (edits inside this project stop asking; commands still do)"
        logged = {"mode": "acceptEdits"}
    new_text = _dump(new_doc)
    old_text = _view(raw)
    changed = new_doc != doc or raw is None
    diff = _diff(path, old_text, _view(new_text.encode("utf-8")),
                 old_name=path if raw is not None else "/dev/null (the file does not exist yet)")
    out("fix: %s" % what)
    out("scope: this project only (%s); nothing outside it changes" % path)
    if dry_run:
        out("dry run: nothing is written. This is the change:")
        out(diff)
        return 0
    _require_terminal(rerun)
    out("settings shown normalised, values that look like secrets hidden:")
    out(diff)
    if not _ask_yes("Apply this one change?", assume_yes):
        out("declined: nothing changed.")
        return 1
    _unlock_confirm("testing fix %s" % kind)
    t = _now(now)
    expires = merge_expiry((rec or {}).get("expires_at"), t + secs if secs else None)
    fields = dict(patch)
    fields.update({"created_at": (rec or {}).get("created_at", t), "expires_at": expires})
    for k, v in (("added", []), ("sandbox", None), ("runners", [])):
        if not rec or k not in rec:
            fields.setdefault(k, v)
    bak = _commit("local", path, raw, rec, new_text, changed, fields, st, h)
    _log(dict({"fix": kind, "path": path, "expires_at": expires}, **logged), h)
    out("fix applied (%s). Recorded for teardown; undo it with: gt_testing_profile.py off"
        % _expiry_text(expires))
    if bak:
        out("  settings backed up first: %s" % bak)
    if rec and expires != rec.get("expires_at"):
        out("  note: one time limit covers everything gt added to this file, earlier changes "
            "included.")
    _note_if_not_ignored(path, out)
    out("  Restart Claude Code if it still asks: it reads settings when it starts.")
    return 0


# --------------------------------------------------------------------- off
def _expiry_text(expires):
    return "no expiry" if expires is None else "expires " + _stamp(expires)


def _stamp(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _owned(rec):
    """Every entry a record says gt put in its file, as (settings key, entry) pairs."""
    out = [("permissions.allow", e) for e in (rec.get("added") or []) + (rec.get("extra_allow") or [])]
    out += [("permissions.additionalDirectories", e) for e in rec.get("dirs") or []]
    return out


def _strip_owned(doc, rec):
    """Take what the record says gt added out of `doc` (in place). -> labels of what went."""
    removed = []
    for key, e in _owned(rec):
        lst = sb._get_list(doc, key)
        if lst is not None and e in lst:
            lst.remove(e)
            removed.append(e)
    for key in ("permissions.allow", "permissions.additionalDirectories"):
        sb._prune_empty(doc, key)
    mode = rec.get("mode")
    perms = doc.get("permissions")
    if isinstance(mode, dict) and isinstance(perms, dict) and perms.get("defaultMode") == mode.get("set"):
        if mode.get("was") is None:
            del perms["defaultMode"]
        else:
            perms["defaultMode"] = mode["was"]
        removed.append("defaultMode=%s" % mode["set"])
    if isinstance(perms, dict) and not perms:
        del doc["permissions"]
    return removed


def _untouched(path, rec):
    """Is the file still exactly as gt's last change left it? Then the original bytes can be put
    back and nothing anyone typed since can be lost."""
    cur = _read_bytes(path)
    return cur is not None and bool(rec.get("restore")) and bool(rec.get("written_sha")) \
        and _sha(cur) == rec["written_sha"]


def _end(path, rec, h=None):
    """Take one recorded profile out of its settings file. -> the list of removed entries."""
    cur = _read_bytes(path)
    restore = rec.get("restore")
    removed = []
    if _untouched(path, rec):
        # still exactly as the last change left it: put the original bytes back
        _backup(path, h)
        removed = [e for _k, e in _owned(rec)] + (
            ["defaultMode=%s" % rec["mode"]["set"]] if isinstance(rec.get("mode"), dict) else [])
        if restore.get("existed"):
            sb._atomic_write(path, base64.b64decode(restore["b64"]).decode("utf-8"))
        else:
            os.unlink(path)
            _rmdir_if_empty(os.path.dirname(path))
        return removed
    if cur is None:
        return removed
    doc, _ = _load_doc(path)
    removed = _strip_owned(doc, rec)
    if removed:
        _backup(path, h)
        if not doc and not (restore or {}).get("existed", True):
            os.unlink(path)
            _rmdir_if_empty(os.path.dirname(path))
        else:
            sb._atomic_write(path, _dump(doc))
    return removed


def _rmdir_if_empty(d):
    try:
        os.rmdir(d)
    except OSError:
        pass


def _what_went(rec, removed):
    """'3 rule(s), 1 working folder(s), defaultMode back to default' for the off message."""
    dirs = set(rec.get("dirs") or [])
    nd = len([e for e in removed if e in dirs])
    nm = 1 if any(str(e).startswith("defaultMode=") for e in removed) else 0
    parts = ["%d rule(s)" % (len(removed) - nd - nm)]
    if nd:
        parts.append("%d working folder(s)" % nd)
    if nm:
        parts.append("defaultMode back to %s" % ((rec.get("mode") or {}).get("was") or "default"))
    return ", ".join(parts)


def turn_off(scope=None, project_dir=None, h=None, out=print):
    """End gt's loosening in settings files and show what changed.

    scope None  this project's records: .claude/settings.json AND .claude/settings.local.json
    scope X     only that file (project, local or user)"""
    scopes = ("project", "local") if scope is None else (scope,)
    st = load_state(h)
    paths = [(sc, settings_for(sc, project_dir, h)) for sc in scopes]
    done = 0
    for sc, path in paths:
        rec = st["profiles"].get(path)
        if not rec:
            continue
        before = _read_bytes(path)
        exact = _untouched(path, rec)
        removed = _end(path, rec, h)
        after = _read_bytes(path)
        del st["profiles"][path]
        _save_state(st, h)
        _log({"off": path, "removed": len(removed)}, h)
        done += 1
        out("testing profile: off (%s): removed %s from %s; hand-edited entries untouched"
            % (rec["scope"], _what_went(rec, removed), path))
        if exact and after is not None:
            out("  %s is back to how it was before gt's first change, byte for byte. The diff:"
                % os.path.basename(path))
        elif after is None:
            out("  gt made %s and nothing else was in it, so it is gone again. The diff:"
                % os.path.basename(path))
        elif before == after:
            out("  %s had none of gt's entries left; nothing to change." % os.path.basename(path))
        else:
            out("  %s was edited after gt's change, so only gt's own entries were taken out. "
                "The diff:" % os.path.basename(path))
        diff = _bytes_diff(path, before, after)
        if diff:
            out(diff.rstrip("\n"))
    if not done:
        out("testing profile: not on for %s (nothing to change)"
            % " or ".join(p for _s, p in paths))
        return 0
    out("  Restart Claude Code for the change to apply.")
    return 0


# --------------------------------------------------------------------- sweep, status
def sweep(h=None, now=None):
    """End every profile whose --until has passed. -> [message]. Removes only; never adds."""
    t = _now(now)
    st = load_state(h)
    msgs = []
    for path, rec in sorted(st["profiles"].items()):
        exp = rec.get("expires_at")
        if exp is not None and t >= exp:
            try:
                n = len(_end(path, rec, h))
            except Exception as e:                       # noqa: BLE001 - say so, keep the record
                msgs.append("testing profile (%s) expired but could not be removed from %s: %s"
                            % (rec.get("scope"), path, e))
                continue
            del st["profiles"][path]
            _log({"expired": path, "removed": n}, h)
            msgs.append("testing profile (%s) expired at %s and was removed: %d change(s) taken out "
                        "of %s" % (rec.get("scope"), _stamp(exp), n, path))
    if msgs:
        _save_state(st, h)
    return msgs


def profiles(h=None):
    return sorted(load_state(h)["profiles"].items())


def summary_line(rec):
    n = len(rec.get("added") or []) + len(rec.get("extra_allow") or [])
    line = "testing profile: ON (%s, %s), %d rules" % (
        rec.get("scope"), _expiry_text(rec.get("expires_at")), n)
    nd = len(rec.get("dirs") or [])
    if nd:
        line += ", %d working folder%s" % (nd, "" if nd == 1 else "s")
    if isinstance(rec.get("mode"), dict):
        line += ", defaultMode %s" % rec["mode"].get("set")
    return line


def is_on(h=None, now=None):
    sweep(h, now)
    return bool(load_state(h)["profiles"])


def status_lines(h=None, now=None, sweep_first=True):
    lines = list(sweep(h, now)) if sweep_first else []
    ps = profiles(h)
    if not ps:
        lines.append("testing profile: off")
        return lines
    for path, rec in ps:
        lines.append(summary_line(rec))
        lines.append("  settings: %s" % path)
        if rec.get("sandbox"):
            lines.append("  writes allowed only under: %s" % rec["sandbox"])
        for r in (rec.get("added") or []) + (rec.get("extra_allow") or []):
            lines.append("  allow %s" % r)
        for d in rec.get("dirs") or []:
            lines.append("  working folder %s" % d)
        if isinstance(rec.get("mode"), dict):
            lines.append("  defaultMode %s (was %s)" % (rec["mode"].get("set"),
                                                      rec["mode"].get("was") or "default"))
    return lines


def status(h=None, now=None, out=print):
    for l in status_lines(h, now):
        out(l)
    return 0


# --------------------------------------------------------------------- CLI
def _parse(argv):
    """Options are taken by hand: a value such as --dangerously-skip-permissions must reach the
    refusal that names it, not die in an option parser."""
    opts = {"scope": None, "project_dir": None, "runners": None, "sandbox": None,
            "until": None, "dry_run": False, "hook": False}
    takes = {"--scope": "scope", "--project-dir": "project_dir", "--runners": "runners",
             "--sandbox": "sandbox", "--until": "until"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in takes:
            if i + 1 >= len(argv):
                raise UsageError("%s needs a value" % a)
            opts[takes[a]] = argv[i + 1]
            i += 2
        elif a == "--dry-run":
            opts["dry_run"] = True
            i += 1
        elif a == "--hook":
            opts["hook"] = True
            i += 1
        else:
            raise UsageError("unknown argument %r" % a)
    if opts["scope"] is not None and opts["scope"] not in SCOPES:
        raise UsageError("--scope must be one of: %s" % ", ".join(SCOPES))
    return opts


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    cmd, rest = a[0], a[1:]
    try:
        if cmd not in ("on", "off", "status", "sweep"):
            raise UsageError("unknown command %r" % cmd)
        o = _parse(rest)
        if cmd == "on":
            return turn_on(o["scope"] or "project", o["project_dir"], o["runners"], o["sandbox"],
                           o["until"], o["dry_run"])
        if cmd == "off":
            return turn_off(o["scope"], o["project_dir"])
        if cmd == "status":
            return status()
        msgs = sweep()                                   # sweep
        if o["hook"]:
            if msgs:
                print(json.dumps({"systemMessage": "gt testing profile: " + " ".join(msgs)}))
        else:
            for m in msgs:
                print(m)
        return 0
    except ProfileError as e:
        print(str(e))
        return 1
    except UsageError as e:
        print("usage: %s" % e, file=sys.stderr)
        print("gt_testing_profile.py on|off|status|sweep [--scope project|local|user] [--project-dir DIR] "
              "[--runners a,b] [--sandbox DIR] [--until 4h] [--dry-run]", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
