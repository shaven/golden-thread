#!/usr/bin/env python3
"""gt_integrity -- keep gt's INSTALLED CODE from being changed behind your back (0.21.0, the
`paranoid` settings group; request 2026-10-08-installed-code-integrity).

    gt_integrity.py status [--json]                 what is locked, what is sealed, which factor
    gt_integrity.py verify [--json]                 re-hash everything against the record
    gt_integrity.py unlock --for-upgrade [--emit-token]   presence-gated: unlock for an upgrade
    gt_integrity.py lock [--from-install]           (re)seal and lock (the installer's last step)
    gt_integrity.py relock                          the same, after a presence check
    gt_integrity.py apply                           make the machine match the `paranoid` settings
    gt_integrity.py bash-guard --hook               PreToolUse(Bash): ask about writes to that code
    gt_integrity.py locked                          exit 0 when anything is locked or sealed

WHAT IT LOCKS. Code only -- never gt's working data. The roots are ~/.claude/golden-thread/hooks,
~/.claude/golden-thread/packs, ~/.claude/golden-thread/templates (0.22.0: the vault-tool hash list
and gt's own vault tools, which decide what the broker runs), the plugin cache and the marketplace
copies of golden-thread-plugin (and the integrity record itself). State, metrics, usage, lockdown, unlock, the io audit, the
expiry guard, model calibration and everything else under ~/.claude/golden-thread stay writable.

THE PARTS (gt_paranoid.py; every one is off unless `paranoid` is on or the part is set on):
  os_lock          macOS `chflags uchg` (user-immutable: nothing may change or remove the file until
                   the flag is cleared), Windows the read-only attribute plus a deny-write ACE for
                   the user, Linux `chmod a-w` (and `chattr +i` where it exists and the user is
                   root). Applied to every file AND every directory, after the Python bytecode is
                   compiled -- a locked directory silently refuses to store it, and every hook
                   would then recompile on each run.
  code_write_gate  the unlock step needs a person: with gt unlock set up, the gate `gt-code-write`
                   (a fresh Touch ID / Windows Hello / authenticator check); without it a typed
                   confirmation on a real terminal, whose caveat is printed every time. An
                   agent's shell has no terminal, so it cannot answer.
  bash_guard       a PreToolUse(Bash) hook that asks (with unlock on, needs presence) when a command
                   names that code together with a writing verb or a redirection, or runs
                   chflags/chmod/chattr/attrib/icacls on it. Best effort; the OS lock is the barrier.
  signed_manifest  integrity.json records every installed code file's SHA-256, signed by the unlock
                   key when unlock is enrolled, PINNED (hashed and stored) when it is not -- weaker,
                   and `status` says so. integrity-stamps.json holds each hook-dir file's
                   [sha, inode, size, mtime, ctime] taken AFTER the lock, so a hook's own check
                   (gt_integrity_gate.py) is a few stat() calls.
  plugin_dirs      guard_protected_paths.py asks about Write/Edit to the plugin cache and marketplace.

THE UPGRADE FLOW (install.sh). Unlock (one presence check) -> write -> compile -> seal -> lock ->
stamp. The presence check yields a short-lived token the installer holds in a shell variable (never
exported, never a file) and hands to `lock`, so one upgrade is one check -- two when the manifest is
signed (the signature is itself a Touch ID). Re-pinning an EXISTING record needs that token or a
fresh presence check; the very first pin is free, because nothing is yet protected that it could
bypass. `lock`/`relock` refuse to seal code that differs from the release manifest installed beside
it: a pin records what the release shipped, not whatever is on disk.

THE LOCK IS FRICTION FOR AN ASSISTANT, NOT A BARRIER AGAINST YOU: the owner of a file can always
clear uchg or chmod it back. Nothing here protects against an attacker with your full shell AND your
presence factors (no local scheme can); it protects against an assistant, or something it ran,
editing the files that constrain it.

Standard library only. Library functions take `home` explicitly so tests act on a throwaway home;
the CLI uses ~ . This module never touches a dev tree: it only ever acts on the INSTALLED copies
under <home>/.claude.
"""
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_tty                                                  # noqa: E402
import gt_paranoid as GP                                       # noqa: E402

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
SCHEMA = 1
NAME, STAMPS, TOKEN, LOG, SEALED = ("integrity.json", "integrity-stamps.json",
                                    "upgrade-token.json", "paranoid-log.jsonl", GP.SEALED)
CONFIRM = "I APPROVE CODE CHANGE"
GATE = "gt-code-write"
SCOPE = "gt:code:write"
TOKEN_TTL_S = 3600
ROUTE_UNLOCK = "python3 ~/.claude/golden-thread/hooks/gt_integrity.py unlock --for-upgrade"
ROUTE_INSTALL = "re-run install.sh (it unlocks, writes, seals and relocks in one step)"
JUNK = (".DS_Store", "Thumbs.db")


class IntegrityError(Exception):
    pass


# ----------------------------------------------------------------------------------- paths
def home_dir(home=None):
    return os.path.abspath(str(home) if home else os.path.expanduser("~"))


def P(home=None):
    h = home_dir(home)
    claude = os.path.join(h, ".claude")
    gt = os.path.join(claude, "golden-thread")
    return {"home": h, "claude": claude, "gt": gt,
            "hooks": os.path.join(gt, "hooks"), "packs": os.path.join(gt, "packs"),
            "templates": os.path.join(gt, "templates"),
            "cache": os.path.join(claude, "plugins", "cache", "golden-thread-plugin"),
            "market": os.path.join(claude, "plugins", "marketplaces", "golden-thread-plugin"),
            "integrity": os.path.join(gt, NAME), "stamps": os.path.join(gt, STAMPS),
            "sealed": os.path.join(gt, SEALED), "token": os.path.join(gt, TOKEN), "log": os.path.join(gt, LOG),
            "config": os.path.join(claude, "vault-config.json"),
            "manifest": os.path.join(gt, "MANIFEST.json")}


def code_roots(home=None):
    """The code roots that EXIST, in a fixed order. Never working data."""
    p = P(home)
    return [p[k] for k in ("hooks", "packs", "templates", "cache", "market") if os.path.isdir(p[k])]


def meta_files(home=None):
    p = P(home)
    return [x for x in (p["integrity"], p["stamps"], p["sealed"]) if os.path.lexists(x)]


def protected_roots(home=None, plugin_dirs=True):
    """Paths the Bash guard treats as protected (they need not exist yet)."""
    p = P(home)
    out = [p["hooks"], p["packs"], p["templates"], p["integrity"], p["stamps"]]
    if plugin_dirs:
        out += [p["cache"], p["market"]]
    return out


def config(home=None):
    return GP.read_config(P(home)["config"])


