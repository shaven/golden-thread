#!/usr/bin/env python3
"""gt_sandbox -- gt sandbox mode: contain Claude's own tools, reach the vault through gt (0.20.0).

    gt_sandbox.py status [--json]              what is on, and what it enforces on THIS platform
    gt_sandbox.py plan   [--json]              the settings gt would write, written nowhere
    gt_sandbox.py apply  [--force] [--json]    merge them into ~/.claude/settings.json
    gt_sandbox.py remove [--json]              take out exactly what gt added
    gt_sandbox.py check  [--json]              drift: is what gt wrote still there, still current?
    gt_sandbox.py verify [--json]              PASS / FAIL / NOT-CHECKED rows (gt_unlock.py verify)

Normally driven by the `sandbox_mode` setting (gt_settings.py set sandbox_mode on|off), which
calls apply / remove. `sandbox_vault_reads` (deny, the default, or allow) decides whether the
shell and the file tools may READ the vault too; writes are always denied while the mode is on.

WHAT IT WRITES (user settings, ~/.claude/settings.json -- checked against
code.claude.com/docs/en/sandboxing, /settings-reference and /permissions, 2026-10-03):

  sandbox.enabled true, sandbox.allowUnsandboxedCommands false, sandbox.failIfUnavailable true
      Claude Code's OS sandbox (Seatbelt on macOS, bubblewrap on Linux and WSL2) around the
      Bash, PowerShell and Monitor tools and every process they start; no unsandboxed retry;
      refuse to start rather than run unsandboxed. NOT written on native Windows, where the
      sandbox does not exist and failIfUnavailable would stop Claude Code from starting.
  sandbox.filesystem.denyWrite   the vault, ~/.claude/golden-thread (hooks, state, the unlock
      home and its markers), the unlock socket's directory when it lives outside that, the LOTR
      home and store, ~/.claude/plugins, ~/.claude/settings.json and vault-config.json
  sandbox.filesystem.denyRead    the unlock home (sealed store, TOTP seed, recovery codes),
      the LOTR home and store, every LOCKED vault folder (a `.gt-locked` stub), and -- with
      sandbox_vault_reads deny -- the whole vault
  sandbox.filesystem.allowWrite  the queue inbox, ~/.gt-inbox: the one writable place, where
      gt_write_queue.py run from Claude's shell leaves its requests for the broker
  permissions.deny               Edit(...) on every denyWrite path and Read(...) on every
      denyRead path, for Claude's file tools (Read, Edit, Write, NotebookEdit, Grep, Glob):
      the sandbox does not cover them. Plus Edit on any .claude/settings.json and
      .claude/settings.local.json, so the file tools cannot switch the sandbox back off.

  Write/NotebookEdit path rules are NOT written: the permissions page says Claude Code
  "checks file permissions against Edit(path) and Read(path) rules only" and ignores (and
  warns about) Write(...) and NotebookEdit(...) path rules; Edit covers every editing tool.

HOW IT MERGES. Only gt's own entries are ever added or removed. Every entry gt adds is recorded
in ~/.claude/golden-thread/sandbox/state.json, so `remove` takes out exactly those and leaves a
user's own entries -- including an identical one the user had first -- in place. A boolean gt
set is restored to what it was before, unless someone changed it since. Managed settings are
never written; `check` reports when they override what gt asked for. Each write is atomic, and
the previous settings.json is copied to ~/.claude/golden-thread/backups/ first.

WHAT IT DOES NOT DO. It is a fence around Claude's own tools. Hooks, MCP servers (gt's vault
server, LOTR) and anything you run in a terminal run outside it, by Claude Code's design -- which
is how the broker can still apply the queue. Malware running as you is not stopped. On native
Windows only the permission rules apply, which is friction, not a boundary: a script Claude runs
can still open any file. See SECURITY.md, "gt sandbox mode".

Stdlib only. Exit: 0 ok, 1 refused / drift / FAIL, 2 usage.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

IS_WINDOWS = os.name == "nt"
STATE_SCHEMA = 1
# Booleans gt sets under `sandbox`, with the value it sets. Order is the order written.
SANDBOX_BOOLS = (("enabled", True), ("allowUnsandboxedCommands", False),
                 ("failIfUnavailable", True))
LISTS = ("sandbox.filesystem.denyWrite", "sandbox.filesystem.denyRead",
         "sandbox.filesystem.allowWrite", "permissions.deny")
ABSENT = "__absent__"
INBOX_NAME = ".gt-inbox"
QUEUE_SUBDIR = "queue"
LOCK_STUB = ".gt-locked"
PASS, FAIL, NC = "PASS", "FAIL", "NOT-CHECKED"
# Edits that would let the file tools switch the sandbox off from a project's settings: a
# local or shared project settings file outranks user settings (code.claude.com/docs/en/settings,
# "Settings precedence"). The sandbox itself already write-protects these inside the working
# directory for shell commands ("Protected paths"); these rules do it for the file tools.
SETTINGS_FILE_RULES = ("Edit(//**/.claude/settings.json)", "Edit(//**/.claude/settings.local.json)")


class SandboxError(Exception):
    pass


# ------------------------------------------------------------------ where things are
def home(h=None):
    return os.path.abspath(h or os.path.expanduser("~"))


def claude_dir(h=None):
    return os.path.join(home(h), ".claude")


def settings_path(h=None):
    return os.path.join(claude_dir(h), "settings.json")


def config_path(h=None):
    return os.path.join(claude_dir(h), "vault-config.json")


def gt_home(h=None):
    return os.path.join(claude_dir(h), "golden-thread")


def state_path(h=None):
    return os.path.join(gt_home(h), "sandbox", "state.json")


def inbox_dir(h=None):
    """The one place a sandboxed command may write: ~/.gt-inbox. Queue requests go in its
    queue/ folder; anything else you put there is yours and gt leaves it alone."""
    return os.path.join(home(h), INBOX_NAME)


def inbox_queue_dir(h=None):
    return os.path.join(inbox_dir(h), QUEUE_SUBDIR)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d
    except (OSError, ValueError):
        return default


def read_config(h=None):
    d = _read_json(config_path(h), {})
    return d if isinstance(d, dict) else {}


def setting(name, h=None):
    """A gt setting's value with its default, read from vault-config.json directly so this
    works from the hooks dir, a test home, or a half-installed machine."""
    defaults = {"sandbox_mode": "off", "sandbox_vault_reads": "deny", "vault_mcp": "auto"}
    allowed = {"sandbox_mode": ("off", "on"), "sandbox_vault_reads": ("deny", "allow"),
               "vault_mcp": ("auto", "on", "off")}
    v = read_config(h).get(name)
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in allowed.get(name, ()) else defaults.get(name)


def is_on(h=None):
    return setting("sandbox_mode", h) == "on"


def vault_mcp_on(h=None):
    """Does the vault MCP server offer its tools? `auto` follows sandbox_mode."""
    v = setting("vault_mcp", h)
    return v == "on" or (v == "auto" and is_on(h))


def vault_path(h=None):
    env = os.environ.get("GT_VAULT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    p = read_config(h).get("vault_path")
    if isinstance(p, str) and p and os.path.isdir(os.path.expanduser(p)):
        return os.path.abspath(os.path.expanduser(p))
    return None


def platform_mode():
    """macos | linux | wsl2 | wsl1 | windows | other -- what Claude Code's sandbox can do here.
    Per code.claude.com/docs/en/sandboxing: macOS (Seatbelt), Linux and WSL2 (bubblewrap);
    native Windows runs commands unsandboxed. WSL1 is not WSL2."""
    if IS_WINDOWS:
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        try:
            rel = os.uname().release.lower()
        except AttributeError:
            rel = ""
        try:
            with open("/proc/version", "r", encoding="utf-8") as fh:
                ver = fh.read().lower()
        except OSError:
            ver = ""
        if "microsoft" in rel or "microsoft" in ver or "wsl" in rel:
            return "wsl2" if ("wsl2" in rel or "microsoft-standard" in rel) else "wsl1"
        return "linux"
    return "other"


def os_sandbox_supported(mode=None):
    return (mode or platform_mode()) in ("macos", "linux", "wsl2")


def linux_deps_missing():
    """bubblewrap and socat, which the Linux / WSL2 sandbox needs (docs: 'Set up Linux and
    WSL2'). Missing either + failIfUnavailable = Claude Code refuses to start."""
    return [b for b in ("bwrap", "socat") if not shutil.which(b)]


def unlock_home(h=None):
    return os.path.join(gt_home(h), "unlock")


def unlock_socket_dir(h=None):
    """The unlock authority's socket directory when it lives OUTSIDE ~/.claude/golden-thread
    (gt_ipc falls back to /tmp/gt-<uid>-<hash>/ for a long home path), else None."""
    if IS_WINDOWS:
        return None
    try:
        import gt_ipc
        addr = gt_ipc.default_address(unlock_home(h), "unlockd")
    except Exception:                                    # noqa: BLE001
        return None
    d = os.path.dirname(addr)
    if _inside(d, gt_home(h)):
        return None
    return d


def lotr_homes(h=None):
    """Every LOTR home: ~/.config/gt-lotr (all zones), plus $LOTR_HOME when set elsewhere."""
    out = [os.path.join(home(h), ".config", "gt-lotr")]
    env = os.environ.get("LOTR_HOME")
    if env:
        p = os.path.abspath(os.path.expanduser(env))
        if not _inside(p, out[0]):
            out.append(p)
    return out


def lotr_store(h=None):
    """LOTR's file: secret store ($LOTR_STORE_DIR, else ~/.secrets)."""
    env = os.environ.get("LOTR_STORE_DIR")
    return os.path.abspath(os.path.expanduser(env)) if env else os.path.join(home(h), ".secrets")


def _inside(path, root):
    try:
        path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def locked_folders(vault):
    """Vault-relative folders holding gt_lock's `.gt-locked` stub (0.20.0). Folder locks are
    vault-only (gt_lock refuses anything outside the vault), and under sandbox mode each one is
    read-denied to the shell and the file tools whatever sandbox_vault_reads says."""
    out = []
    if not vault or not os.path.isdir(vault):
        return out
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if LOCK_STUB in files:
            out.append(root)
            dirs[:] = []
    return sorted(out)


# ------------------------------------------------------------------ the plan
def _spellings(path):
    """The path as configured and, when different, as resolved: a symlinked vault is denied
    under both names (Seatbelt and bubblewrap act on the real path; a rule on the link alone
    would miss it)."""
    a = os.path.abspath(path)
    r = os.path.realpath(path)
    return [a] if a == r else [a, r]


def sandbox_path(p):
    """A path for sandbox.filesystem.*: an absolute POSIX path (docs: 'Sandbox path prefixes')."""
    return p.replace("\\", "/") if IS_WINDOWS else p


_GLOB = re.compile(r"([\[\]*?!#])")


def rule_path(p, directory):
    """A path for a Read(...) / Edit(...) rule: `//` + the absolute path (docs: 'Read and Edit':
    `//path` is absolute; a single `/` anchors at the settings file). Windows paths are
    normalised to POSIX form before matching -- C:\\Users\\x becomes /c/Users/x. Gitignore
    pattern characters in the path are escaped so the rule matches only that path. A directory
    gets `/**`."""
    s = p.replace("\\", "/")
    if IS_WINDOWS or re.match(r"^[A-Za-z]:/", s):
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        if m:
            s = "/%s/%s" % (m.group(1).lower(), m.group(2))
    s = _GLOB.sub(r"\\\1", s)
    s = "/" + s if s.startswith("/") else "//" + s
    return s.rstrip("/") + ("/**" if directory else "")


def plan(h=None, vault=None, vault_reads=None, mode=None):
    """-> the settings gt wants while sandbox mode is on, as
    {"sandbox_bools": {...}, "lists": {list key: [entries]}, "paths": {...}, "mode", "notes"}.
    Pure: it reads the filesystem (where things are) but writes nothing."""
    mode = mode or platform_mode()
    vault = vault or vault_path(h)
    vault_reads = vault_reads or setting("sandbox_vault_reads", h)
    if not vault:
        raise SandboxError("no vault: set vault_path in %s first (Core rule 2: the vault is "
                           "named, never guessed)" % config_path(h))
    gth = gt_home(h)
    uh = unlock_home(h)
    sock = unlock_socket_dir(h)
    lotr = lotr_homes(h)
    store = lotr_store(h)
    locked = locked_folders(vault)
    deny_write_dirs, deny_read_dirs = [], []
    deny_write_dirs += _spellings(vault)
    deny_write_dirs += _spellings(gth)                  # hooks, state, unlock home, markers
    if sock:
        deny_write_dirs += _spellings(sock)
        deny_read_dirs += _spellings(sock)
    for p in lotr + [store]:
        deny_write_dirs.append(p)
        deny_read_dirs.append(p)
    deny_write_dirs.append(os.path.join(claude_dir(h), "plugins"))
    deny_write_files = [settings_path(h), config_path(h)]
    deny_read_dirs += _spellings(uh)
    if vault_reads == "deny":
        deny_read_dirs += _spellings(vault)
    else:
        for f in locked:
            deny_read_dirs += _spellings(f)
    deny_write_dirs = list(dict.fromkeys(deny_write_dirs))
    deny_read_dirs = list(dict.fromkeys(deny_read_dirs))
    inbox = inbox_dir(h)
    lists = {k: [] for k in LISTS}
    notes = []
    if os_sandbox_supported(mode):
        lists["sandbox.filesystem.denyWrite"] = [sandbox_path(p) for p in
                                                 deny_write_dirs + deny_write_files]
        lists["sandbox.filesystem.denyRead"] = [sandbox_path(p) for p in deny_read_dirs]
        lists["sandbox.filesystem.allowWrite"] = [sandbox_path(inbox)]
        bools = dict(SANDBOX_BOOLS)
    else:
        bools = {}
        notes.append("%s: Claude Code has no OS sandbox here, so only the permission rules "
                     "for Claude's file tools are written -- friction, not a boundary"
                     % {"windows": "native Windows", "wsl1": "WSL1"}.get(mode, mode))
    deny = [("Edit(%s)" % rule_path(p, True)) for p in deny_write_dirs]
    deny += [("Edit(%s)" % rule_path(p, False)) for p in deny_write_files]
    deny += [("Read(%s)" % rule_path(p, True)) for p in deny_read_dirs]
    deny += list(SETTINGS_FILE_RULES)
    lists["permissions.deny"] = list(dict.fromkeys(deny))
    for p in [vault, inbox]:
        if _GLOB.search(p.replace("\\", "/")) and mode in ("linux", "wsl2"):
            notes.append("%s contains a glob character: on Linux the sandbox skips write-list "
                         "entries with * ? or [ (settings-reference, 'Sandbox path prefixes')" % p)
    return {"mode": mode, "vault": vault, "vault_reads": vault_reads,
            "sandbox_bools": bools, "lists": lists, "notes": notes,
            "paths": {"vault": vault, "gt_home": gth, "unlock_home": uh,
                      "unlock_socket_dir": sock, "lotr_homes": lotr, "lotr_store": store,
                      "inbox": inbox, "locked_folders": locked}}


# ------------------------------------------------------------------ settings.json I/O
def _get_list(d, key):
    cur = d
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur if isinstance(cur, list) else None


def _ensure_list(d, key):
    parts = key.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    lst = cur.get(parts[-1])
    if not isinstance(lst, list):
        lst = []
        cur[parts[-1]] = lst
    return lst


def _prune_empty(d, key):
    """Drop empty containers along `key` that gt's removal left behind."""
    parts = key.split(".")
    chain = [d]
    cur = d
    for part in parts[:-1]:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return
        chain.append(cur)
    for i in range(len(parts) - 1, -1, -1):
        parent, name = chain[i], parts[i]
        v = parent.get(name)
        if isinstance(v, (list, dict)) and not v:
            del parent[name]
        else:
            break


def load_settings(h=None):
    p = settings_path(h)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except ValueError as e:
        raise SandboxError("%s is not valid JSON (%s); fix it before turning sandbox mode on "
                           "or off -- gt never overwrites a file it cannot read" % (p, e))
    if not isinstance(d, dict):
        raise SandboxError("%s is not a JSON object" % p)
    return d


def _atomic_write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".gt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_settings(d, h=None):
    p = settings_path(h)
    if os.path.exists(p):
        backups = os.path.join(gt_home(h), "backups")
        os.makedirs(backups, exist_ok=True)
        bak = os.path.join(backups, "settings.json.%s.sandbox" % time.strftime("%Y%m%d_%H%M%S"))
        if not os.path.exists(bak):
            shutil.copyfile(p, bak)
    _atomic_write(p, json.dumps(d, indent=2) + "\n")


def load_state(h=None):
    st = _read_json(state_path(h), None)
    if not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA:
        return None
    return st


def save_state(st, h=None):
    _atomic_write(state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------ managed / project overrides
def managed_paths():
    """Managed settings files (code.claude.com/docs/en/managed-settings, 'File-based')."""
    if IS_WINDOWS:
        base = r"C:\Program Files\ClaudeCode"
    elif sys.platform == "darwin":
        base = "/Library/Application Support/ClaudeCode"
    else:
        base = "/etc/claude-code"
    out = [os.path.join(base, "managed-settings.json")]
    d = os.path.join(base, "managed-settings.d")
    try:
        out += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json"))
    except OSError:
        pass
    return out


def managed_sandbox(paths=None):
    """-> merged `sandbox` booleans the managed files set (later files win), {} when none."""
    out = {}
    for p in paths if paths is not None else managed_paths():
        d = _read_json(p, None)
        sb = d.get("sandbox") if isinstance(d, dict) else None
        if isinstance(sb, dict):
            for k, v in sb.items():
                if isinstance(v, bool):
                    out[k] = v
            fs = sb.get("filesystem")
            if isinstance(fs, dict) and isinstance(fs.get("disabled"), bool):
                out["filesystem.disabled"] = fs["disabled"]
    return out


def project_overrides(cwd=None):
    """Project settings in `cwd` that outrank user settings and loosen the sandbox: a local or
    shared project file setting sandbox.enabled false (or the filesystem layer off). -> [text]."""
    cwd = cwd or os.getcwd()
    out = []
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(cwd, ".claude", name)
        d = _read_json(p, None)
        sb = d.get("sandbox") if isinstance(d, dict) else None
        if not isinstance(sb, dict):
            continue
        if sb.get("enabled") is False:
            out.append("%s sets sandbox.enabled false, which outranks your user settings in "
                       "this project" % p)
        if sb.get("allowUnsandboxedCommands") is True:
            out.append("%s sets sandbox.allowUnsandboxedCommands true (user settings' false "
                       "holds from Claude Code v2.1.285)" % p)
        fs = sb.get("filesystem")
        if isinstance(fs, dict) and (fs.get("allowRead") or fs.get("allowWrite")):
            out.append("%s adds sandbox.filesystem allowRead/allowWrite entries; a narrower "
                       "allowRead re-opens part of a denied region" % p)
    return out


# ------------------------------------------------------------------ apply / remove
def _bool_get(d, key):
    sb = d.get("sandbox")
    if not isinstance(sb, dict) or key not in sb:
        return ABSENT
    return sb[key]


def apply(h=None, force=False, vault=None, reads=None):
    """Merge the plan into user settings and record what gt added. Idempotent: entries that
    are already there are left alone (and not claimed), entries gt added earlier that the plan
    no longer wants (a moved vault, an unlocked folder) are taken out. -> report dict."""
    if vault:
        vault = os.path.abspath(os.path.expanduser(vault))
    p = plan(h, vault=vault, vault_reads=reads)
    mode = p["mode"]
    if mode in ("linux", "wsl2"):
        missing = linux_deps_missing()
        if missing and not force:
            raise SandboxError(
                "the %s sandbox needs %s, which %s not on PATH. With failIfUnavailable set, "
                "Claude Code would refuse to start. Install %s (e.g. sudo apt-get install "
                "bubblewrap socat), then turn sandbox mode on again -- or pass --force to "
                "write the settings anyway." % (mode, " and ".join(missing),
                                               "is" if len(missing) == 1 else "are",
                                               " and ".join(missing)))
    d = load_settings(h)
    sb = d.get("sandbox") if isinstance(d.get("sandbox"), dict) else {}
    fs = sb.get("filesystem") if isinstance(sb.get("filesystem"), dict) else {}
    if fs.get("disabled") is True and os_sandbox_supported(mode):
        raise SandboxError("%s sets sandbox.filesystem.disabled true, which turns off the "
                           "filesystem layer every gt rule depends on. gt does not override "
                           "your own setting: remove it, then turn sandbox mode on again"
                           % settings_path(h))
    st = load_state(h) or {"schema": STATE_SCHEMA, "added": {k: [] for k in LISTS},
                           "booleans": {}}
    added = {k: list(st.get("added", {}).get(k) or []) for k in LISTS}
    changes = []
    for key in LISTS:
        want = p["lists"][key]
        lst = _get_list(d, key)
        # stale: entries gt added before that the plan no longer wants
        for e in [x for x in added[key] if x not in want]:
            if lst is not None and e in lst:
                lst.remove(e)
                changes.append("removed stale %s %s" % (key, e))
            added[key].remove(e)
        if want:
            lst = _ensure_list(d, key)
            for e in want:
                if e not in lst:
                    lst.append(e)
                    if e not in added[key]:
                        added[key].append(e)
                    changes.append("added %s %s" % (key, e))
        _prune_empty(d, key)
    bools = dict(st.get("booleans") or {})
    for name, val in SANDBOX_BOOLS:
        if name not in p["sandbox_bools"]:
            continue
        cur = _bool_get(d, name)
        if cur == val:
            continue                                     # already so: not gt's to restore
        if name not in bools:
            bools[name] = {"prev": cur, "set": val}
        d.setdefault("sandbox", {})
        if not isinstance(d["sandbox"], dict):
            d["sandbox"] = {}
        d["sandbox"][name] = val
        changes.append("set sandbox.%s %s (was %s)" % (name, json.dumps(val),
                                                      "unset" if cur == ABSENT else json.dumps(cur)))
    if changes:
        save_settings(d, h)
    st.update({"schema": STATE_SCHEMA, "added": added, "booleans": bools, "mode": mode,
               "vault": p["vault"], "vault_reads": p["vault_reads"],
               "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    save_state(st, h)
    os.makedirs(inbox_queue_dir(h), exist_ok=True)
    try:
        os.chmod(inbox_dir(h), 0o700)
    except OSError:
        pass
    return {"applied": True, "mode": mode, "changes": changes, "notes": p["notes"],
            "restart": bool(changes)}


def remove(h=None):
    """Take out exactly what gt added; restore booleans it changed. -> report dict."""
    st = load_state(h)
    if st is None:
        return {"removed": False, "changes": [], "notes": ["gt had written nothing (no %s)"
                                                         % state_path(h)]}
    d = load_settings(h)
    changes = []
    for key in LISTS:
        lst = _get_list(d, key)
        for e in st.get("added", {}).get(key) or []:
            if lst is not None and e in lst:
                lst.remove(e)
                changes.append("removed %s %s" % (key, e))
        _prune_empty(d, key)
    notes = []
    for name, rec in (st.get("booleans") or {}).items():
        cur = _bool_get(d, name)
        if cur != rec.get("set"):
            notes.append("sandbox.%s was changed since gt set it; left as %s" % (
                name, "unset" if cur == ABSENT else json.dumps(cur)))
            continue
        prev = rec.get("prev", ABSENT)
        if prev == ABSENT:
            d["sandbox"].pop(name, None)
        else:
            d["sandbox"][name] = prev
        changes.append("restored sandbox.%s to %s" % (name, "unset" if prev == ABSENT
                                                      else json.dumps(prev)))
    if isinstance(d.get("sandbox"), dict) and not d["sandbox"]:
        del d["sandbox"]
    if changes:
        save_settings(d, h)
    try:
        os.unlink(state_path(h))
    except OSError:
        pass
    return {"removed": True, "changes": changes, "notes": notes, "restart": bool(changes)}


# ------------------------------------------------------------------ drift / verify
def check(h=None, cwd=None, managed=None):
    """-> {"state": ok|drift|off|stale-off, "missing": [...], "stale": [...], "problems": [...],
    "mode", "notes"}. Never writes."""
    on = is_on(h)
    st = load_state(h)
    try:
        d = load_settings(h)
    except SandboxError as e:
        return {"state": "drift", "on": on, "missing": [], "stale": [], "problems": [str(e)],
                "mode": platform_mode(), "notes": []}
    if not on:
        left = []
        if st is not None:
            for key in LISTS:
                lst = _get_list(d, key) or []
                left += ["%s %s" % (key, e) for e in st.get("added", {}).get(key) or [] if e in lst]
        return {"state": "stale-off" if left else "off", "on": False, "missing": [],
                "stale": left, "problems": (["sandbox mode is off but gt's entries are still in "
                                             "settings.json: gt_sandbox.py remove"] if left else []),
                "mode": platform_mode(), "notes": []}
    try:
        p = plan(h)
    except SandboxError as e:
        return {"state": "drift", "on": True, "missing": [], "stale": [], "problems": [str(e)],
                "mode": platform_mode(), "notes": []}
    missing, stale, problems = [], [], []
    for key in LISTS:
        lst = _get_list(d, key) or []
        missing += ["%s %s" % (key, e) for e in p["lists"][key] if e not in lst]
        if st is not None:
            stale += ["%s %s" % (key, e) for e in st.get("added", {}).get(key) or []
                      if e not in p["lists"][key] and e in lst]
    for name, val in p["sandbox_bools"].items():
        if _bool_get(d, name) != val:
            missing.append("sandbox.%s %s" % (name, json.dumps(val)))
    sb = d.get("sandbox") if isinstance(d.get("sandbox"), dict) else {}
    fs = sb.get("filesystem") if isinstance(sb.get("filesystem"), dict) else {}
    if fs.get("disabled") is True:
        problems.append("sandbox.filesystem.disabled is true in your user settings: the "
                        "filesystem layer, and every gt deny, is off")
    if sb.get("allowAppleEvents") is True:
        problems.append("sandbox.allowAppleEvents is true: sandboxed commands can launch "
                        "other apps unsandboxed (docs: 'Security limitations')")
    ms = managed if managed is not None else managed_sandbox()
    for name, val in p["sandbox_bools"].items():
        if name in ms and ms[name] != val:
            problems.append("managed settings set sandbox.%s %s, which overrides gt's %s (gt "
                            "never changes managed settings)" % (name, json.dumps(ms[name]),
                                                                 json.dumps(val)))
    if ms.get("filesystem.disabled") is True:
        problems.append("managed settings turn the sandbox's filesystem layer off")
    problems += project_overrides(cwd)
    notes = list(p["notes"])
    exc = sb.get("excludedCommands")
    if isinstance(exc, list) and exc:
        notes.append("excludedCommands run OUTSIDE the sandbox: %s" % ", ".join(map(str, exc)))
    state = "ok" if not (missing or stale or problems) else "drift"
    return {"state": state, "on": True, "missing": missing, "stale": stale,
            "problems": problems, "mode": p["mode"], "notes": notes}


def claude_status(timeout=20):
    """`claude sandbox status` (Claude Code 2.1.289: one JSON line, no model call) -> dict or
    None. Read-only; used as a second opinion on what Claude Code itself resolves."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "sandbox", "status"], capture_output=True, text=True,
                             timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed((out.stdout or "").strip().splitlines()):
        try:
            v = json.loads(line)
            return v if isinstance(v, dict) else None
        except ValueError:
            continue
    return None


ENFORCED = {
    "macos": "Seatbelt (OS-enforced) around Claude's Bash/PowerShell/Monitor commands and "
             "their children, plus permission rules on Claude's file tools",
    "linux": "bubblewrap (OS-enforced) around Claude's Bash/PowerShell/Monitor commands and "
             "their children, plus permission rules on Claude's file tools",
    "wsl2": "bubblewrap (OS-enforced) around Claude's Bash/PowerShell/Monitor commands and "
            "their children, plus permission rules on Claude's file tools",
    "windows": "permission rules on Claude's file tools ONLY -- native Windows has no Claude "
               "Code sandbox, so a script Claude runs can still open any file: friction, "
               "not a boundary",
    "wsl1": "permission rules on Claude's file tools only (WSL1 has no sandbox): friction",
    "other": "permission rules on Claude's file tools only: friction",
}


def _probe_write(vault):
    """Try to create (and at once remove) a probe file in the vault. -> (wrote, errno name)."""
    path = os.path.join(vault, ".gt-sandbox-probe-%d" % os.getpid())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except OSError as e:
        import errno
        return False, errno.errorcode.get(e.errno, str(e.errno))
    os.close(fd)
    try:
        os.unlink(path)
    except OSError:
        pass
    return True, None


def verify_rows(h=None, cwd=None, live=True, status=None):
    """PASS / FAIL / NOT-CHECKED rows for `gt_unlock.py verify` and gt doctor."""
    rows = []

    def add(name, state, why):
        rows.append({"check": name, "state": state, "why": why})
    mode = platform_mode()
    if not is_on(h):
        c = check(h, cwd)
        if c["state"] == "stale-off":
            add("sandbox", FAIL, "sandbox mode is off but %d of gt's entries are still in "
                                 "settings.json (gt_sandbox.py remove)" % len(c["stale"]))
        else:
            add("sandbox", NC, "sandbox mode is off (the default): Claude's shell and file tools "
                               "can read and write the vault and gt's state. Turn it on with "
                               "gt_settings.py set sandbox_mode on (SECURITY.md)")
        return rows
    c = check(h, cwd)
    # PASS only where an OS boundary exists. Native Windows (and WSL1) get the permission rules
    # alone: NOT-CHECKED, with the word "friction", never a PASS that reads as a boundary.
    add("sandbox-mode", PASS if os_sandbox_supported(mode) else NC,
        "on (%s): %s" % (mode, ENFORCED[mode]))
    if c["state"] == "ok":
        add("sandbox-settings", PASS, "every entry gt needs is in ~/.claude/settings.json and "
                                      "nothing overrides it")
    else:
        why = c["problems"] + (["missing: " + "; ".join(c["missing"][:6])
                                + (" ..." if len(c["missing"]) > 6 else "")] if c["missing"] else [])
        why += (["stale: " + "; ".join(c["stale"][:4])] if c["stale"] else [])
        add("sandbox-settings", FAIL, " | ".join(why) + " (gt_sandbox.py apply)")
    if mode in ("linux", "wsl2"):
        miss = linux_deps_missing()
        add("sandbox-deps", FAIL if miss else PASS,
            ("missing %s: with failIfUnavailable Claude Code will not start" % ", ".join(miss))
            if miss else "bubblewrap and socat are on PATH")
    if os_sandbox_supported(mode):
        cs = status if status is not None else claude_status()
        if cs is None:
            add("claude-sandbox", NC, "`claude sandbox status` did not answer (Claude Code not "
                                      "on PATH, or older than 2.1.289)")
        elif cs.get("enabled") and cs.get("strictMode") and cs.get("supported", True):
            add("claude-sandbox", PASS, "Claude Code reports the sandbox enabled (source: %s), "
                                        "strict mode on, filesystem policy %s"
                % (cs.get("enabledSource"), cs.get("filesystemPolicy")))
        else:
            add("claude-sandbox", FAIL, "Claude Code reports enabled=%s strictMode=%s "
                                        "supported=%s%s" % (
                                            cs.get("enabled"), cs.get("strictMode"),
                                            cs.get("supported"),
                                            (" (%s)" % cs.get("unavailableReason"))
                                            if cs.get("unavailableReason") else ""))
    v = vault_path(h)
    if not live or not v:
        add("sandbox-live", NC, "no live probe (no vault, or not asked)")
    elif not os.environ.get("CLAUDECODE"):
        add("sandbox-live", NC, "not run from Claude's shell, so the fence the assistant meets "
                                "was not exercised (ask Claude to run gt_unlock.py verify)")
    elif not os_sandbox_supported(mode):
        wrote, _e = _probe_write(v)
        add("sandbox-live", NC, "Claude's shell %s the vault: expected on %s, which has no "
                                "sandbox (permission rules only)"
            % ("could write" if wrote else "could not write", mode))
    else:
        wrote, err = _probe_write(v)
        if wrote:
            add("sandbox-live", FAIL, "this shell is Claude's and it COULD write the vault: the "
                                      "sandbox is not in force (restart Claude Code after "
                                      "turning sandbox mode on; check /sandbox)")
        else:
            add("sandbox-live", PASS, "this shell is Claude's and writing the vault was refused "
                                      "by the OS (%s)" % err)
    return rows


# ------------------------------------------------------------------ CLI
def _print_report(rep, as_json):
    if as_json:
        print(json.dumps(rep, indent=2))
        return
    for c in rep.get("changes") or []:
        print("  " + c)
    for n in rep.get("notes") or []:
        print("  note: " + n)
    if rep.get("restart"):
        print("Restart Claude Code for the sandbox settings to take effect.")


def status_text(h=None):
    mode = platform_mode()
    lines = ["gt sandbox mode: %s   (platform: %s)" % ("ON" if is_on(h) else "off", mode),
             "  enforces: %s" % (ENFORCED[mode] if is_on(h) else "nothing -- off"),
             "  vault reads from Claude's shell/file tools: %s"
             % ("denied" if setting("sandbox_vault_reads", h) == "deny" else "allowed"),
             "  vault MCP tools (vault_list/read/search/queue_write): %s"
             % ("offered" if vault_mcp_on(h) else "not offered"),
             "  queue inbox (the one writable place): %s" % inbox_queue_dir(h)]
    c = check(h)
    lines.append("  settings: %s" % c["state"])
    for x in c["problems"] + c["missing"][:8] + c["stale"][:8]:
        lines.append("    - " + x)
    for n in c["notes"]:
        lines.append("  note: " + n)
    return "\n".join(lines)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in a
    force = "--force" in a
    rest = [x for x in a if x not in ("--json", "--force")]
    hh = None
    if "--home" in rest:
        i = rest.index("--home")
        hh = rest[i + 1] if i + 1 < len(rest) else None
        rest = rest[:i] + rest[i + 2:]
    cmd = rest[0] if rest else "status"
    try:
        if cmd == "status":
            if as_json:
                print(json.dumps({"on": is_on(hh), "mode": platform_mode(),
                                  "vault_reads": setting("sandbox_vault_reads", hh),
                                  "vault_mcp": vault_mcp_on(hh), "inbox": inbox_queue_dir(hh),
                                  "check": check(hh)}, indent=2))
            else:
                print(status_text(hh))
            return 0
        if cmd == "plan":
            p = plan(hh)
            if as_json:
                print(json.dumps(p, indent=2))
            else:
                print("platform %s; vault reads %s" % (p["mode"], p["vault_reads"]))
                for k, v in p["sandbox_bools"].items():
                    print("  sandbox.%s = %s" % (k, json.dumps(v)))
                for k in LISTS:
                    for e in p["lists"][k]:
                        print("  %s += %s" % (k, e))
                for n in p["notes"]:
                    print("  note: " + n)
            return 0
        if cmd == "apply":
            rep = apply(hh, force=force)
            _print_report(rep, as_json)
            return 0
        if cmd == "remove":
            rep = remove(hh)
            _print_report(rep, as_json)
            return 0
        if cmd == "check":
            c = check(hh)
            if as_json:
                print(json.dumps(c, indent=2))
            else:
                print("sandbox settings: %s" % c["state"])
                for x in c["problems"] + c["missing"] + c["stale"]:
                    print("  - " + x)
            return 0 if c["state"] in ("ok", "off") else 1
        if cmd == "verify":
            rows = verify_rows(hh)
            if as_json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print("  %-11s %-16s %s" % (r["state"], r["check"], r["why"]))
            return 1 if any(r["state"] == FAIL for r in rows) else 0
    except SandboxError as e:
        print("gt_sandbox: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("gt_sandbox: could not write (%s). Inside Claude Code's sandbox, ~/.claude is "
              "write-protected: run this from a terminal." % e, file=sys.stderr)
        return 1
    print("usage: gt_sandbox.py status|plan|apply [--force]|remove|check|verify [--json] "
          "[--home H]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
