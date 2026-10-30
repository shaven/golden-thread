#!/usr/bin/env python3
"""gt_sync.py -- keep the vault in step with its git remote: status, pull (fast-forward only),
push (gated by the push check).

    gt_sync.py status [--vault V] [--no-fetch] [--json]
    gt_sync.py pull   [--vault V] [--dry-run] [--json]
    gt_sync.py push   [--vault V] [--dry-run] [--json]
    gt_sync.py behind [--vault V] [--mode cached|fetch]      # the SessionStart line

WHY THIS EXISTS (0.18.1, request 2026-09-24-cross-device-sync). The vault is a git repo so its
truth reaches every machine, and nothing in gt moved it: a session opened a project from files
another machine had already superseded, wrote back on top of them, and left a divergence for
the owner to untangle by hand. The push check (gt_push_check.py, 0.9.x) says when THIS machine
has commits origin lacks; nothing said the opposite, and that needs a `git fetch` to know --
the request assumed the push check's `status --branch` output already showed it, and it does
not (triage correction).

WHAT EACH VERB DOES. Every git command names the vault with `git -C <vault>` (Core rule 2); no
alias, no config change, no merge, no rebase -- ever.

  status  fetch (bounded), then: branch -> upstream, commits ahead / behind, uncommitted
          changes, and how old the last fetch is. --no-fetch compares with the refs on disk.
  pull    fetch, then `git pull --ff-only`. Diverged (ahead AND behind) -> stops and says so,
          before git is asked to do anything; it never merges or rebases. Up to date -> says
          so. Otherwise reports the files changed and the newest commit's subject.
  push    runs the push check (gt_push_check.py) and refuses unless it reports the vault
          AHEAD of a real upstream (no upstream, no remote, detached HEAD -> refused, with the
          check's own text). Also refuses when origin has commits this machine lacks (pull
          first). Then `git push`. Nothing to push -> says so, exit 0.
  behind  one line for SessionStart, driven by the `sync_check` setting (off by default):
          `cached` compares with the remote-tracking ref already on disk (no network) and says
          how old it is; `fetch` runs one fetch bounded to a few seconds first. gt_push_check
          prints it after its own line, so no new hook is registered.

FETCHES NEVER HANG AND NEVER PROMPT: GIT_TERMINAL_PROMPT=0, and ssh in BatchMode with a connect
timeout, unless GIT_SSH_COMMAND is already set; every git call has a timeout.

--dry-run touches nothing, not even .git: no fetch, no pull, no push -- it reports from the refs
on disk and names the command it would run.

Exit: 0 ok / nothing to do | 1 refused or git failed (diverged, push check failed, behind on
push, pull not fast-forward) | 2 usage, no vault, not a git repo.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
FETCH_TIMEOUT = 60          # interactive verbs
HOOK_FETCH_TIMEOUT = 4      # SessionStart: bounded hard
GIT_TIMEOUT = 15
NET_TIMEOUT = 180           # pull / push


def find_vault(explicit: str | None) -> Path | None:
    cand = explicit or os.environ.get("GT_VAULT")
    if not cand:
        try:
            cand = json.loads((Path.home() / ".claude" / "vault-config.json")
                              .read_text(encoding="utf-8")).get("vault_path")
        except Exception:
            cand = None
    if not cand:
        return None
    p = Path(cand).expanduser()
    return p if p.is_dir() else None


def _env() -> dict:
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
    return e


def git(vault: Path, *args, timeout: float = GIT_TIMEOUT):
    """-> (ok, stdout, stderr). Never raises; a timeout is (False, "", "timed out after Ns")."""
    if shutil.which("git") is None:
        return False, "", "git is not installed"
    try:
        p = subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True,
                           timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        return False, "", "timed out after %ss" % timeout
    except OSError as exc:
        return False, "", str(exc)
    return p.returncode == 0, (p.stdout or "").strip(), (p.stderr or "").strip()


def _last_fetch_age(vault: Path) -> float | None:
    ok, gitdir, _ = git(vault, "rev-parse", "--absolute-git-dir")
    if not ok:
        return None
    try:
        return time.time() - (Path(gitdir) / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never fetched"
    if seconds < 90:
        return "fetched just now"
    if seconds < 5400:
        return "last fetch %d min ago" % (seconds // 60)
    if seconds < 172800:
        return "last fetch %d h ago" % (seconds // 3600)
    return "last fetch %d days ago" % (seconds // 86400)


def state(vault: Path, fetch: bool, fetch_timeout: float = FETCH_TIMEOUT) -> dict:
    """Everything the verbs decide on. `problem` is set when a comparison is impossible."""
    s = {"vault": str(vault), "repo": False, "branch": None, "upstream": None, "ahead": None,
         "behind": None, "dirty": None, "fetched": False, "fetch_error": None,
         "fetch_age": None, "problem": None}
    ok, _, err = git(vault, "rev-parse", "--git-dir")
    if not ok:
        s["problem"] = "not a git repository (%s)" % (err.splitlines()[-1] if err else "?")
        return s
    s["repo"] = True
    ok, branch, _ = git(vault, "rev-parse", "--abbrev-ref", "HEAD")
    s["branch"] = branch if ok else None
    if not ok or branch == "HEAD":
        s["problem"] = "detached HEAD -- check out a branch first"
        return s
    ok, up, _ = git(vault, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not ok:
        s["problem"] = "branch '%s' has no upstream (git -C \"%s\" push -u origin %s)" % (
            branch, vault, branch)
        return s
    s["upstream"] = up
    ok, porcelain, _ = git(vault, "status", "--porcelain")
    s["dirty"] = len([x for x in porcelain.splitlines() if x.strip()]) if ok else None
    if fetch:
        remote = up.split("/", 1)[0]
        ok, _, err = git(vault, "fetch", "--quiet", remote, timeout=fetch_timeout)
        s["fetched"] = ok
        if not ok:
            s["fetch_error"] = (err.splitlines() or ["failed"])[-1][:200]
    s["fetch_age"] = _last_fetch_age(vault)
    ok, counts, err = git(vault, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if not ok:
        s["problem"] = "could not compare with %s (%s)" % (up, err[:120])
        return s
    a, b = counts.split()
    s["ahead"], s["behind"] = int(a), int(b)
    return s


# ------------------------------------------------------------------ verbs ----

def _emit(a, data: dict, lines: list[str]) -> None:
    if a.json:
        print(json.dumps(data, indent=1))
    else:
        print("\n".join(lines))


def do_status(a, vault: Path) -> int:
    s = state(vault, fetch=not a.no_fetch)
    if s["problem"]:
        _emit(a, s, ["gt-sync: %s: %s" % (vault, s["problem"])])
        return 2 if not s["repo"] else 1
    lines = ["gt-sync: %s on %s -> %s: %d ahead, %d behind; %s uncommitted change(s); %s"
             % (vault, s["branch"], s["upstream"], s["ahead"], s["behind"],
                s["dirty"] if s["dirty"] is not None else "?",
                ("fetch FAILED: %s" % s["fetch_error"]) if s["fetch_error"]
                else _age(s["fetch_age"]))]
    if s["behind"] and s["ahead"]:
        lines.append("  DIVERGED: both sides have commits. Nothing here merges or rebases; "
                     "reconcile by hand in %s." % vault)
    elif s["behind"]:
        lines.append("  pull with: /gt:gt-sync pull")
    elif s["ahead"]:
        lines.append("  push with: /gt:gt-sync push")
    _emit(a, s, lines)
    return 0


def do_pull(a, vault: Path) -> int:
    s = state(vault, fetch=not a.dry_run)
    if s["problem"]:
        _emit(a, s, ["gt-sync pull: REFUSED -- %s" % s["problem"]])
        return 2 if not s["repo"] else 1
    if s["fetch_error"]:
        _emit(a, s, ["gt-sync pull: could not fetch %s: %s" % (s["upstream"], s["fetch_error"])])
        return 1
    if s["ahead"] and s["behind"]:
        s["result"] = "diverged"
        _emit(a, s, ["gt-sync pull: STOPPED -- the vault has DIVERGED from %s: %d local "
                     "commit(s) not on %s, %d commit(s) there not here. A fast-forward is "
                     "impossible, and this tool never merges or rebases. Reconcile by hand "
                     "(e.g. `git -C \"%s\" rebase %s` after reading both sides), then pull "
                     "again." % (s["upstream"], s["ahead"], s["upstream"], s["behind"], vault,
                                 s["upstream"])])
        return 1
    if not s["behind"]:
        s["result"] = "up-to-date"
        _emit(a, s, ["gt-sync pull: already up to date with %s%s"
                     % (s["upstream"], " (as of the refs on disk; --dry-run does not fetch)"
                        if a.dry_run else "")])
        return 0
    if a.dry_run:
        s["result"] = "would-pull"
        _emit(a, s, ["gt-sync pull: would run `git -C \"%s\" pull --ff-only` -- %d commit(s) "
                     "behind %s (as of the refs on disk)" % (vault, s["behind"], s["upstream"])])
        return 0
    ok, before, _ = git(vault, "rev-parse", "HEAD")
    ok, out, err = git(vault, "pull", "--ff-only", "--quiet", timeout=NET_TIMEOUT)
    if not ok:
        s["result"] = "failed"
        _emit(a, s, ["gt-sync pull: git refused the fast-forward: %s"
                     % ((err or out).splitlines() or ["(no message)"])[-1]])
        return 1
    _, stat, _ = git(vault, "diff", "--stat", "%s..HEAD" % before)
    _, files, _ = git(vault, "diff", "--name-only", "%s..HEAD" % before)
    _, subject, _ = git(vault, "log", "-1", "--format=%s")
    s.update({"result": "pulled", "commits": s["behind"],
              "files_changed": len([f for f in files.splitlines() if f.strip()]),
              "newest": subject})
    _emit(a, s, ["gt-sync pull: fast-forwarded %d commit(s) from %s -- %d file(s) changed; "
                 "newest: %s" % (s["behind"], s["upstream"], s["files_changed"], subject)]
          + (["  " + stat.splitlines()[-1].strip()] if stat else []))
    return 0


def _push_check(vault: Path):
    """-> (passed, text). The push check's own verdict: it must say AHEAD of an upstream."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_push_check                                      # noqa: PLC0415
    except Exception as exc:
        return False, "gt_push_check.py could not be loaded (%s)" % exc.__class__.__name__
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            gt_push_check.report(str(vault))
    except Exception as exc:
        return False, "the push check crashed (%s)" % exc.__class__.__name__
    text = buf.getvalue().strip()
    return ("AHEAD of" in text), text