# ------------------------------------------------------------------------- the walk and the lock
def walk(root):
    """Yield (path, kind) for root and everything under it; kind is dir | file | link. Symlinks
    are never followed. Deterministic order (sorted), directories before their contents."""
    st = os.lstat(root)
    kind = "link" if stat.S_ISLNK(st.st_mode) else ("dir" if stat.S_ISDIR(st.st_mode) else "file")
    yield root, kind
    if kind != "dir":
        return
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for n in names:
        if n == ".git":
            # Claude Code's clone of a GitHub marketplace (the prompt install, 0.21.3) keeps a .git
            # whose pack files git makes read-only. They are not gt's code; walked, they read as
            # "locked" on Windows and Linux and an install with paranoid off stopped to ask for a
            # typed confirmation the prompt cannot give (a Windows test machine, 2026-10-13).
            continue
        yield from walk(os.path.join(root, n))


def chattr_applicable(euid=None, which=None):
    """chattr +i only works as root, and only where chattr exists (Linux)."""
    import shutil
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else -1
    which = which or shutil.which
    return euid == 0 and bool(which("chattr"))


def windows_commands(path, user, lock=True):
    """The commands that lock/unlock `path` on Windows: the read-only attribute and a deny-write
    ACE for `user` (write, delete, write-data, append-data), inherited by children. Built here and
    run by the Windows validation box (Phase 5): no Windows host exists in the unit tests."""
    if lock:
        return [["attrib", "+R", path + "\\*", "/S", "/D"], ["attrib", "+R", path],
                ["icacls", path, "/deny", "%s:(OI)(CI)(W,D,WD,AD)" % user, "/T", "/C"]]
    return [["icacls", path, "/remove:d", user, "/T", "/C"],
            ["attrib", "-R", path + "\\*", "/S", "/D"], ["attrib", "-R", path]]


def _entry_lock(path, kind, lock):
    """Set or clear the OS lock on ONE entry (macOS/Linux). -> None, or an error string."""
    try:
        if IS_MAC:
            fl = os.lstat(path).st_flags
            new = (fl | stat.UF_IMMUTABLE) if lock else (fl & ~stat.UF_IMMUTABLE)
            if new != fl:
                (os.lchflags if kind == "link" else os.chflags)(path, new)
        else:
            if kind == "link":
                return None
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            new = (mode & ~0o222) if lock else (mode | 0o200)
            if new != mode:
                os.chmod(path, new)
    except OSError as e:
        return "%s: %s" % (path, e.strerror or e)
    return None


def _chattr(path, lock):
    if chattr_applicable():
        subprocess.run(["chattr", "-R", "+i" if lock else "-i", path], capture_output=True)


def _windows_run(root, lock):
    import getpass
    for c in windows_commands(root, getpass.getuser(), lock):
        subprocess.run(c, capture_output=True, shell=False)


def lock_tree(root):
    """Lock one root: files and links first, then directories deepest first. -> (n, errors)."""
    if IS_WINDOWS:
        _windows_run(root, True)
        return sum(1 for _ in walk(root)), []
    entries = list(walk(root))
    errs, n = [], 0
    for path, kind in entries:
        if kind != "dir":
            e = _entry_lock(path, kind, True)
            errs += [e] if e else []
            n += 1
    for path, kind in reversed(entries):
        if kind == "dir":
            e = _entry_lock(path, kind, True)
            errs += [e] if e else []
            n += 1
    if not IS_MAC:
        _chattr(root, True)
    return n, errs


def unlock_tree(root):
    """Unlock one root: directories first (top down) so their contents can be changed, then files."""
    if IS_WINDOWS:
        _windows_run(root, False)
        return sum(1 for _ in walk(root)), []
    if not IS_MAC:
        _chattr(root, False)
    entries = list(walk(root))
    errs, n = [], 0
    for path, kind in entries:
        if kind == "dir":
            e = _entry_lock(path, kind, False)
            errs += [e] if e else []
            n += 1
    for path, kind in entries:
        if kind != "dir":
            e = _entry_lock(path, kind, False)
            errs += [e] if e else []
            n += 1
    return n, errs


