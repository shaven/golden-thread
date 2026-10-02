#!/usr/bin/env python3
"""gt_checkpoint.py -- progress checkpoints for gt's batch tools, so an interrupted run resumes.

    gt_checkpoint.py find  --tool scan|ingest|scan-language [--target PATH] [--vault V] [--json]
    gt_checkpoint.py show  FILE
    gt_checkpoint.py prune [--vault V] [--days 7] [--dry-run]

WHY THIS EXISTS (0.18.1, request 2026-09-24-batch-skill-checkpoint-resume). `/gt:gt-scan` and
`/gt:gt-ingest` work through lists. A session that ran out of context, was cancelled or crashed
part-way started the next run from item 1 -- and a session that hit context pressure could not
save its place and continue in a NEW session at all, which was the case that mattered. So the
batch tools write a checkpoint before the first item and after every item, and take
`--resume <checkpoint>`.

RESUME WORKS ACROSS SESSIONS. The request's draft scoped checkpoints to one session; the owner's
triage overruled it ("resume must work across sessions, else the motivation fails"). The session
id is recorded in the file name and the body for the reader, and `find` ignores it: any
checkpoint for the same tool and target is offered, newest first.

WHERE. `<vault>/Projects/golden-thread/spool/<tool>/<session>-<project>-<UTC stamp>.progress.json`
when a vault is known -- gt's own spool, written by the tool itself, never through the write
queue (the queue refuses spool/ by design: it belongs to gt's tools). With no vault, the same
layout under `~/.claude/golden-thread/spool/`.

FORMAT (JSON, written by atomic rename -- a temp file in the same directory, then os.replace --
so a crash never leaves a half-written checkpoint):

    {"schema": 1, "tool": "scan", "target": "/abs/path", "session": "...", "created": ISO,
     "updated": ISO, "args": {...}, "items": [...], "last_completed_index": N, "results": [...]}

`last_completed_index` is -1 before the first item. Items complete in order; `results[i]` is the
result of `items[i]`. A finished run deletes its checkpoint. A run WITHOUT `--resume` always
starts fresh, under a new file name, whatever checkpoints exist.

PRUNE removes checkpoints older than 7 days (by their `updated` time, else mtime). The vault's
`gt_session.py register` runs the same pruning at every session registration.

REHEARSAL HOOK. `GT_CHECKPOINT_ABORT_AFTER=N` makes a batch tool stop with exit 75 right after
its Nth item is checkpointed, leaving the checkpoint in place -- an interruption on demand, so
the resume path can be proven (the tests use it) without killing a process at the right moment.

Exit: 0 ok | 1 a checkpoint is unreadable or belongs to another tool | 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
SUFFIX = ".progress.json"
SPOOL_REL = Path("Projects") / "golden-thread" / "spool"
LOCAL_SPOOL = Path.home() / ".claude" / "golden-thread" / "spool"
TOOLS = ("scan", "scan-language", "ingest")
MAX_AGE_DAYS = 7
ABORTED = 75          # EX_TEMPFAIL: interrupted on purpose, checkpoint kept


class CheckpointError(Exception):
    pass


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _slug(text: str, n: int = 40) -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:n].strip("-")) or "x"


def session_id() -> str:
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    return "nosession"


def find_vault(explicit: str | None = None) -> Path | None:
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


def spool_dir(tool: str, vault: Path | None) -> Path:
    return (vault / SPOOL_REL / tool) if vault else (LOCAL_SPOOL / tool)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ckpt.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Checkpoint:
    """One batch run's progress. Create with `start`, reopen with `load`."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self._done_since_start = 0

    # -- construction -----------------------------------------------------------------
    @classmethod
    def start(cls, tool: str, target: str, items: list, vault: Path | None = None,
              args: dict | None = None, path: Path | None = None) -> "Checkpoint":
        if tool not in TOOLS:
            raise CheckpointError("unknown tool %r" % tool)
        now = _now()
        sid = session_id()
        if path is None:
            d = spool_dir(tool, vault)
            stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
            path = d / ("%s-%s-%s%s" % (_slug(sid, 12), _slug(os.path.basename(
                os.path.normpath(target)) or "root"), stamp, SUFFIX))
        data = {"schema": SCHEMA, "tool": tool, "target": str(target), "session": sid,
                "created": now.isoformat(timespec="seconds"),
                "updated": now.isoformat(timespec="seconds"),
                "args": args or {}, "items": list(items), "last_completed_index": -1,
                "results": []}
        ck = cls(Path(path), data)
        ck.save()
        prune(spool_dir(tool, vault))                 # opportunistic; never fatal
        return ck

    @classmethod
    def load(cls, path, tool: str | None = None) -> "Checkpoint":
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CheckpointError("no checkpoint at %s" % p)
        except (OSError, ValueError) as exc:
            raise CheckpointError("checkpoint %s is unreadable (%s)" % (p, exc.__class__.__name__))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise CheckpointError("checkpoint %s has an unsupported schema" % p)
        for k, t in (("items", list), ("results", list), ("last_completed_index", int)):
            if not isinstance(data.get(k), t):
                raise CheckpointError("checkpoint %s is malformed (%s)" % (p, k))
        if tool and data.get("tool") != tool:
            raise CheckpointError("checkpoint %s belongs to %r, not %r"
                                  % (p, data.get("tool"), tool))
        n = data["last_completed_index"]
        if n < -1 or n >= len(data["items"]) or len(data["results"]) != n + 1:
            raise CheckpointError("checkpoint %s is inconsistent (index %d, %d result(s), %d "
                                  "item(s))" % (p, n, len(data["results"]), len(data["items"])))
        return cls(p, data)

    # -- progress ---------------------------------------------------------------------
    @property
    def items(self) -> list:
        return self.data["items"]

    @property
    def results(self) -> list:
        return self.data["results"]

    @property
    def next_index(self) -> int:
        return self.data["last_completed_index"] + 1

    @property
    def total(self) -> int:
        return len(self.data["items"])

    def remaining(self):
        """-> [(index, item)] still to do, in order."""
        return list(enumerate(self.items))[self.next_index:]

    def save(self) -> None:
        self.data["updated"] = _now().isoformat(timespec="seconds")
        _atomic_write(self.path, self.data)

    def done(self, index: int, result) -> None:
        """Record item `index` as complete. Items complete strictly in order."""
        if index != self.next_index:
            raise CheckpointError("item %d completed out of order (next is %d)"
                                  % (index, self.next_index))
        self.data["results"].append(result)
        self.data["last_completed_index"] = index
        self.save()
        self._done_since_start += 1
        stop = os.environ.get("GT_CHECKPOINT_ABORT_AFTER")
        if stop and stop.isdigit() and self._done_since_start >= int(stop) \
                and self.next_index < self.total:
            print("gt-checkpoint: stopping after %d item(s) as GT_CHECKPOINT_ABORT_AFTER asked; "
                  "resume with --resume %s" % (self._done_since_start, self.path),
                  file=sys.stderr)
            raise SystemExit(ABORTED)

    def finish(self) -> None:
        """The run completed: the checkpoint has nothing left to protect."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        for d in (self.path.parent,):
            try:
                d.rmdir()                       # only succeeds when empty
            except OSError:
                pass

    def summary(self) -> str:
        return ("%s checkpoint for %s: %d of %d item(s) done, last updated %s"
                % (self.data.get("tool"), self.data.get("target"), self.next_index, self.total,
                   self.data.get("updated")))


# ------------------------------------------------------------------ finding / pruning ----

def _updated(p: Path) -> datetime.datetime:
    try:
        d = json.loads(p.read_text(encoding="utf-8")).get("updated")
        t = datetime.datetime.fromisoformat(d)
        return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc)


def find(tool: str, target: str | None = None, vault: Path | None = None) -> list[dict]:
    """Every live checkpoint for `tool` (and `target`), newest first. Session is ignored."""
    out = []
    want = os.path.realpath(target) if target else None
    for d in {spool_dir(tool, vault), spool_dir(tool, None)}:
        if not d.is_dir():
            continue
        for p in d.glob("*" + SUFFIX):
            try:
                ck = Checkpoint.load(p, tool)
            except CheckpointError:
                continue
            if want and os.path.realpath(ck.data.get("target", "")) != want:
                continue
            out.append({"path": str(p), "target": ck.data.get("target"),
                        "done": ck.next_index, "total": ck.total,
                        "session": ck.data.get("session"), "updated": ck.data.get("updated")})
    out.sort(key=lambda r: r["updated"] or "", reverse=True)
    return out


def prune(directory: Path, days: int = MAX_AGE_DAYS, dry_run: bool = False) -> list[Path]:
    """Delete checkpoints in `directory` older than `days`. Never raises."""
    gone = []
    try:
        if not directory.is_dir():
            return gone
        cutoff = _now() - datetime.timedelta(days=days)
        for p in directory.glob("*" + SUFFIX):
            try:
                if _updated(p) < cutoff:
                    if not dry_run:
                        p.unlink()
                    gone.append(p)
            except OSError:
                continue
    except Exception:
        pass
    return gone


# ------------------------------------------------------------------ CLI ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_checkpoint.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="checkpoints a tool could resume, newest first")
    f.add_argument("--tool", required=True, choices=TOOLS)
    f.add_argument("--target")
    f.add_argument("--vault")
    f.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="one checkpoint's progress")
    s.add_argument("file")
    pr = sub.add_parser("prune", help="delete checkpoints older than --days (default 7)")
    pr.add_argument("--vault")
    pr.add_argument("--days", type=int, default=MAX_AGE_DAYS)
    pr.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "show":
        try:
            print(Checkpoint.load(a.file).summary())
        except CheckpointError as exc:
            print("gt-checkpoint: %s" % exc, file=sys.stderr)
            return 1
        return 0
    vault = find_vault(a.vault)
    if a.cmd == "find":
        rows = find(a.tool, a.target, vault)
        if a.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print("A previous %s of %s was interrupted at item %d of %d (updated %s): %s"
                      % (a.tool, r["target"], r["done"], r["total"], r["updated"], r["path"]))
        return 0
    gone = []
    for tool in TOOLS:
        for d in {spool_dir(tool, vault), spool_dir(tool, None)}:
            gone += prune(d, a.days, a.dry_run)
    for p in gone:
        print("gt-checkpoint: %s %s" % ("would remove" if a.dry_run else "removed", p))
    if not gone:
        print("gt-checkpoint: nothing older than %d day(s)" % a.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