def do_push(a, vault: Path) -> int:
    s = state(vault, fetch=not a.dry_run)
    if not s["repo"]:
        _emit(a, s, ["gt-sync push: REFUSED -- %s" % s["problem"]])
        return 2
    if s["fetch_error"]:
        _emit(a, s, ["gt-sync push: could not fetch %s first: %s" % (s["upstream"],
                                                                     s["fetch_error"])])
        return 1
    if not s["problem"] and s["ahead"] == 0:
        s["result"] = "nothing-to-push"
        _emit(a, s, ["gt-sync push: nothing to push -- in step with %s" % s["upstream"]])
        return 0
    passed, text = _push_check(vault)
    s["push_check"] = text
    if s["problem"] or not passed:
        s["result"] = "refused"
        _emit(a, s, ["gt-sync push: REFUSED -- the push check did not pass:"]
              + ["  " + x for x in text.splitlines()]
              + (["  (%s)" % s["problem"]] if s["problem"] else []))
        return 1
    if s["behind"]:
        s["result"] = "refused"
        _emit(a, s, ["gt-sync push: REFUSED -- %s has %d commit(s) this machine lacks; "
                     "run /gt:gt-sync pull first (a push would be rejected or need a merge)"
                     % (s["upstream"], s["behind"])])
        return 1
    if a.dry_run:
        s["result"] = "would-push"
        _emit(a, s, ["gt-sync push: push check passed; would run `git -C \"%s\" push` -- %d "
                     "commit(s) to %s" % (vault, s["ahead"], s["upstream"])])
        return 0
    ok, out, err = git(vault, "push", "--quiet", timeout=NET_TIMEOUT)
    if not ok:
        s["result"] = "failed"
        _emit(a, s, ["gt-sync push: git push FAILED: %s"
                     % ((err or out).splitlines() or ["(no message)"])[-1]])
        return 1
    s["result"] = "pushed"
    _emit(a, s, ["gt-sync push: pushed %d commit(s) to %s" % (s["ahead"], s["upstream"])])
    return 0