def is_locked(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if IS_WINDOWS:
        return not os.access(path, os.W_OK)
    if IS_MAC:
        return bool(st.st_flags & stat.UF_IMMUTABLE)
    if stat.S_ISLNK(st.st_mode):
        return True                                   # a link has no mode of its own on Linux
    return not (stat.S_IMODE(st.st_mode) & 0o222)


def lock_all(home=None):
    """OS-lock every code root and the integrity record. Returns the number of entries. No
    presence, no sealing: the primitive. Used by the engine, and by tests that need the state."""
    n = 0
    for r in code_roots(home):
        c, _e = lock_tree(r)
        n += c
    for f in meta_files(home):
        _entry_lock(f, "file", True)
    return n


def unlock_all(home=None):
    """The primitive inverse, with NO presence check -- the engine and tests only; the CLI's
    `unlock` is the gated route."""
    n = 0
    for f in meta_files(home):
        _entry_lock(f, "file", False)
    for r in code_roots(home):
        c, _e = unlock_tree(r)
        n += c
    return n


def active(home=None):
    """Is anything locked, sealed or remembered as sealed here? (What `unlock` would act on.)"""
    p = P(home)
    return (os.path.exists(p["integrity"]) or os.path.exists(p["sealed"])
            or count_locked(home)[0] > 0)


def count_locked(home=None):
    """-> (files, dirs, total entries) currently locked, over the code roots."""
    files = dirs = total = 0
    for r in code_roots(home):
        for path, kind in walk(r):
            total += 1
            if is_locked(path):
                if kind == "dir":
                    dirs += 1
                else:
                    files += 1
    return files, dirs, total


# -------------------------------------------------------------------------------- hashing / record
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel_claude(path, home):
    return os.path.relpath(path, P(home)["claude"]).replace(os.sep, "/")


def file_map(home=None, include_pyc=True):
    """{path relative to ~/.claude: sha256 | 'link:<target>'} over every code root."""
    out = {}
    for r in code_roots(home):
        for path, kind in walk(r):
            if kind == "dir" or os.path.basename(path) in JUNK:
                continue
            if not include_pyc and path.endswith(".pyc"):
                continue
            out[_rel_claude(path, home)] = (("link:" + os.readlink(path)) if kind == "link"
                                            else sha256_file(path))
    return out


def digest_of(version, files, parts=None):
    """The digest the record is signed (or pinned) over: release, every file's hash and -- so a
    config edit cannot quietly relax what was sealed -- which paranoid parts were on."""
    body = {"schema": SCHEMA, "version": version, "files": files}
    if parts is not None:
        body["parts"] = parts
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Atomic, LF, 0644. The directory is the golden-thread home, which is never locked."""
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _installed_version(home=None):
    m = read_json(P(home)["manifest"]) or {}
    return str(m.get("version") or "")


def release_cross_check(home=None):
    """-> [problem] for every hooks-dir file that differs from the release MANIFEST installed
    beside it. A pin must record what the release shipped, not whatever is on disk."""
    p = P(home)
    man = read_json(p["manifest"])
    if not man or not isinstance(man.get("files"), dict):
        return ["no release manifest is installed (golden-thread/MANIFEST.json); cannot check "
                "what is being sealed against the release manifest"]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gt_components_for_integrity",
                                                      os.path.join(HERE, "gt_components.py"))
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)
        hookdir = set(gc.HOOK_DIR_SCRIPTS)
    except Exception as e:                                  # noqa: BLE001
        return ["gt_components.py could not be loaded (%s); cannot map the release manifest"
                % type(e).__name__]
    problems = []
    for rel, rec in sorted(man["files"].items()):
        bits = rel.split("/")
        if len(bits) == 2 and (bits[0] == "hooks" or (bits[0] == "scripts" and bits[1] in hookdir)):
            dst = os.path.join(p["hooks"], bits[1])
            try:
                got = sha256_file(dst)
            except OSError:
                problems.append("%s: missing (the release manifest lists it)" % bits[1])
                continue
            if got != rec.get("sha256"):
                problems.append("%s: differs from the release manifest" % bits[1])
    return problems


# --------------------------------------------------------------------- dependency list for the hooks
_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
_SH_REF = re.compile(r"[A-Za-z0-9_.-]+\.(?:py|sh)")


def _scan_hooks(hooks_dir):
    """-> (names, direct, pycs): the .py / .sh files in the hooks dir, the files each one imports or
    mentions DIRECTLY, and each module's bytecode files. Static: `import X` / `from X import` where X
    is a file beside it, and for shell wrappers every *.py / *.sh name they mention."""
    names = {n for n in os.listdir(hooks_dir) if n.endswith((".py", ".sh"))}
    direct = {}
    for n in names:
        try:
            with open(os.path.join(hooks_dir, n), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if n.endswith(".py"):
            found = {m + ".py" for m in _IMPORT.findall(text) if m + ".py" in names}
        else:
            found = {m for m in _SH_REF.findall(text) if m in names}
        found.discard(n)
        direct[n] = found
    pyc_dir = os.path.join(hooks_dir, "__pycache__")
    pycs = {}
    if os.path.isdir(pyc_dir):
        for f in os.listdir(pyc_dir):
            if f.endswith(".pyc") and "." in f:
                pycs.setdefault(f.split(".")[0] + ".py", []).append("hooks/__pycache__/" + f)
    return names, direct, pycs


def hook_deps(hooks_dir):
    """{hooks/<file>: [files it imports or sources, transitively, plus their bytecode]} for every
    .py / .sh in the hooks dir (see _scan_hooks). The stamps file stores hook_graph instead, because
    this transitive form grows with the square of the module count (0.22.0: 332 KB parsed by every
    hook); gt_integrity_gate walks the graph and gets the same list."""
    names, direct, pycs = _scan_hooks(hooks_dir)
    out = {}
    for n in names:
        seen, todo = set(), [n]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(direct.get(cur, ()))
        deps = sorted("hooks/" + d for d in seen if d != n)
        deps += sorted(x for d in seen for x in pycs.get(d, []))
        out["hooks/" + n] = deps
    return out


def hook_graph(hooks_dir):
    """{"edges": {hooks/<file>: [files it imports or sources DIRECTLY]}, "pycs": {hooks/<file>: [its
    bytecode files]}}, empty lists left out. One entry per file however many modules it reaches, so the
    sealed record grows linearly; the gate takes the transitive closure of the entry it is checking."""
    names, direct, pycs = _scan_hooks(hooks_dir)
    edges = {"hooks/" + n: sorted("hooks/" + d for d in direct[n]) for n in sorted(direct) if direct[n]}
    return {"edges": edges, "pycs": {"hooks/" + n: sorted(v) for n, v in sorted(pycs.items()) if n in names}}


def write_stamps(home, record):
    """integrity-stamps.json: [sha, inode, size, mtime_ns, ctime_ns] of every hooks-dir file,
    taken NOW (after the OS lock moved every ctime), plus the import graph the hooks use."""
    p = P(home)
    files = {}
    for rel, sha in record["files"].items():
        if not rel.startswith("golden-thread/hooks/"):
            continue
        st = os.lstat(os.path.join(p["claude"], rel))
        files[rel[len("golden-thread/"):]] = [sha, st.st_ino, st.st_size, st.st_mtime_ns,
                                              st.st_ctime_ns]
    write_json(p["stamps"], {"schema": SCHEMA, "digest": record["digest"], "files": files,
                             "graph": hook_graph(p["hooks"])})


# ------------------------------------------------------------------------------------ presence
def unlock_enabled(home=None):
    try:
        import gt_unlock_policy
        return bool(gt_unlock_policy.enabled_at(gt_unlock_policy.unlock_home(home_dir(home))))
    except Exception:                                       # noqa: BLE001
        return False


def typed_confirmation(reason):
    """The fallback when gt unlock is not set up: type a phrase at a real terminal. -> (ok, message).

    CAVEAT, printed every time: this proves a terminal is open and someone is typing; it does not
    prove the someone is you. An agent's shell has no controlling terminal and cannot answer;
    a process that can open one could. GT_INTEGRITY_NO_TTY=1 (the test harness) refuses to open it."""
    if os.environ.get("GT_INTEGRITY_NO_TTY") == "1":
        return False, "no terminal is available to this process (GT_INTEGRITY_NO_TTY is set)"
    text = ("\ngt paranoid: %s\n"
            "This changes the protection of gt's installed code (hooks, packs, templates, plugin\n"
            "cache, marketplace).\n"
            "Caveat: gt unlock is not set up, so this is a typed confirmation, not Touch ID. It\n"
            "proves a terminal is open and someone is typing; it does not prove the someone is\n"
            "you. Set up gt unlock (gt_unlock.py) for a fingerprint check instead.\n"
            "Type %s to continue (anything else cancels): " % (reason, CONFIRM))
    try:
        if IS_WINDOWS:
            if not gt_tty.has_terminal(sys.stdin):
                return False, "this needs a terminal; this process has none"
            sys.stderr.write(text)
            sys.stderr.flush()
            answer = sys.stdin.readline()
        else:
            import select
            fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.write(fd, text.encode("utf-8"))
                buf, deadline = b"", time.monotonic() + 180
                while b"\n" not in buf and time.monotonic() < deadline:
                    r, _w, _x = select.select([fd], [], [], 1.0)
                    if r:
                        chunk = os.read(fd, 256)
                        if not chunk:
                            break
                        buf += chunk
                answer = buf.decode("utf-8", "replace")
            finally:
                os.close(fd)
    except OSError:
        return False, "this needs a terminal; this process has none"
    if answer.strip() == CONFIRM:
        return True, ""
    return False, "the confirmation was not given"


def require_presence(reason, home=None, cfg=None, force=False):
    """A person must be there. -> (ok, how, message).

    paranoid.code_write_gate off -> no check (the owner turned that part off; it is logged and
    shown as partial) -- except `force`, which a change to a paranoid SETTING always passes. Unlock on -> the gate scope `gt:code:write` through the authority (Touch ID,
    Windows Hello, authenticator app; a fresh confirmation, FAIL CLOSED if the authority cannot be
    asked). Unlock off -> a typed confirmation at a real terminal, with its caveat."""
    cfg = config(home) if cfg is None else cfg
    # the live config can ADD to what the sealed record demands but never take it away: the record
    # remembers which parts were on, so editing vault-config.json by hand does not turn this off
    if not force and not GP.enforced("paranoid.code_write_gate", cfg, home_dir(home)):
        return True, "none (paranoid.code_write_gate is off)", ""
    if unlock_enabled(home):
        try:
            import gt_unlock_client
            ans = gt_unlock_client.tty_answer if gt_tty.has_terminal(sys.stdin) else None
            v = gt_unlock_client.check(SCOPE, request=True, reason=reason, answer=ans)
        except Exception as e:                              # noqa: BLE001
            return False, "unlock", ("gt unlock is on and the authority could not be asked (%s)"
                                     % type(e).__name__)
        if v.get("allowed"):
            return True, "gt unlock", ""
        return False, "gt unlock", "%s (%s)" % (v.get("message"), v.get("code"))
    ok, msg = typed_confirmation(reason)
    return ok, "typed confirmation (weaker than Touch ID: gt unlock is not set up)", msg


def refusal(msg):
    return ("refused: %s\n  Route: %s   (from a terminal; an assistant's shell has none)\n"
            "  or %s." % (msg, ROUTE_UNLOCK, ROUTE_INSTALL))


# ------------------------------------------------------------------------------- signing (mode)
def unlock_record(home=None):
    """The enrolled platform factor's public record, if any: (name, record)."""
    import gt_unlock_policy
    d = read_json(os.path.join(gt_unlock_policy.unlock_home(home_dir(home)), "enrolment.json")) or {}
    for name in ("touchid", "hello"):
        rec = (d.get("factors") or {}).get(name)
        if isinstance(rec, dict):
            return name, rec
    return None, None


def challenge(nonce, requester, factor, digest):
    """What the authority asks the factor to sign for a code manifest -- the same derivation as
    gt_unlockd.Authority._challenge, with purpose `code-sign` and scope `code:<digest>`."""
    blob = json.dumps({"v": 1, "purpose": "code-sign", "scope": "code:" + digest,
                       "requester": requester, "factor": factor}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(b"gt-unlock-challenge\0" + nonce + b"\0" + blob).digest()


def sign_digest(digest, home=None):
    """Ask the unlock authority for a platform-factor signature over this manifest digest.
    -> a signature record, or None when signing is not possible here (unlock off, no Touch ID /
    Hello enrolled): the caller then PINS instead. Raises IntegrityError when signing was possible
    but refused (a refused signature is not a reason to quietly weaken the record)."""
    if not unlock_enabled(home):
        return None
    name, _rec = unlock_record(home)
    if name is None:
        return None
    try:
        import gt_ipc
        import gt_unlock_client
        res = gt_unlock_client.call("sign_code", {"digest": digest})
    except gt_ipc.IpcError as e:
        raise IntegrityError("gt unlock did not sign the manifest: %s" % e.message)
    except Exception as e:                                  # noqa: BLE001
        raise IntegrityError("could not ask gt unlock to sign the manifest (%s)" % type(e).__name__)
    if not isinstance(res, dict) or "sig" not in res:
        raise IntegrityError("gt unlock did not sign the manifest: no signature came back")
    return {"factor": res["factor"], "nonce": res["nonce"], "sig": res["sig"],
            "requester": res["requester"]}


def verify_signature(record, home=None):
    """-> (ok, message) for a signed record, under the CURRENTLY ENROLLED platform key."""
    import base64
    sig = record.get("signature")
    if not isinstance(sig, dict):
        return False, "the record says signed but carries no signature"
    name, rec = unlock_record(home)
    if name is None or name != sig.get("factor"):
        return False, ("the record was signed with %s, which is not enrolled now (unlock was "
                       "reset?): re-seal with gt_integrity.py relock" % sig.get("factor"))
    try:
        nonce = base64.b64decode(sig["nonce"], validate=True)
        raw = base64.b64decode(sig["sig"], validate=True)
        ch = challenge(nonce, str(sig.get("requester") or ""), name, record["digest"])
        if name == "touchid":
            import gt_unlock_touchid as T
            ok = T.TouchIdFactor().verify(rec, ch, raw)
        else:
            import gt_unlock_hello as H
            ok = H.HelloFactor().verify(rec, ch, raw)
    except Exception as e:                                  # noqa: BLE001
        return False, "the signature could not be checked (%s)" % type(e).__name__
    return (True, "") if ok else (False, "the signature does not verify under the enrolled key")


# --------------------------------------------------------------------------------- seal and engine
def precompile(home=None):
    """Compile every .py under the code roots with the interpreter running this AND the one the
    hooks use (the recorded interpreter, when different), BEFORE the lock. -> (n_interpreters, notes)."""
    p = P(home)
    interps = [sys.executable]
    try:
        with open(os.path.join(p["gt"], "python"), encoding="utf-8") as fh:
            rec = fh.readline().strip()
        if rec and os.path.isabs(rec) and os.access(rec, os.X_OK) and \
                os.path.realpath(rec) != os.path.realpath(sys.executable):
            interps.append(rec)
    except OSError:
        pass
    notes = []
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    for py in interps:
        for r in code_roots(home):
            c = subprocess.run([py, "-m", "compileall", "-q", r], capture_output=True, text=True,
                               env=env)
            if c.returncode != 0:
                first = (c.stdout + c.stderr).strip().splitlines()[:1]
                notes.append("%s: some files did not compile (%s)" % (os.path.basename(r), first))
    return len(interps), notes


def remove_bytecode(home=None):
    import shutil
    n = 0
    for r in code_roots(home):
        for dirpath, dirs, _files in os.walk(r):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirs.remove(d)
                    n += 1
    return n


def seal(home=None, signer=None, cross_check=True):
    """Write integrity.json for what is on disk NOW. Refuses code that differs from the release
    manifest. -> the record. Signed when `signer(digest)` returns a signature record, else pinned;
    signer=False never signs."""
    p = P(home)
    if cross_check:
        bad = release_cross_check(home)
        if bad:
            raise IntegrityError("refusing to seal: installed code differs from the release "
                                 "manifest:\n  " + "\n  ".join(bad[:8]))
    cfg = config(home)
    files = file_map(home, include_pyc=GP.effective("paranoid.os_lock", cfg))
    version = _installed_version(home)
    sealed_parts = {k: GP.effective(k, cfg) for k in GP.KEYS}
    digest = digest_of(version, files, sealed_parts)
    rec = {"schema": SCHEMA, "version": version, "created": int(time.time()), "digest": digest,
           "files": files, "parts": sealed_parts}
    if signer is False:
        sig = None
    else:
        sig = signer(digest) if signer else sign_digest(digest, home)
    if sig:
        rec["mode"], rec["signature"] = "signed", sig
    else:
        rec["mode"], rec["signature"] = "pinned", None
        rec["pin"] = {"manifest_sha256": sha256_file(p["manifest"]) if os.path.isfile(p["manifest"])
                      else None, "digest": digest}
    write_json(p["integrity"], rec)
    return rec


def _drop(*paths):
    for f in paths:
        try:
            os.unlink(f)
        except OSError:
            pass


def converge(home=None, signer=None, cross_check=True):
    """Make the machine match the `paranoid` settings. NO presence check here: callers decide.
    -> (messages, errors). Idempotent.

      want os_lock or signed_manifest:
        unlock everything -> (compile, if os_lock) -> seal (if signed_manifest, else drop the
        record) -> lock (if os_lock) -> stamp -> lock the record
      want neither: unlock everything, drop the record, stamps, token and bytecode."""
    msgs, errs = [], []
    p = P(home)
    if not os.path.isdir(p["hooks"]):
        return ["nothing installed under %s: the settings take effect at the next install"
                % os.path.relpath(p["gt"], p["home"]).replace(os.sep, "/")], []
    cfg = config(home)
    want_lock = GP.effective("paranoid.os_lock", cfg)
    want_seal = GP.effective("paranoid.signed_manifest", cfg)
    unlock_all(home)
    if not want_lock and not want_seal:
        _drop(p["integrity"], p["stamps"], p["token"], p["sealed"])
        n = remove_bytecode(home)
        msgs.append("unlocked; no record kept%s" % ("; compiled bytecode removed" if n else ""))
        return msgs, errs
    write_json(p["sealed"], {"schema": SCHEMA, "created": int(time.time()),
                             "parts": {k: GP.effective(k, cfg) for k in GP.KEYS}})
    if want_lock:
        _n, notes = precompile(home)
        msgs += notes
    else:
        remove_bytecode(home)
    rec = None
    if want_seal:
        try:
            rec = seal(home, signer=signer, cross_check=cross_check)
        except IntegrityError as e:
            errs.append(str(e))
            return msgs, errs
    else:
        _drop(p["integrity"], p["stamps"])
    if want_lock:
        total = 0
        for r in code_roots(home):
            c, e = lock_tree(r)
            total += c
            errs += e
        msgs.append("locked %d entries under %d roots" % (total, len(code_roots(home))))
    if rec:
        write_stamps(home, rec)
        msgs.append("sealed %d files (%s)" % (len(rec["files"]), rec["mode"] if rec["mode"] ==
                                              "signed" else "pinned: not signed, weaker"))
        if want_lock:
            for f in meta_files(home):
                _entry_lock(f, "file", True)
    return msgs, errs


def lock_trusted(home=None):
    """converge() for callers that ARE the trusted path (tests restoring a fixture): no presence,
    no signing (pinned), cross-check on."""
    msgs, errs = converge(home, signer=False)
    if errs:
        raise IntegrityError("; ".join(errs))
    return msgs


# ---------------------------------------------------------------------------------------- tokens
def make_token(home=None):
    tok = secrets.token_hex(16)
    write_json(P(home)["token"], {"sha256": hashlib.sha256(tok.encode()).hexdigest(),
                                  "expires": int(time.time()) + TOKEN_TTL_S})
    return tok


def token_ok(tok, home=None):
    d = read_json(P(home)["token"]) or {}
    if not tok or not d:
        return False
    return (hashlib.sha256(tok.encode()).hexdigest() == d.get("sha256")
            and int(d.get("expires") or 0) > time.time())


def drop_token(home=None):
    _drop(P(home)["token"])


# ---------------------------------------------------------------------------------- log and state
def log_change(home, **row):
    row = dict(row, ts=int(time.time()))
    try:
        os.makedirs(P(home)["gt"], exist_ok=True)
        with open(P(home)["log"], "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def gate_apply(home=None):
    """Wire (or unwire) the presence gate `gt-code-write` in the unlock policy to match the
    `code_write_gate` part. Best effort: unlock off writes the policy file directly. -> a message."""
    want = GP.effective("paranoid.code_write_gate", config(home))
    cli = os.path.join(HERE, "gt_unlock.py")
    if not os.path.isfile(cli):
        return ""
    env = dict(os.environ, HOME=home_dir(home), USERPROFILE=home_dir(home))
    r = subprocess.run([sys.executable, cli, "gate", "on" if want else "off", GATE],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        last = (r.stdout + r.stderr).strip().splitlines()[-1:]
        return "gate %s: could not %s it (%s)" % (GATE, "wire" if want else "unwire",
                                                  last[0] if last else r.returncode)
    return ""


def apply(home=None, signer=None):
    """Bring the machine in line with the `paranoid` settings (called by gt_settings after a change
    and by the installer). The caller has already had the presence it needed. -> (ok, [lines])."""
    lines = []
    g = gate_apply(home)
    if g:
        lines.append(g)
    msgs, errs = converge(home, signer=signer)
    lines += msgs
    lines += ["error: " + e for e in errs]
    return (not errs), lines


def state(home=None):
    """Everything `status` reports, as a dict."""
    p = P(home)
    cfg = config(home)
    st = GP.state(cfg)
    rec = read_json(p["integrity"])
    files, dirs, total = count_locked(home)
    mode = rec.get("mode") if rec else None
    if mode == "signed":
        factor = "signed by the unlock key (%s)" % ((rec.get("signature") or {}).get("factor"))
    elif mode == "pinned":
        factor = ("pinned hash, no signature (gt unlock is not set up): weaker -- anything that can "
                  "unlock and rewrite integrity.json can bless a change")
    else:
        factor = "none (nothing sealed)"
    return {"label": st["label"], "master": st["master"],
            "parts": {k: {"on": GP.effective(k, cfg), "raw": GP.raw(k, cfg)} for k in GP.KEYS},
            "off": st["off"], "on": st["on"], "installed": os.path.isdir(p["hooks"]),
            "mode": mode, "weaker": mode == "pinned", "factor": factor,
            "sealed_files": len((rec or {}).get("files") or {}), "version": (rec or {}).get("version"),
            "locked": {"files": files, "dirs": dirs, "entries": total, "roots": code_roots(home)},
            "platform": ("macOS chflags uchg" if IS_MAC else "Windows read-only + deny ACE"
                         if IS_WINDOWS else "Linux chmod a-w"),
            "unlock_enrolled": unlock_enabled(home)}


def render_status(s):
    out = ["gt paranoid: %s" % s["label"]]
    if s["label"] == "paranoid (partial)" and s["off"]:
        out.append("  parts that are off: " + ", ".join(k.split(".", 1)[1] for k in s["off"]))
    for k, _num, _title in GP.PARTS:
        v = s["parts"][k]
        out.append("  %-26s %-3s %s" % (k, "on" if v["on"] else "off",
                                       "(set %s)" % v["raw"] if v["raw"] != "follow"
                                       else "(follows paranoid)"))
    out.append("  platform lock: %s%s" % (s["platform"], "" if s["installed"] else
                                         "   (gt is not installed under this home)"))
    out.append("  locked: %d files and %d directories of %d entries, under %d roots"
               % (s["locked"]["files"], s["locked"]["dirs"], s["locked"]["entries"],
                  len(s["locked"]["roots"])))
    for r in s["locked"]["roots"]:
        out.append("    " + r)
    out.append("  manifest: %s" % (s["factor"] if s["mode"] else "none sealed"))
    if s["mode"]:
        out.append("  sealed: %d files, release %s" % (s["sealed_files"], s["version"] or "?"))
    if s["weaker"]:
        out.append("  NOTE: this is the weaker, pinned form. Set up gt unlock (Touch ID / Windows "
                   "Hello) and run `gt_integrity.py relock` to sign it.")
    return "\n".join(out)


def verify(home=None, check_signature=True):
    """Re-hash everything against the record. -> {ok, problems:[str], checked, mode}."""
    p = P(home)
    cfg = config(home)
    problems = []
    rec = read_json(p["integrity"])
    # what is enforced: the live config, plus whatever the sealed record remembers was on
    want_lock = GP.enforced("paranoid.os_lock", cfg, p["home"])
    want_seal = GP.enforced("paranoid.signed_manifest", cfg, p["home"])
    checked = 0
    if rec is None:
        if want_seal and os.path.isdir(p["hooks"]):
            problems.append("integrity.json: missing (paranoid.signed_manifest is on, so a "
                            "sealed record must exist)")
    else:
        files = rec.get("files") or {}
        if digest_of(rec.get("version"), files, rec.get("parts")) != rec.get("digest"):
            problems.append("integrity.json: edited (its own digest does not match its contents)")

        if rec.get("mode") == "signed" and check_signature:
            ok, why = verify_signature(rec, home)
            if not ok:
                problems.append("integrity.json: " + why)
        seen = set()
        for rel, sha in sorted(files.items()):
            path = os.path.join(p["claude"], rel)
            seen.add(rel)
            checked += 1
            try:
                got = ("link:" + os.readlink(path)) if os.path.islink(path) else sha256_file(path)
            except OSError:
                problems.append("%s: missing" % rel)
                continue
            if got != sha:
                problems.append("%s: changed since it was sealed" % rel)
        for r in code_roots(home):
            for path, kind in walk(r):
                if kind == "dir" or os.path.basename(path) in JUNK:
                    continue
                if path.endswith(".pyc") and not want_lock:
                    continue
                rel = _rel_claude(path, home)
                if rel not in seen:
                    problems.append("%s: not in the sealed manifest (added since)" % rel)
    for k, was_on in sorted(GP.sealed_parts(p["home"]).items()):
        if was_on and k in GP.KEYS and k != "paranoid.require_unlock" and not GP.effective(k, cfg):
            problems.append("%s was on when the code was last locked or sealed and is off in "
                            "vault-config.json now: changed outside `gt_settings.py set` (which "
                            "asks a person)? Put it back, or turn it off properly." % k)
    if want_lock and os.path.isdir(p["hooks"]):
        unl, example = 0, None
        for r in code_roots(home):
            for path, _kind in walk(r):
                if not is_locked(path):
                    unl += 1
                    example = example or path
        if unl:
            problems.append("%d entries are not locked, e.g. %s (paranoid.os_lock is on)"
                            % (unl, _rel_claude(example, home)))
    return {"ok": not problems, "problems": problems, "checked": checked,
            "mode": (rec or {}).get("mode")}


# -------------------------------------------------------------------------------------- Bash guard
_WRAP = {"sudo", "env", "command", "nohup", "time", "exec", "builtin", "nice", "doas", "stdbuf"}
_ANY_ARG = {"rm", "rmdir", "unlink", "shred", "truncate", "mv", "chmod", "chown", "chgrp",
            "chflags", "chattr", "attrib", "icacls", "setfacl", "xattr", "touch", "mkdir",
            "takeown", "cacls"}
_FLAG_VERBS = {"chflags", "chmod", "chattr", "attrib", "icacls", "chown", "chgrp", "setfacl",
               "xattr", "takeown", "cacls"}
_DEST_LAST = {"cp", "install", "rsync", "ln", "scp", "ditto"}
_INLINE = {"python", "python3", "perl", "ruby", "node", "php", "osascript", "pwsh", "powershell"}
_INLINE_FLAGS = ("-c", "-e", "-E", "--eval", "--command", "-Command")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_RECURSIVE = re.compile(r"^-[A-Za-z]*[rR][A-Za-z]*$")


def _expand(word, home):
    w = word
    if w == "~" or w.startswith("~/"):
        w = home + w[1:]
    return w.replace("${HOME}", home).replace("$HOME", home)


_MSYS_DRIVE = re.compile(r"(?<![A-Za-z0-9_.~$])/([A-Za-z])(?=/|$|[\s'\"])")


def _msys_to_win(text):
    """Git Bash spells C:\\Users as /c/Users, and Claude Code runs Bash through it on Windows, so a
    command can name a protected file as /c/Users/x/.claude/.... Rewrite the drive prefix to C: so
    the guard sees one path whichever way it was spelled (0.21.4: /c/... slipped past every rule)."""
    if not IS_WINDOWS:
        return text
    return _MSYS_DRIVE.sub(lambda m: m.group(1).upper() + ":", text)


def _norm(path, cwd, home):
    w = _msys_to_win(_expand(path, home))
    if "\\" in w:
        w = w.replace("\\", "/")                  # attrib / icacls are given Windows-style paths
    if not os.path.isabs(w):
        w = os.path.join(cwd, w)
    return os.path.normpath(w)


def _cf(s):
    return s.casefold() if (IS_MAC or IS_WINDOWS) else s


def _inside(path, root):
    a, b = _cf(path), _cf(os.path.normpath(root))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _hits(path, roots):
    """-> (root, is_ancestor): the root `path` is inside (False), or that it is an ancestor of."""
    try:
        rp = os.path.realpath(path)
    except OSError:
        rp = path
    for r in roots:
        for cand in (path, rp):
            if _inside(cand, r):
                return r, False
    for r in roots:
        a, b = _cf(path), _cf(os.path.normpath(r))
        if b.startswith(a.rstrip(os.sep) + os.sep):
            return r, True
    return None, False


def _lex(cmd):
    """Split a command line into simple commands [(words, redirect targets)] plus the bodies of any
    command substitution (analysed on their own). Quotes are removed from words; operators are
    recognised outside quotes only."""
    cmds, subs = [], []
    words, redirs, cur, has = [], [], [], False
    state_ = {"redir": False}
    i, n = 0, len(cmd)

    def end_word():
        nonlocal cur, has
        if has:
            w = "".join(cur)
            if state_["redir"]:
                redirs.append(w)
                state_["redir"] = False
            else:
                words.append(w)
        cur, has = [], False

    def end_cmd():
        nonlocal words, redirs
        end_word()
        if words or redirs:
            cmds.append((words, redirs))
        words, redirs = [], []

    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            # bash drops the backslash; keeping it before an ordinary character errs toward
            # detection, and is what an attrib / icacls path (hooks\gt_paths.py) needs
            cur.append(cmd[i + 1] if cmd[i + 1] in " \t\n'\"\\$`;&|<>()" else "\\" + cmd[i + 1])
            has = True; i += 2; continue
        if c == "'":
            j = cmd.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(cmd[i + 1:j]); has = True; i = j + 1; continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf.append(cmd[j + 1] if cmd[j + 1] in '"\\$`\n' else "\\" + cmd[j + 1])
                    j += 2; continue
                if cmd[j:j + 2] == "$(":
                    depth, k = 1, j + 2
                    while k < n and depth:
                        depth += (cmd[k] == "(") - (cmd[k] == ")")
                        k += 1
                    subs.append(cmd[j + 2:k - 1])
                buf.append(cmd[j]); j += 1
            cur.append("".join(buf)); has = True; i = j + 1; continue
        if c == "$" and cmd[i:i + 2] == "${":
            j = cmd.find("}", i + 2)
            j = n - 1 if j < 0 else j
            cur.append(cmd[i:j + 1]); has = True; i = j + 1; continue
        if c == "$" and cmd[i:i + 2] == "$(":
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (cmd[j] == "(") - (cmd[j] == ")")
                j += 1
            subs.append(cmd[i + 2:j - 1])
            cur.append(cmd[i:j]); has = True; i = j; continue
        if c == "`":
            j = cmd.find("`", i + 1)
            j = n if j < 0 else j
            subs.append(cmd[i + 1:j])
            cur.append(cmd[i:j + 1]); has = True; i = j + 1; continue
        if c in "\n;(){}":
            end_cmd(); i += 1; continue
        if c in "&|":
            if cmd[i:i + 2] == "&>":
                end_word(); state_["redir"] = True
                i += 3 if cmd[i:i + 3] == "&>>" else 2
                continue
            end_cmd(); i += 2 if cmd[i:i + 2] in ("&&", "||") else 1; continue
        if c == ">":
            if has and "".join(cur).isdigit():          # an fd prefix (2>, 1>>) is part of the operator
                cur, has = [], False
            else:
                end_word()
            j = i + 1
            if cmd[j:j + 1] == ">":
                j += 1
            elif cmd[j:j + 1] == "|":
                j += 1
            if cmd[j:j + 1] == "&":                      # >&2 / >&- : a duplication, no file
                k = j + 1
                while k < n and (cmd[k].isdigit() or cmd[k] == "-"):
                    k += 1
                i = k; continue
            state_["redir"] = True
            i = j; continue
        if c == "<":
            end_word()
            j = i + 1
            while j < n and cmd[j] in "<-":
                j += 1
            while j < n and cmd[j] == " ":
                j += 1
            k = j
            while k < n and cmd[k] not in " \t\n;&|)":
                k += 1
            i = k; continue
        if c in " \t":
            end_word(); i += 1; continue
        cur.append(c); has = True; i += 1
    end_cmd()
    return cmds, subs


def _show(path, home):
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def bash_write_reason(command, cwd, home, roots, _depth=0):
    """-> a one-line reason if `command` writes to, or changes the flags of, one of `roots`
    (None when it does not). Best effort and conservative toward 'not a write': reading, listing,
    running a script, copying FROM a protected path, and a path inside a quoted string are all
    fine. The OS lock is the barrier; this makes the attempt visible."""
    if not isinstance(command, str) or not command.strip() or _depth > 3:
        return None
    try:
        cmds, subs = _lex(command)
    except Exception:                                        # noqa: BLE001
        return None
    for body in subs:
        why = bash_write_reason(body, cwd, home, roots, _depth + 1)
        if why:
            return why
    cwd = os.path.normpath(cwd or "/")

    def prot(word, where):
        """-> the normalised path if `word` is a path inside a protected root, else None."""
        if not word or "://" in word or (word.startswith("-") and "=" not in word):
            return None
        w = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
        path = _norm(w, where, home)
        r, anc = _hits(path, roots)
        return path if (r and not anc) else None

    def reaches(word, where, recursive):
        """Protected, or (for a recursive verb) an ancestor of a protected root."""
        if not word or "://" in word or word.startswith("-"):
            return None
        path = _norm(word, where, home)
        r, anc = _hits(path, roots)
        return path if r and (not anc or recursive) else None

    for words, redirs in cmds:
        for t in redirs:
            path = prot(t, cwd)
            if path:
                return "redirects output into %s" % _show(path, home)
        w = list(words)
        while w and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]) or os.path.basename(w[0]) in _WRAP):
            wrapped = os.path.basename(w[0]) in _WRAP
            w.pop(0)
            while wrapped and w and w[0].startswith("-"):
                w.pop(0)
        if not w:
            continue
        verb, args = os.path.basename(w[0]), w[1:]
        recursive = any(_RECURSIVE.match(a) or a == "--recursive" or a.lower() in ("/s", "/t")
                        for a in args)
        if verb == "cd":
            if args:
                cwd = _norm(args[-1], cwd, home)
            continue
        if verb in _SHELLS and "-c" in args:
            k = args.index("-c")
            if k + 1 < len(args):
                why = bash_write_reason(args[k + 1], cwd, home, roots, _depth + 1)
                if why:
                    return why
            continue
        if verb == "eval" and args:
            why = bash_write_reason(" ".join(args), cwd, home, roots, _depth + 1)
            if why:
                return why
            continue
        if re.sub(r"[0-9.]+$", "", verb) in _INLINE and any(a in _INLINE_FLAGS for a in args):
            blob = _cf(_msys_to_win(_expand(" ".join(args), home)).replace("~/", home + "/").replace("\\", "/"))
            for r in roots:
                if _cf(os.path.normpath(r).replace("\\", "/")) in blob:
                    return "%s runs inline code that names %s" % (verb, _show(r, home))
            continue
        if verb == "tee":
            for a in args:
                path = prot(a, cwd)
                if path:
                    return "tee writes to %s" % _show(path, home)
        elif verb in ("sed", "gsed", "perl", "ruby") and any(
                a == "--in-place" or a.startswith("--in-place=")
                or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]
                    and not re.match(r"^-[A-Za-z]*e", a)) for a in args):
            for a in args:
                path = prot(a, cwd)
                if path:
                    return "%s -i edits %s in place" % (verb, _show(path, home))
        elif verb in _ANY_ARG:
            for a in args:
                path = reaches(a, cwd, recursive)
                if path:
                    what = ("changes the flags or permissions of" if verb in _FLAG_VERBS
                            else "removes, moves or changes")
                    return "%s %s %s" % (verb, what, _show(path, home))
        elif verb in _DEST_LAST:
            plain = [a for a in args if not a.startswith("-")]
            tdir = None
            for k, a in enumerate(args):
                if a in ("-t", "--target-directory") and k + 1 < len(args):
                    tdir = args[k + 1]
                elif a.startswith("--target-directory="):
                    tdir = a.split("=", 1)[1]
            dest = tdir if tdir else (plain[-1] if plain else None)
            path = prot(dest, cwd) if dest is not None else None
            if path:
                return "%s writes into %s" % (verb, _show(path, home))
        elif verb == "dd":
            for a in args:
                path = prot(a[3:], cwd) if a.startswith("of=") else None
                if path:
                    return "dd writes to %s" % _show(path, home)
        elif verb in ("curl", "wget", "tar", "unzip"):
            flags = ("-o", "-O", "--output", "-P", "--directory-prefix", "-C", "-d", "--directory")
            for k, a in enumerate(args):
                if a in flags and k + 1 < len(args):
                    path = prot(args[k + 1], cwd)
                    if path:
                        return "%s writes into %s" % (verb, _show(path, home))
    return None


