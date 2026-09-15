#!/usr/bin/env python3
"""The files install.sh refreshes INSIDE a vault, refreshed without destroying the owner's.

  vault_refresh.py backup  --vault V --out FILE   before install.sh writes anything
  vault_refresh.py prune   --vault V --backup FILE  keep FILE only if something changed
  vault_refresh.py refresh --vault V [--dry-run]  .githooks/, vault tools, core.hooksPath

Until 0.15.0 install.sh did this inline, and a fresh-context validation found four ways
it changed owner content with nobody choosing it:

  * .githooks/ was copied over on every install -- an uncommitted edit to a hook was
    gone, with no backup anywhere;
  * a vault tool was replaced when its file was OLDER on disk than the release's copy.
    Modification times say nothing about who wrote a file: an owner's edit older than a
    freshly unpacked release was "stale" and replaced, while a pristine copy seeded after
    the release was unpacked was reported "AHEAD";
  * core.hooksPath was set unconditionally, so an owner's own hooks directory was
    silently disconnected;
  * the only backup, gt_upgrade's vault tarball, was taken AFTER all of the above.

The rules now, by CONTENT. templates/shipped-hashes.json lists every tool and hook text
gt has ever shipped (dev/shipped_hashes.py):

  vault copy == this release        -> nothing to do
  vault copy == something gt shipped -> it is gt's text: replaced
  anything else                     -> the owner's. A TOOL is kept and reported (it is
                                       code the owner may depend on). A HOOK with
                                       uncommitted changes is kept; a committed one is
                                       backed up outside the vault, then replaced (git
                                       has it too).

`backup` records every file this install may write, before it writes; `prune` deletes
that backup again when nothing in it changed, so an install that touched nothing leaves
nothing behind.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"
TOOLS_REL = Path("Projects") / "golden-thread" / "tools"
BACKUPS = Path.home() / ".claude" / "golden-thread" / "backups"
GIT_CONFIG_MEMBER = ".gt-install/core.hooksPath"
HASHES_MEMBER = ".gt-install/sha256.json"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def git(vault, *args):
    try:
        p = subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True)
        return p.returncode, p.stdout
    except OSError:
        return 127, ""


def is_git(vault):
    return git(vault, "rev-parse", "--git-dir")[0] == 0


def shipped(path=None):
    """-> {"tools": {name: set}, "githooks": {name: set}}; empty when the list is missing,
    which makes every differing copy the owner's -- the safe reading."""
    try:
        d = json.loads(Path(path or TEMPLATES / "shipped-hashes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tools": {}, "githooks": {}}
    return {k: {n: set(v) for n, v in (d.get(k) or {}).items()} for k in ("tools", "githooks")}


# ---- what install.sh may write ------------------------------------------------

def targets(vault):
    """Existing files an install may write into, relative to the vault."""
    out = []
    for rel in ("CLAUDE.md", "TASKS.md"):
        if (vault / rel).is_file():
            out.append(Path(rel))
    for d in [vault / ".githooks", vault / TOOLS_REL]:
        if d.is_dir():
            out += [f.relative_to(vault) for f in sorted(d.iterdir()) if f.is_file()]
    projects = vault / "Projects"
    if projects.is_dir():
        for cand in sorted(list(projects.glob("*/core-rules")) + list(projects.glob("*/*/core-rules"))):
            if (cand / "core_rule_priority_model.md").is_file():
                out += [f.relative_to(vault) for f in sorted(cand.glob("*.md"))]
    return out


def hooks_path(vault):
    rc, out = git(vault, "config", "--get", "core.hooksPath")
    return out.strip() if rc == 0 else ""


def cmd_backup(a):
    vault, out = Path(a.vault).resolve(), Path(a.out)
    if not vault.is_dir():
        return 0
    files = targets(vault)
    hashes = {str(r): sha(vault / r) for r in files}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        for r in files:
            tf.add(vault / r, arcname=str(r))
        for name, data in ((GIT_CONFIG_MEMBER, hooks_path(vault) if is_git(vault) else ""),
                           (HASHES_MEMBER, json.dumps(hashes, indent=1))):
            b = data.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(b), int(time.time())
            tf.addfile(info, io.BytesIO(b))
    return 0


def cmd_prune(a):
    vault, bak = Path(a.vault).resolve(), Path(a.backup)
    if not bak.is_file():
        return 0
    try:
        with tarfile.open(bak, "r:gz") as tf:
            before = json.loads(tf.extractfile(HASHES_MEMBER).read().decode("utf-8"))
            was_hp = tf.extractfile(GIT_CONFIG_MEMBER).read().decode("utf-8")
    except (OSError, KeyError, ValueError, tarfile.TarError):
        print("Vault files were backed up before this install wrote anything → %s" % bak)
        return 0
    changed = [r for r, h in before.items()
               if not (vault / r).is_file() or sha(vault / r) != h]
    hp_changed = is_git(vault) and hooks_path(vault) != was_hp
    if not changed and not hp_changed:
        bak.unlink()
        return 0
    print("Vault files this install changed were backed up before it wrote anything → %s" % bak)
    for r in changed:
        print("  changed: %s" % r)
    if hp_changed:
        print("  changed: git config core.hooksPath (was %r)" % was_hp)
    return 0


# ---- the refresh ---------------------------------------------------------------

def uncommitted(vault, rel):
    rc, out = git(vault, "status", "--porcelain", "--", str(rel))
    return rc == 0 and bool(out.strip())


def refresh_githooks(vault, known, dry, say):
    src = TEMPLATES / "githooks"
    if not src.is_dir():
        return
    dest_dir = vault / ".githooks"
    for t in sorted(src.iterdir()):
        if not t.is_file():
            continue
        dest, rel = dest_dir / t.name, Path(".githooks") / t.name
        if dest.is_file() and sha(dest) == sha(t):
            if not dry and not os.access(dest, os.X_OK):
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            continue
        if not dest.exists():
            note = "Seeded git hook → %s" % rel
        elif sha(dest) in known.get(t.name, set()):
            note = "Updated git hook → %s (was an earlier release's copy)" % rel
        else:
            # Matches no hook gt has shipped: the owner's edit, committed or not. Kept, as a
            # locally modified vault tool is (0.15.0: committed edits used to be replaced).
            if uncommitted(vault, rel):
                say("⚠ Git hook KEPT → %s has uncommitted changes, so it was not refreshed." % rel)
            else:
                say("⚠ Git hook MODIFIED LOCALLY → %s matches no hook gt has shipped; kept." % rel)
            say("  Diff it against the %s copy: %s" % (VERSION, t))
            continue
        if not dry:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(t, dest)
            dest.chmod(0o755)
        say(note)


def refresh_tools(vault, known, dry, say):
    src = TEMPLATES / "tools"
    if not src.is_dir():
        return
    dest_dir = vault / TOOLS_REL
    for t in sorted(src.glob("*.py")):
        dest = dest_dir / t.name
        if not dest.exists():
            if not dry:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(t, dest)
            say("Seeded vault tool → %s" % t.name)
        elif sha(dest) == sha(t):
            say("Vault tool verified → %s matches the %s template" % (t.name, VERSION))
        elif sha(dest) in known.get(t.name, set()):
            if not dry:
                shutil.copyfile(t, dest)
            say("Vault tool UPDATED → %s (was an earlier release's copy)" % t.name)
        else:
            say("⚠ Vault tool MODIFIED LOCALLY → %s matches no version gt has shipped; kept." % t.name)
            say("  Diff it against %s" % t)
            say("  and fold the change back into the plugin if it is a fix.")


def refresh_hooks_path(vault, dry, say):
    cur = hooks_path(vault)
    ours = {".githooks", str(vault / ".githooks"), str(vault / ".githooks") + "/", ".githooks/"}
    if cur in ours:
        say("Vault git attribution wired → %s (.githooks)" % vault)
        return
    if cur:
        say("⚠ core.hooksPath is %r (yours), so the attribution hooks in .githooks/ are not wired." % cur)
        say("  To keep both, call them from your hooks: %s/.githooks/<hook> \"$@\"" % vault)
        return
    if not dry:
        git(vault, "config", "core.hooksPath", ".githooks")
    say("Wired vault git attribution → %s (.githooks)" % vault)


def cmd_refresh(a):
    vault = Path(a.vault).resolve()
    if not vault.is_dir() or not is_git(vault):
        return 0
    known = shipped(a.shipped)
    lines = []
    refresh_githooks(vault, known["githooks"], a.dry_run, lines.append)
    refresh_tools(vault, known["tools"], a.dry_run, lines.append)
    refresh_hooks_path(vault, a.dry_run, lines.append)
    for l in lines:
        print(l)
    return 0


def release_version():
    try:
        return json.loads((HERE.parent / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return HERE.parent.name


VERSION = release_version()


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", required=True, help="the vault (never inferred)")
    common.add_argument("--dry-run", "-n", action="store_true", help="say what would change; write nothing")
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0], parents=[])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", parents=[common])
    b.add_argument("--out", required=True)
    r = sub.add_parser("prune", parents=[common])
    r.add_argument("--backup", required=True)
    f = sub.add_parser("refresh", parents=[common])
    f.add_argument("--shipped", help=argparse.SUPPRESS)   # tests: a different hash list
    a = p.parse_args(argv)
    if a.dry_run and a.cmd != "refresh":
        return 0
    return {"backup": cmd_backup, "prune": cmd_prune, "refresh": cmd_refresh}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
