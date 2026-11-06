#!/usr/bin/env python3
"""gt_release_watch.py -- tell the user a newer gt release exists upstream, and ask before installing (0.22.0, phase 81).

    gt_release_watch.py poll [--force]          read the newest release tag from the source; refresh the cache
    gt_release_watch.py notice [--session ID]   SessionStart: print the one-line notice, or nothing
    gt_release_watch.py decline [--session ID]  the user said "not now": no repeat in this session
    gt_release_watch.py install-command         the exact command that installs the newest release (printed, never run)
    gt_release_watch.py status

gt_version_check.py already says when a newer release is CHECKED IN beside the installed one. This is the new
part: a release exists that is not on this machine yet. `git ls-remote --tags <gt_release_source>` reads tag
names only (a public repo needs no token; a private one uses the user's own git credentials, which gt never
reads), the highest vMAJOR.MINOR.PATCH wins, and the answer is cached in ~/.claude/golden-thread/update/latest.json
as {checked_at, latest, source_host, failed_at}: a host name, never a URL with a credential.

The SessionStart path never waits on the network. It reads the cache and, when that is older than 6 hours,
starts a detached background poll (capped at 20 seconds) for the next start. A failed poll keeps the old
value and records the time. It prints (to the model, which asks the user once; install now / not now):

    gt 0.22.0 is available (installed 0.21.6). Install now? You will need to restart Claude Code afterwards.

Declining writes only a marker for that session id, so the line does not repeat when the same session starts
again (resume, compact); the next session asks again while the release is pending.

Nothing installs without a yes, and gt never installs. On a yes the model runs `install-command` and hands its
output to gt_terminal.py, which opens the user's own terminal holding the command: fetch the release tag into
a temporary folder and run its install.sh, the owner-gated, restart-attached path. This module never starts
an installer, and there is NO auto-install value: `release_notice` is ask or off.

Settings (vault-config.json): `release_notice` ask (default) | off; `gt_release_source` a git URL, the public
gt repository by default; `none` turns the watch off, and a machine on another git host points it at its own
repository. Test hooks: GT_RELEASE_NO_SPAWN (no background poll),
GT_RELEASE_SPAWN_DRY (say "would poll" on stderr instead of spawning), GT_RELEASE_POLL_CAP (seconds).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
STALE_AFTER = 6 * 3600
POLL_CAP = 20
try:
    sys.path.insert(0, HERE)
    import gt_components as _gc
    DEFAULT_SOURCE = _gc.DEFAULT_RELEASE_SOURCE         # "" in a source tree: no release source set; the publish step stamps it
except Exception:                                       # noqa: BLE001 -- a missing neighbour must never break the hook
    DEFAULT_SOURCE = ""
TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
GT_KEY = "gt@golden-thread-plugin"


def home():
    return os.path.expanduser("~")


def folder():
    return os.path.join(home(), ".claude", "golden-thread", "update")


def cache_file():
    return os.path.join(folder(), "latest.json")


def state_file():
    return os.path.join(folder(), "state.json")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def config():
    return _read(os.path.join(home(), ".claude", "vault-config.json"))


def notice_mode(cfg=None):
    """ask or off. Anything but `ask` reads as off: there is no value that installs."""
    v = str((cfg if cfg is not None else config()).get("release_notice") or "ask").strip().lower()
    return "ask" if v == "ask" else "off"


def source(cfg=None):
    """the configured git URL (the build's stamped default when unset, empty in a source tree), or "" when it is `none` or carries a credential."""
    v = str((cfg if cfg is not None else config()).get("gt_release_source") or DEFAULT_SOURCE).strip()
    return v if v.lower() != "none" and valid_source(v) else ""


def valid_source(v):
    """a git URL or local path with no whitespace, no option-like start and no user:password."""
    if not v or any(c.isspace() for c in v) or v.startswith("-") or len(v) > 300:
        return False
    if "://" in v:
        net = urllib.parse.urlsplit(v).netloc
        return bool(net) and ":" not in net.rpartition("@")[0]
    return True


def host_of(v):
    """the host name only; a local path or unreadable URL gives "local"."""
    if "://" in v:
        return (urllib.parse.urlsplit(v).hostname or "local")[:100]
    m = re.match(r"^[\w.-]+@([\w.-]+):", v)
    return m.group(1) if m else "local"


def parse_version(v):
    m = TAG.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newest_tag(ls_remote_output):
    """-> "MAJOR.MINOR.PATCH" of the highest release tag in `git ls-remote --tags` output, or None.
    Peeled refs (^{}) and every tag that is not a plain release number (rc suffix, words) are ignored."""
    best = None
    for line in ls_remote_output.splitlines():
        ref = line.split("\t")[-1].strip()
        if not ref.startswith("refs/tags/") or ref.endswith("^{}"):
            continue
        key = parse_version(ref[len("refs/tags/"):])
        if key and (best is None or key > best):
            best = key
    return ".".join(str(x) for x in best) if best else None


def poll_cap():
    try:
        return max(1, int(os.environ.get("GT_RELEASE_POLL_CAP") or POLL_CAP))
    except ValueError:
        return POLL_CAP


def poll(src, now=None):
    """one read of the source's tags. A failure keeps the old `latest` and records the time. -> the cache."""
    now = time.time() if now is None else now
    old = _read(cache_file())
    doc = {"checked_at": now, "latest": old.get("latest"), "source_host": host_of(src), "failed_at": None}
    try:
        p = subprocess.run(["git", "ls-remote", "--tags", src], capture_output=True, text=True,
                           timeout=poll_cap(), stdin=subprocess.DEVNULL,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if p.returncode != 0:
            raise OSError("ls-remote failed")
        doc["latest"] = newest_tag(p.stdout)
    except (OSError, subprocess.SubprocessError):
        doc["failed_at"] = now
        doc["checked_at"] = old.get("checked_at") or 0
    _write(cache_file(), doc)
    return doc


def installed_version():
    try:
        import gt_version_check
        return gt_version_check.installed_version(GT_KEY)
    except Exception:                                    # noqa: BLE001
        return None


def stale(doc, now=None):
    now = time.time() if now is None else now
    last = max(float(doc.get("checked_at") or 0), float(doc.get("failed_at") or 0))
    return now - last > STALE_AFTER


def _spawn_poll():
    """a detached background poll for the next start; the caller never waits on it."""
    if os.environ.get("GT_RELEASE_NO_SPAWN"):
        return
    if os.environ.get("GT_RELEASE_SPAWN_DRY"):
        sys.stderr.write("gt release watch: would poll in the background\n")
        return
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "poll", "--force"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        pass


def notice(session=None, cfg=None):
    """-> the SessionStart text ("" for nothing). Reads the cache; starts a background poll when it is stale."""
    cfg = config() if cfg is None else cfg
    src = source(cfg)
    if notice_mode(cfg) != "ask" or not src:
        return ""
    doc = _read(cache_file())
    if stale(doc):
        _spawn_poll()
    latest, have = parse_version(doc.get("latest")), parse_version(installed_version())
    if not latest or not have or latest <= have:
        return ""
    st = _read(state_file())
    if session and st.get("declined_session") == session:
        return ""
    _write(state_file(), {"notified_session": session, "declined_session": st.get("declined_session")})
    new = ".".join(str(x) for x in latest)
    return ("gt %s is available (installed %s). Install now? You will need to restart Claude Code afterwards.\n"
            "Ask the user once: install now, or not now. Only on an explicit yes, run "
            "`python3 ~/.claude/golden-thread/hooks/gt_release_watch.py install-command` and give its output "
            "to gt_terminal.py open --command, so the user sees the command and runs it in their own terminal. "
            "On not now, run `python3 ~/.claude/golden-thread/hooks/gt_release_watch.py decline --session %s`. "
            "Never install without the yes.\n" % (new, ".".join(str(x) for x in have), session or "-"))


def decline(session=None):
    st = _read(state_file())
    sid = session or st.get("notified_session")
    _write(state_file(), {"notified_session": st.get("notified_session"), "declined_session": sid})
    return sid


def install_command(src, version):
    """the one-line command for the user's terminal: the release tag into a temporary folder, then its install.sh.
    Printed and handed to the terminal offer; nothing here runs it."""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"       # noqa: E731
    return ('d=$(mktemp -d) && git clone --depth 1 --branch v%s %s "$d/gt" && bash "$d/gt/install.sh"'
            % (version, q(src)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_release_watch.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll")
    p.add_argument("--force", action="store_true")
    for n in ("notice", "decline"):
        p = sub.add_parser(n)
        p.add_argument("--session")
    sub.add_parser("install-command")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    cfg = config()
    src = source(cfg)
    if a.cmd == "poll":
        if notice_mode(cfg) == "ask" and src and (a.force or stale(_read(cache_file()))):
            poll(src)
        return 0
    if a.cmd == "notice":
        try:
            sys.stdout.write(notice(a.session, cfg))
        except Exception:                                # noqa: BLE001 -- a session start never fails on this
            pass
        return 0
    if a.cmd == "decline":
        decline(a.session)
        return 0
    if a.cmd == "install-command":
        latest = _read(cache_file()).get("latest")
        if not src or not parse_version(latest):
            print("no release known: set gt_release_source and run `gt_release_watch.py poll`", file=sys.stderr)
            return 2
        print(install_command(src, latest))
        return 0
    doc = _read(cache_file())
    print("release_notice %s; source %s; latest %s; installed %s; checked %s%s" % (
        notice_mode(cfg), host_of(src) if src else "(none set)", doc.get("latest") or "-",
        installed_version() or "-", time.strftime("%Y-%m-%d %H:%M", time.localtime(doc["checked_at"]))
        if doc.get("checked_at") else "never", "; last poll failed" if doc.get("failed_at") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