def bash_guard_main(stdin=None):
    """PreToolUse(Bash). Silent unless paranoid.bash_guard is on AND the command writes to the
    protected code. Then: `ask` -- or, with gt unlock on, a fresh confirmation (deny if it is
    not given). Never `allow`: no output is no decision."""
    try:
        data = json.loads((stdin or sys.stdin).read())
    except ValueError:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    if not GP.enforced("paranoid.bash_guard"):
        return 0
    cmd = (data.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str):
        return 0
    home = home_dir()
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and os.path.isabs(data["cwd"]) \
        else os.getcwd()
    why = bash_write_reason(cmd, cwd, home, protected_roots(home, plugin_dirs=True))
    if not why:
        return 0
    base = ("Protected gt code (gt paranoid.bash_guard): this command %s.\n\n"
            "gt's installed code is locked so an assistant cannot rewrite the files that guard it. "
            "If this change is intended -- an upgrade, a repair -- the way is:\n  %s\n(or %s). "
            "Turn the guard off with `gt_settings.py set paranoid.bash_guard off`."
            % (why, ROUTE_UNLOCK, ROUTE_INSTALL))
    decision, reason = "ask", base
    if unlock_enabled(home):
        decision = "deny"
        try:
            import gt_unlock_client
            v = gt_unlock_client.check("gt:settings:hooks", request=True,
                                       reason="Bash command writes to gt's installed code")
            if v.get("allowed"):
                return 0                                     # a fresh confirmation was given
            reason = "BLOCKED (gt unlock): %s\n\n%s (%s)" % (base, v.get("message"), v.get("code"))
        except Exception as e:                               # noqa: BLE001 - fail closed
            reason = "BLOCKED (gt unlock could not be asked: %s)\n\n%s" % (type(e).__name__, base)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))
    return 0