def behind_line(vault: Path | str | None, mode: str) -> str | None:
    """SessionStart: one line, or None when the setting is off. Never raises."""
    if mode not in ("cached", "fetch") or not vault:
        return None
    try:
        v = Path(vault)
        s = state(v, fetch=(mode == "fetch"), fetch_timeout=HOOK_FETCH_TIMEOUT)
        if s["problem"]:
            return "GOLDEN THREAD sync: behind-check skipped -- %s." % s["problem"]
        when = ("fetch failed (%s); comparing with the refs on disk, %s"
                % (s["fetch_error"], _age(s["fetch_age"])) if s["fetch_error"]
                else _age(s["fetch_age"]))
        if s["behind"]:
            return ("GOLDEN THREAD sync: vault is %d commit(s) BEHIND %s (%s)%s -- pull with "
                    "/gt:gt-sync pull" % (s["behind"], s["upstream"], when,
                                          ", and DIVERGED" if s["ahead"] else ""))
        return "GOLDEN THREAD sync: vault not behind %s (%s)." % (s["upstream"], when)
    except Exception as exc:
        return "GOLDEN THREAD sync: behind-check could not run (%s)." % exc.__class__.__name__


def setting(name: str, fallback: str) -> str:
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_settings                                        # noqa: PLC0415
        return gt_settings.get(name) or fallback
    except Exception:
        return fallback


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_sync.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("status", "ahead/behind and uncommitted state"),
                      ("pull", "fetch, then fast-forward only"),
                      ("push", "push check, then push"),
                      ("behind", "the SessionStart line (sync_check setting)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--vault")
        p.add_argument("--json", action="store_true")
        p.add_argument("--dry-run", action="store_true",
                       help="no fetch, no pull, no push: report from the refs on disk")
        if name == "status":
            p.add_argument("--no-fetch", action="store_true")
        if name == "behind":
            p.add_argument("--mode", choices=("cached", "fetch"),
                           help="default: the sync_check setting")
    a = ap.parse_args(argv)
    vault = find_vault(a.vault)
    if vault is None:
        print("gt-sync: no vault -- pass --vault", file=sys.stderr)
        return 2
    if a.cmd == "behind":
        mode = a.mode or setting("sync_check", "off")
        if a.dry_run and mode == "fetch":
            mode = "cached"
        line = behind_line(vault, mode)
        print(line or "GOLDEN THREAD sync: behind-check is off (sync_check).")
        return 0
    if a.cmd == "status":
        if a.dry_run:
            a.no_fetch = True
        return do_status(a, vault)
    return do_pull(a, vault) if a.cmd == "pull" else do_push(a, vault)


if __name__ == "__main__":
    raise SystemExit(main())
