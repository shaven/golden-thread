#!/usr/bin/env python3
"""gt_lock -- per-file age locks for vault folders (0.20.0).

    gt_lock.py add FILE... --vault V [--recipient R | --recipients-file F] [--identity I] [--dry-run]
    gt_lock.py open FILE.age --vault V [--identity I] [--show]
    gt_lock.py restore FILE.age --vault V [--identity I] [--dry-run]
    gt_lock.py status --vault V [--json]

`add` encrypts each FILE with age to FILE.age beside it, then removes the plaintext. With no
--recipient / --recipients-file it encrypts to the identity's own recipient (`age -e -i`).
`open` decrypts to standard output only, and only when that is a pipe: never to a file, and
never to a terminal unless --show. `restore` turns FILE.age back into the plaintext file.
`status` lists every folder holding locked files and rates the identity honestly.

Each folder holding a locked file gets a plaintext `.gt-locked` stub naming its unlock scope
(gt:lock:<folder>), its level and how to open it. gt's readers -- the hooks, gt_lint, the
TASKS.md rollup and keyword recall -- treat such a folder as absent and say so once.

Levels (design Addendum 2): L1 when any recipient's identity is a plaintext file (the usual
~/.config/sops/age/keys.txt); L2 when every recipient is hardware-held (age-plugin-se,
age-plugin-yubikey). Never L3: once opened, the plaintext is readable by any process of yours.
With unlock off, `open` still works and the lock is only as strong as the identity file.

What it does NOT remove: earlier plaintext copies in git history, Dropbox / iCloud version
history, Time Machine, or Obsidian's own cache. Lock a file before it is ever committed or
synced. The identity: --identity, else $GT_AGE_IDENTITY, else the sops age key file. The age
binary: $GT_AGE (a path), else `age` on PATH.

Exit codes: 0 done, 1 failed or refused, 2 usage error, 3 the unlock authority refused.
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_unlock_brokers as B       # noqa: E402  (age identity classification only)

STUB = ".gt-locked"
SUFFIX = ".age"
AGE_HEADER = b"age-encryption.org/v1\n"
# SECURITY INVARIANT (owner decision (b), Addendum 2): these folders are NEVER lockable.
PROTECTED = ("core-rules", "global-memory")
PROTECTED_WHY = ("Core-rule injection reads them every turn; locking them would silently "
                 "switch it off")
CMD = "python3 ~/.claude/golden-thread/hooks/gt_lock.py"
EXIT_FAIL, EXIT_USAGE, EXIT_GATE = 1, 2, 3


class LockError(Exception):
    pass


def _say(msg):
    sys.stderr.write("gt_lock: %s\n" % msg)


# ------------------------------------------------------------------ paths
def _rel_inside(root, path):
    """Vault-relative POSIX path of `path` under `root`, or None when it is not inside."""
    try:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:                                   # another drive (Windows)
        return None
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + "/"):
        return None
    return rel


def _configured_core_rules():
    try:
        import gt_paths
        p = gt_paths.read_config().get("core_rules_path")
        return p.strip("/").replace("\\", "/").lower() if isinstance(p, str) and p else None
    except Exception:                                    # noqa: BLE001 - optional extra guard
        return None


def protected_reason(rel):
    """Why `rel` (vault-relative) can never be locked, or None.
    SECURITY INVARIANT: any path component named core-rules or global-memory -- the root
    folders, the pre-0.17.0 Projects/golden-thread/core-rules, any case spelling -- and the
    configured core_rules_path are refused, so Core-rule injection can never be switched off
    by a lock."""
    parts = [p.lower() for p in rel.split("/") if p]
    for name in PROTECTED:
        if name in parts:
            return "%s/ is never lockable: %s" % (name, PROTECTED_WHY)
    conf = _configured_core_rules()
    low = "/".join(parts)
    if conf and (low == conf or low.startswith(conf + "/")):
        return "%s/ (the configured core_rules_path) is never lockable: %s" % (conf, PROTECTED_WHY)
    return None


def target(vault, path, *, locked, for_add=False):
    """Validate a FILE argument -> (absolute path, vault-relative path).
    SECURITY INVARIANTS: the file must lie inside the vault both as spelled (after ../ is
    collapsed) AND after every symlink is resolved, and both must name the same place -- so a
    symlinked file or folder can never carry a lock (or a decrypt) somewhere else; no hidden or
    tool folder (.git, .obsidian); a regular file; for `add`, nothing protected."""
    raw = os.path.expanduser(path)
    cand = raw if os.path.isabs(raw) else os.path.abspath(raw)
    if not os.path.lexists(cand) and not os.path.isabs(raw) \
            and os.path.lexists(os.path.join(vault, raw)):
        cand = os.path.join(vault, raw)                  # a vault-relative spelling
    lex = os.path.normpath(cand)
    vlex, vreal = os.path.normpath(vault), os.path.realpath(vault)
    rel = _rel_inside(vlex, lex) or _rel_inside(vreal, lex)
    rel_real = _rel_inside(vreal, os.path.realpath(lex))
    if rel is None or rel_real is None:
        raise LockError("%s is outside the vault %s; only vault files can be locked" % (path, vault))
    if for_add:
        for r in (rel, rel_real):
            why = protected_reason(r)
            if why:
                raise LockError("refused %s: %s" % (path, why))
    if os.path.islink(lex):
        raise LockError("refused %s: it is a symlink; lock the file it points to instead" % path)
    if rel.lower() != rel_real.lower():
        raise LockError("refused %s: a folder on its path is a symlink (it resolves to %s)"
                        % (path, rel_real))
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts[:-1]) or parts[-1] == STUB:
        raise LockError("refused %s: hidden and tool folders (.git, .obsidian) and the %s stub "
                        "are not lockable" % (path, STUB))
    if len(parts) < 2:
        raise LockError("refused %s: lock files inside a folder (e.g. Secrets/), not at the "
                        "vault root" % path)
    if locked != rel.endswith(SUFFIX):
        raise LockError(("%s is not a locked file (no %s)" if locked
                         else "refused %s: it is already a locked %s file") % (path, SUFFIX))
    if not os.path.isfile(lex):
        raise LockError("%s is not a file" % path if os.path.lexists(lex)
                        else "%s does not exist" % path)
    return lex, rel


def folder_of(rel):
    return rel.rsplit("/", 1)[0]


def scope_for(rel):
    return "gt:lock:" + folder_of(rel)


# ------------------------------------------------------------------ age
def age_argv():
    """argv prefix for age: $GT_AGE (a path), else `age` on PATH. A list so a test can stand a
    fake in for it in process."""
    p = os.environ.get("GT_AGE") or shutil.which("age")
    if not p:
        raise LockError("age is not installed (brew install age / apt install age / winget "
                        "install FiloSottile.age), or set GT_AGE to its path")
    return [p]


def default_identity(explicit=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("GT_AGE_IDENTITY")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    p = B._sops_identity()
    return p if os.path.isfile(p) else None


def recipient_level(r):
    """Level of one recipient string (public; safe to read). age1se1... and age1yubikey1...
    are hardware-held (L2); a plain age1... X25519 key, an SSH key or another plugin is L1."""
    r = r.strip()
    if r.startswith("age1"):
        hrp = r[:r.rfind("1")].lower()                  # bech32: the HRP ends at the last "1"
        return "L2" if hrp in ("age1se", "age1yubikey") else "L1"
    return "L1"


def weakest(levels):
    levels = [lv for lv in levels if lv]
    return "L1" if (not levels or "L1" in levels) else "L2"


def _recipients_file_levels(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(recipient_level(line))
    return out


def _recipients_note(level):
    if level == "L2":
        return "every recipient is hardware-held (age-plugin-se / age-plugin-yubikey)"
    return "a recipient's identity may be a plaintext file, which any process of yours can read"


def encryption_plan(ns):
    """-> (age args, level, note)."""
    if ns.recipient:
        lv = weakest([recipient_level(r) for r in ns.recipient])
        return sum((["-r", r] for r in ns.recipient), []), lv, _recipients_note(lv)
    if ns.recipients_file:
        f = os.path.abspath(os.path.expanduser(ns.recipients_file))
        try:
            lv = _recipients_file_levels(f)
        except OSError as e:
            raise LockError("cannot read the recipients file %s (%s)" % (f, e.strerror))
        if not lv:
            raise LockError("the recipients file %s names no recipient" % f)
        return ["-R", f], weakest(lv), _recipients_note(weakest(lv))
    ident = default_identity(ns.identity)
    if not ident:
        raise LockError("no recipient: pass --recipient / --recipients-file, or --identity "
                        "(or set GT_AGE_IDENTITY) to encrypt to your own identity")
    k = B.age_identity_kind(ident)
    if not k["level"]:
        raise LockError(k["note"])
    return ["-i", ident], k["level"], k["note"]


def _decrypt(ident, path, out_fd):
    """age -d to `out_fd`. SECURITY INVARIANT: the plaintext goes straight from age to the fd
    it is given (the pipe, or restore's 0600 temp file); this process never holds or writes it."""
    return subprocess.run(age_argv() + ["-d", "-i", ident, path], stdin=subprocess.DEVNULL,
                          stdout=out_fd, check=False).returncode


# ------------------------------------------------------------------ stub
def read_stub(folder_abs):
    out = {}
    try:
        with open(os.path.join(folder_abs, STUB), "r", encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.partition(":")
                if sep and not k.startswith("#") and k.strip() in ("scope", "level"):
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_stub(folder_abs, folder_rel, level, why):
    prev = read_stub(folder_abs).get("level")
    level = weakest([level, prev]) if prev else level
    example = "%s/<name>.age" % folder_rel
    text = ("# This folder holds files locked by gt (gt_lock.py, 0.20.0). Files ending in .age\n"
            "# are encrypted with age. While this file is here, gt's hooks, gt_lint, the TASKS.md\n"
            "# rollup and keyword recall treat the folder as absent (plaintext beside them too).\n"
            "scope: gt:lock:%s\n"
            "level: %s\n"
            "why: %s\n"
            "open: %s open %s --vault . | less     (from the vault root; a pipe only)\n"
            "restore: %s restore %s --vault .\n"
            "status: %s status --vault .\n"
            % (folder_rel, level, why, CMD, example, CMD, example, CMD))
    tmp = os.path.join(folder_abs, STUB + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, os.path.join(folder_abs, STUB))
    return level


def _has_locked(folder_abs):
    try:
        return any(n.endswith(SUFFIX) for n in os.listdir(folder_abs))
    except OSError:
        return False


# ------------------------------------------------------------------ the unlock gate
def gate(scope, reason):
    """-> (allowed, message). With unlock off it allows and says what the lock then rests on.
    SECURITY INVARIANT: with unlock on, nothing is decrypted unless the AUTHORITY answers
    allowed for gt:lock:<folder>; an unreachable authority refuses (gt_unlock_client)."""
    import gt_unlock_client as C
    if not C.enabled():
        return True, ("unlock is off, so this lock is only as strong as the identity file "
                      "(L1 for a plaintext identity); `gt_unlock.py policy enable` gates it")
    tty = bool(sys.stdin and sys.stdin.isatty())
    v = C.check(scope, request=True, reason=reason, answer=C.tty_answer if tty else None)
    if v.get("allowed"):
        return True, None
    hints = "; ".join(v.get("hints") or [])
    return False, "%s needs an unlock grant: %s (%s)%s" % (
        scope, v.get("message"), v.get("code"), (" -- " + hints) if hints else "")


def stdout_kind():
    """tty | file | pipe (pipes, sockets, /dev/null and other devices count as a pipe)."""
    try:
        if sys.stdout.isatty():
            return "tty"
        st = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return "pipe"
    return "file" if stat.S_ISREG(st.st_mode) else "pipe"


def stdout_fd():
    sys.stdout.flush()
    return sys.stdout.fileno()


# ------------------------------------------------------------------ commands
def cmd_add(ns, vault):
    try:
        args, level, why = encryption_plan(ns)
        files = [target(vault, f, locked=False, for_add=True) for f in ns.files]
        age = age_argv() if not ns.dry_run else None
    except LockError as e:
        _say(str(e))
        return EXIT_FAIL
    rc = 0
    for path, rel in files:
        out = path + SUFFIX
        scope = scope_for(rel)
        if os.path.lexists(out):
            _say("refused %s: %s already exists" % (rel, rel + SUFFIX))
            rc = EXIT_FAIL
            continue
        if ns.dry_run:
            print("would lock %s -> %s (scope %s, level %s; %s)" % (rel, rel + SUFFIX, scope,
                                                                   level, why))
            continue
        tmp = out + ".tmp-%d" % os.getpid()
        try:
            p = subprocess.run(age + ["-e"] + args + ["-o", tmp, path],
                               stdin=subprocess.DEVNULL, check=False)
            with open(tmp, "rb") as f:
                head = f.read(len(AGE_HEADER) + 1)
            if p.returncode != 0 or not head.startswith(AGE_HEADER) or len(head) <= len(AGE_HEADER):
                raise LockError("age did not produce a valid file (exit %d)" % p.returncode)
            os.replace(tmp, out)
        except (OSError, LockError) as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            _say("could not lock %s: %s; the plaintext is untouched" % (rel, e))
            rc = EXIT_FAIL
            continue
        # SECURITY INVARIANT: the plaintext is removed only after a well-formed .age exists.
        os.remove(path)
        lv = write_stub(os.path.dirname(path), folder_of(rel), level, why)
        print("locked %s -> %s (scope %s, level %s)" % (rel, rel + SUFFIX, scope, lv))
    if not ns.dry_run and rc == 0:
        print("note: earlier plaintext copies (git history, sync version history, backups, "
              "Obsidian's cache) are not removed")
    return rc


def cmd_open(ns, vault):
    try:
        path, rel = target(vault, ns.file, locked=True)
    except LockError as e:
        _say(str(e))
        return EXIT_FAIL
    kind = stdout_kind()
    # SECURITY INVARIANT: decrypt to a pipe only -- never to a file on disk, never to a
    # terminal (scrollback, screen sharing) unless the user says --show.
    if kind == "file":
        _say("refusing to decrypt into a file; `open` writes to a pipe only "
             "(use `restore` to unlock the file for good)")
        return EXIT_FAIL
    if kind == "tty" and not ns.show:
        _say("refusing to print to a terminal; pipe it (| less) or pass --show")
        return EXIT_FAIL
    ident = default_identity(ns.identity)
    if not ident:
        _say("no age identity: pass --identity or set GT_AGE_IDENTITY")
        return EXIT_FAIL
    ok, msg = gate(scope_for(rel), "open the locked file %s" % rel)
    if not ok:
        _say(msg)
        return EXIT_GATE
    if msg:
        _say(msg)
    try:
        rc = _decrypt(ident, path, stdout_fd())
    except LockError as e:
        _say(str(e))
        return EXIT_FAIL
    if rc != 0:
        _say("age could not decrypt %s (exit %d)" % (rel, rc))
        return EXIT_FAIL
    return 0


def cmd_restore(ns, vault):
    try:
        path, rel = target(vault, ns.file, locked=True)
    except LockError as e:
        _say(str(e))
        return EXIT_FAIL
    plain = path[:-len(SUFFIX)]
    if os.path.lexists(plain):
        _say("refused: %s already exists; move it aside first" % rel[:-len(SUFFIX)])
        return EXIT_FAIL
    if ns.dry_run:
        print("would restore %s -> %s (scope %s)" % (rel, rel[:-len(SUFFIX)], scope_for(rel)))
        return 0
    ident = default_identity(ns.identity)
    if not ident:
        _say("no age identity: pass --identity or set GT_AGE_IDENTITY")
        return EXIT_FAIL
    ok, msg = gate(scope_for(rel), "restore the locked file %s" % rel)
    if not ok:
        _say(msg)
        return EXIT_GATE
    if msg:
        _say(msg)
    tmp = plain + ".tmp-%d" % os.getpid()
    # SECURITY INVARIANT: the plaintext is created owner-only (0600) and exclusively.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        try:
            rc = _decrypt(ident, path, fd)
        finally:
            os.close(fd)
        if rc != 0:
            raise LockError("age could not decrypt %s (exit %d)" % (rel, rc))
        os.replace(tmp, plain)
    except (OSError, LockError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        _say("could not restore %s: %s; the locked file is untouched" % (rel, e))
        return EXIT_FAIL
    os.remove(path)
    folder = os.path.dirname(path)
    if not _has_locked(folder) and os.path.exists(os.path.join(folder, STUB)):
        os.remove(os.path.join(folder, STUB))
    print("restored %s" % rel[:-len(SUFFIX)])
    return 0


def scan(vault):
    """Every folder holding .age files or a stub. Hidden folders are not walked."""
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        n = sum(1 for f in files if f.endswith(SUFFIX))
        if n or STUB in files:
            rel = _rel_inside(vault, root) or "."
            stub = read_stub(root) if STUB in files else {}
            out.append({"folder": rel, "files": n, "stub": STUB in files,
                        "scope": stub.get("scope"), "level": stub.get("level")})
    return out


def cmd_status(ns, vault):
    import gt_unlock_client as C
    ident = default_identity(None)
    k = B.age_identity_kind(ident) if ident else {"kind": "missing", "level": None,
                                                   "note": "no age identity found"}
    folders = scan(vault)
    problems = []
    for f in folders:
        if f["files"] and not f["stub"]:
            problems.append("%s: locked files but no %s stub" % (f["folder"], STUB))
        if f["stub"] and not f["files"]:
            problems.append("%s: a %s stub but no locked files" % (f["folder"], STUB))
        if protected_reason(f["folder"]):
            problems.append("%s: locked files here cannot be read by Core-rule injection"
                            % f["folder"])
    unlock = "on" if C.enabled() else "off"
    if ns.json:
        print(json.dumps({"vault": vault, "unlock": unlock,
                          "identity": {"path": ident, "kind": k["kind"], "level": k["level"],
                                       "note": k["note"]},
                          "folders": folders, "problems": problems}, indent=2))
        return 1 if problems else 0
    print("identity: %s -- %s, level %s" % (ident or "(none)", k["kind"], k["level"] or "-"))
    print("  " + k["note"])
    print("unlock: %s%s" % (unlock, "" if unlock == "on" else
                            " (opening a locked file needs only the identity file)"))
    if not folders:
        print("no locked folders")
    for f in folders:
        print("%-40s %3d file(s)  scope %s  level %s" % (f["folder"], f["files"],
                                                        f["scope"] or "-", f["level"] or "-"))
    for p in problems:
        print("problem: " + p)
    return 1 if problems else 0


# ------------------------------------------------------------------ CLI
def parser():
    ap = argparse.ArgumentParser(prog="gt_lock.py", description=__doc__.split("\n\n")[0])
    # --vault / --dry-run parse before AND after the subcommand (dev/check_cli_contract.py);
    # the subparser copies default to SUPPRESS so they never overwrite a value given first.
    ap.add_argument("--vault")
    ap.add_argument("--dry-run", action="store_true")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add", parents=[common], help="encrypt files to FILE.age, remove the plaintext")
    a.add_argument("files", nargs="+", metavar="FILE")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--recipient", action="append", help="an age recipient (repeatable)")
    g.add_argument("--recipients-file", help="a file of age recipients, one per line")
    a.add_argument("--identity", help="encrypt to this identity's recipient (the default)")
    o = sub.add_parser("open", parents=[common], help="decrypt to a pipe (stdout) only")
    o.add_argument("file", metavar="FILE.age")
    o.add_argument("--identity")
    o.add_argument("--show", action="store_true", help="allow printing to a terminal")
    r = sub.add_parser("restore", parents=[common], help="decrypt back to the plaintext file")
    r.add_argument("file", metavar="FILE.age")
    r.add_argument("--identity")
    s = sub.add_parser("status", parents=[common], help="locked folders and the honest level")
    s.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    ap = parser()
    ns = ap.parse_args(argv)
    if not ns.cmd:
        ap.print_usage(sys.stderr)
        return EXIT_USAGE
    if not ns.vault:
        _say("--vault is required (name the vault; Core rule core_explicit_vault_target)")
        return EXIT_USAGE
    vault = os.path.abspath(os.path.expanduser(ns.vault))
    if not os.path.isdir(vault):
        _say("vault not found at %s" % vault)
        return EXIT_USAGE
    return {"add": cmd_add, "open": cmd_open, "restore": cmd_restore,
            "status": cmd_status}[ns.cmd](ns, vault)


if __name__ == "__main__":
    sys.exit(main())