# ----------------------------------------------------------------------------------------- CLI
def _opt(a, flag):
    if flag in a:
        k = a.index(flag)
        return a[k + 1] if k + 1 < len(a) else None
    return None


def cmd_status(a):
    s = state()
    if "--json" in a:
        print(json.dumps(s, indent=1, sort_keys=True))
    else:
        print(render_status(s))
    return 0


def cmd_verify(a):
    v = verify()
    if "--json" in a:
        print(json.dumps(v, indent=1))
    elif v["ok"]:
        print("integrity: ok (%d files checked%s)" % (v["checked"], ", " + v["mode"] if v["mode"]
                                                      else ""))
    else:
        print("integrity: FAILED -- %d problem(s)" % len(v["problems"]))
        for pr in v["problems"][:40]:
            print("  " + pr)
        print("  Route: %s, then %s." % (ROUTE_UNLOCK, ROUTE_INSTALL))
    return 0 if v["ok"] else 1


def cmd_unlock(a):
    if "--for-upgrade" not in a:
        print("usage: gt_integrity.py unlock --for-upgrade [--emit-token]   "
              "(unlocking for anything else is not offered)", file=sys.stderr)
        return 2
    say = (lambda m: print(m, file=sys.stderr)) if "--emit-token" in a else print
    if not code_roots():
        say("nothing is installed to unlock")
        if "--emit-token" in a:
            print("")
        return 0
    ok, how, msg = require_presence("unlock gt's installed code for an upgrade or repair")
    if not ok:
        say(refusal(msg))
        return 1
    n = unlock_all()
    log_change(home_dir(), event="unlock", by=how, entries=n)
    say("unlocked %d entries (presence: %s)" % (n, how))
    if "--emit-token" in a:
        print(make_token())
    return 0


def cmd_lock(a, force_presence=False):
    """(Re)seal and lock. Presence rules: nothing locked or sealed yet -> the first pin is free;
    locked -> only with the installer's token (the code must be unlocked first); sealed -> the
    token or a fresh presence check. The same rules unlock when the group has been turned off
    (editing the config file by hand is not a way around the check)."""
    home = home_dir()
    tok = os.environ.get("GT_INTEGRITY_TOKEN") or _opt(a, "--token")
    p = P(home)
    if not os.path.isdir(p["hooks"]):
        print("nothing installed under this home: nothing to lock")
        return 0
    cfg = config(home)
    locked = count_locked(home)[0] > 0
    sealed = os.path.exists(p["integrity"]) or os.path.exists(p["sealed"])
    wanted = GP.effective("paranoid.os_lock", cfg) or GP.effective("paranoid.signed_manifest", cfg)
    if not wanted and not (locked or sealed or os.path.exists(p["stamps"])):
        return 0                                        # the default: nothing to do, say nothing
    if locked and wanted and not token_ok(tok, home):
        print(refusal("the code is locked; unlock it first (that is the presence check)"))
        return 1
    if (sealed or locked or force_presence) and not token_ok(tok, home):
        ok, _how, msg = require_presence("re-seal and lock gt's installed code" if wanted
                                         else "release the lock on gt's installed code",
                                         force=not wanted)
        if not ok:
            print(refusal(msg))
            return 1
    ok, lines = apply(home)
    for ln in lines:
        print(ln)
    return 0 if ok else 1


def cmd_finish(a):
    """The installer is done with its token: it is spent."""
    drop_token(home_dir())
    return 0


def cmd_locked(a):
    return 0 if active() else 1


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_integrity.py status|verify|unlock --for-upgrade|lock|relock|apply|locked"
              "|bash-guard --hook")
        return 2
    cmd, rest = a[0], a[1:]
    table = {"status": cmd_status, "verify": cmd_verify, "unlock": cmd_unlock, "lock": cmd_lock,
             "apply": cmd_lock, "locked": cmd_locked, "finish": cmd_finish}
    if cmd == "bash-guard":
        return bash_guard_main()
    if cmd == "relock":
        return cmd_lock(rest, force_presence=True)
    if cmd in table:
        return table[cmd](rest)
    print("unknown command %r" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
