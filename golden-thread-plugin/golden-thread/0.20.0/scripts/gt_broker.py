#!/usr/bin/env python3
"""gt_broker.py -- apply the vault write queue: one pass, in order, then exit.

    gt_broker.py drain  [--vault V] [--dry-run] [--json]
    gt_broker.py status [--vault V] [--dry-run] [--json]
    gt_broker.py audit  [--vault V] [--since HOURS] [--dry-run] [--json]

`gt_write_queue.py` puts write requests in `<vault>/Projects/golden-thread/spool/queue/`. This
applies them, oldest first, and decides each one exactly once. It is not a daemon and wires no
hook: nothing drains the queue until someone runs `drain`, and a queue nobody drains blocks
nobody -- requests wait. `gt_surface.py` counts them at session start so they are not forgotten.

THE DECISIONS, each logged as one JSON line to
`<vault>/Projects/golden-thread/spool/broker/log-<YYYY-MM-DD>.jsonl` (never `spool/log/`: that
folder is gt_log.py's, and log.md is generated from it):

    apply        written. append (any number, from any sessions, in timestamp order), create
                 of a file that does not exist, replace-section of a section nobody changed
                 since the request was made
    deduplicate  not written, because it is already there: an append whose text is >= 90%
                 token-identical to a block already in the section, a create whose file
                 already holds exactly that content, or a replace-section another request in
                 the same pass contains in full (a strict superset wins)
    escalate     not written; a task for the owner instead. Two sessions replacing one
                 section with neither a superset of the other; a replace-section of a section
                 that changed since it was requested; a create over different content; any
                 write to a design.md or global-memory/ (review targets); and any append or
                 replace from a farmed (external) result. Every version goes into
                 `spool/broker/conflicts/<id>.md` and the task, made with the vault's own
                 gt_task.py and tagged #conflict, points at that file -- content never goes in
                 the task line
    held         left queued: another LIVE session holds a claim on the target (Core rule 1,
                 asked of the vault's gt_session.py), the file changed under the broker, or the
                 conflict task could not be made. The next drain tries again
    reject       malformed or not allowed (see gt_write_queue.py); the request file moves to
                 `spool/broker/rejected/`, so nothing is deleted

AUDIT -- a detection aid, not a guard. `audit` lists vault .md files in the queue-governed scope
whose mtime falls in the last --since hours (default 24) and that have no broker `apply` row
within 5 s of that mtime: content that changed by some path other than the broker. It is
read-only (it writes nothing, logs nothing; --dry-run is accepted and changes nothing) and
always exits 0. It CANNOT tell who wrote a file -- macOS FSEvents does not identify the writing
process -- so the owner's own Obsidian edits appear in it too, as do files a tool writes directly
by design (a #conflict task the broker raises through gt_task.py, for one). It is a list to glance
at, not an alarm. Out of scope, as for the guard: dot-folders (.obsidian/ .git/ .gt/ .trash/),
Sources/, core-rules/, Projects/golden-thread/{spool,sessions,tools}/, and the generated files
log.md, TASKS.md, any decisions.md and any review-queue.md.

HOW THIS RELATES TO ADR-8. ADR-8's broker is the (deferred) write path for third-party
extensions, and it limits auto-apply to a "closed class" a deterministic check fully decides.
This is not that broker and not a second one: it serialises writes from the owner's own
sessions, which ADR-2 scopes as trusted, and it accepts no request from an extension at all.
Its auto-apply class is closed in the same sense -- every decision above is a deterministic
comparison of text and hashes, and anything a check cannot decide (contradiction, review
targets, externally authored free text) goes to a human.

THE SANDBOX INBOX (0.20.0). Under gt sandbox mode, gt_write_queue.py run from Claude's shell leaves
its request in ~/.gt-inbox/queue/ (the one folder the sandbox lets it write). Each `drain` first
moves this vault's inbox requests into the queue -- only regular files with one link, addressed
to this vault, passing gt_write_queue's validation; the rest go to
spool/broker/rejected/inbox-*.json (at most INBOX_REJECT_KEEP copies; past that, logged and
deleted). Each file is opened once, relative to an O_NOFOLLOW handle on the inbox folder, with
O_NOFOLLOW, checked on the open descriptor and read once, bounded; those bytes are all that is
ever used, so a writer racing the broker cannot swap in a symlink, a hard link or a FIFO (see
_Inbox). A symlinked inbox folder is not read. The broker stamps every inbox request origin
"inbox" (a body's "farm" is kept: it is stricter) and session "unknown-inbox": a sandboxed writer
cannot speak for a session, so no claim holder's permission covers its request. `status` counts
the inbox.

Exit: 0 every request decided (or nothing queued) | 1 something left queued, or another drain
is running | 2 usage or no vault.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gt_write_queue as wq                                     # noqa: E402  the ONE format

try:
    import fcntl
except ImportError:                                             # pragma: no cover
    fcntl = None

BROKER_REL = Path("Projects") / "golden-thread" / "spool" / "broker"
TOOLS_REL = Path("Projects") / "golden-thread" / "tools"
NEAR_DUP = 0.90
REVIEW_NAMES = {"design.md"}
REVIEW_PREFIXES = ("global-memory/",)


# ------------------------------------------------------------------------ helpers ----

def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _blocks(body: str) -> list[str]:
    """Paragraphs and single lines: an append is compared with both, so a restated bullet is
    caught inside a list paragraph."""
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    lines = [ln for ln in body.split("\n") if ln.strip()]
    return paras + lines


def near_duplicate(new: str, body: str) -> bool:
    t = _tokens(new)
    if not t:
        return True
    for block in _blocks(body):
        b = _tokens(block)
        if b and difflib.SequenceMatcher(None, t, b, autojunk=False).ratio() >= NEAR_DUP:
            return True
    return False


def _lineset(text: str) -> set:
    return {ln.strip() for ln in text.split("\n") if ln.strip()}


def _read(path: Path):
    """-> (text or None if absent, sha of bytes or None)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, None
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def _gap(prev: str, new: list) -> list:
    """A blank line between the old text and the appended block -- except a list item after a
    list item, which would otherwise turn a tight list loose."""
    return [] if (LIST_ITEM.match(prev) and LIST_ITEM.match(new[0])) else [""]


def _render_append(lines, section, content):
    new = content.strip("\n").split("\n")
    if section is None:
        out = list(lines)
        while out and not out[-1].strip():
            out.pop()
        return out + (_gap(out[-1], new) if out else []) + new
    rng = wq.find_section(lines, section)
    if rng is None:
        out = list(lines)
        while out and not out[-1].strip():
            out.pop()
        return out + ([""] if out else []) + ["## " + section, ""] + new
    h, end = rng
    last = end - 1
    while last > h and not lines[last].strip():
        last -= 1
    tail = lines[end:]
    gap = _gap(lines[last], new) if last > h else [""]
    return lines[:last + 1] + gap + new + ([""] if tail else []) + tail


def _render_replace(lines, section, content):
    rng = wq.find_section(lines, section)
    if rng is None:
        return _render_append(lines, section, content)
    h, end = rng
    tail = lines[end:]
    return lines[:h + 1] + [""] + content.strip("\n").split("\n") + ([""] if tail else []) + tail


def _refusal(path, exc):
    """gt_write_probe.eperm_message: one line, the interpreter and the fix (0.20.0)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import gt_write_probe
        return gt_write_probe.eperm_message(path, exc)
    except Exception:                                            # noqa: BLE001
        return "the OS refused the write (%s) running %s" % (exc, sys.executable)


def _atomic_replace(path: Path, text: str, expect_sha: str | None) -> bool:
    """Write text over path unless the file changed since it was read (expect_sha; None means
    it must still not exist). -> False if it changed: the caller holds the request."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".gt_broker.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        if _read(path)[1] != expect_sha:
            os.unlink(tmp)
            return False
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -------------------------------------------------------------------------- claims ----

def my_session() -> str | None:
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    return None


def claim_holders(vault: Path, rel: str, req_sid: str, allowed: set):
    """-> (holders, error). holders: LIVE sessions other than `allowed` claiming rel.

    Asked of the vault's own gt_session.py -- the tool that owns what LIVE means (pid on this
    machine, else heartbeat). A vault without it falls back to gt_demote's parser of the same
    session files. Any failure to ask is an error, and an error HOLDS the request: waiting one
    drain is cheap, writing over a live session's file is the failure Core rule 1 exists for.
    """
    tool = vault / TOOLS_REL / "gt_session.py"
    if tool.is_file():
        cmd = [sys.executable, str(tool), "--vault", str(vault)]
        if req_sid:
            cmd += ["--id", req_sid]
        try:
            p = subprocess.run(cmd + ["check", rel], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], "gt_session.py could not run (%s)" % exc.__class__.__name__
        if p.returncode not in (0, 1):
            return [], "gt_session.py check exited %d" % p.returncode
        holders = []
        for line in p.stdout.splitlines():
            m = re.match(r"^LIVE\s+.*\s+held by (\S+)", line)
            if m and m.group(1) not in allowed:
                holders.append(m.group(1))
        return holders, None
    try:
        import gt_demote                                        # noqa: PLC0415
        claims, unreadable = gt_demote._claim_rows(str(vault))
    except Exception as exc:                                    # noqa: BLE001
        return [], "session files could not be read (%s)" % exc.__class__.__name__
    if unreadable:
        return [], "%d session file(s) unreadable" % len(unreadable)
    return [sid for sid, status, last, files in claims
            if rel in files and sid not in allowed and gt_demote._is_live(status, last)], None


# ---------------------------------------------------------------------- escalation ----

def _task_project(vault: Path, rel: str):
    """-> ("--project", slug) | ("--inbox", None) | None. The target's own project when it has
    a README (sub-project first), else golden-thread's, else the inbox."""
    parts = rel.split("/")
    if parts[0] == "Projects" and len(parts) >= 3:
        if len(parts) >= 4 and (vault / "Projects" / parts[1] / parts[2] / "README.md").is_file():
            return ("--project", "%s/%s" % (parts[1], parts[2]))
        if (vault / "Projects" / parts[1] / "README.md").is_file():
            return ("--project", parts[1])
    if (vault / "Projects" / "golden-thread" / "README.md").is_file():
        return ("--project", "golden-thread")
    if (vault / "INBOX.md").is_file():
        return ("--inbox", None)
    return None


def _fence(text: str) -> str:
    n = max([3] + [len(m) + 1 for m in re.findall(r"`{3,}", text)])
    return "`" * n


def _clean(s: str, n: int = 80) -> str:
    s = re.sub(r"[\[\]\n\r`|]", " ", s or "")
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 3] + "..."


def write_conflict(vault: Path, reqs: list, reason: str, current: str | None) -> str:
    """The versions, in full, in a file the task points at. Named from the request ids so a
    retry overwrites rather than duplicates. -> vault-relative path."""
    cid = hashlib.sha1("\n".join(r["id"] for r in reqs).encode()).hexdigest()[:12]
    first = reqs[0]
    rel = BROKER_REL / "conflicts" / ("%s-%s.md" % (_now().strftime("%Y-%m-%d"), cid))
    old = sorted((vault / BROKER_REL / "conflicts").glob("*-%s.md" % cid))
    if old:
        rel = old[0].relative_to(vault).as_posix()
    out = ["# Write conflict: %s%s" % (first["path"],
                                       (" § " + first["section"]) if first.get("section") else ""),
           "", "- reason: %s" % reason, "- detected: %s" % _now().isoformat(timespec="seconds"),
           "- target: `%s`" % first["path"], "",
           "Nothing below has been written to the target. Decide which version stands (or merge "
           "them), make the edit yourself, then close the #conflict task.", ""]
    if current is not None:
        f = _fence(current)
        out += ["## Current content of the target", "", f, current, f, ""]
    for i, r in enumerate(reqs, 1):
        f = _fence(r["content"])
        out += ["## Version %d -- session %s (%s, %s)" % (i, r["session"], r["origin"], r["op"]),
                "", "- submitted: %s" % r["submitted"], "- request: `%s`" % r["id"]]
        if r.get("hint"):
            out.append("- hint: %s" % _clean(r["hint"], 200))
        out += ["", f, r["content"].rstrip("\n"), f, ""]
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes("\n".join(out).encode("utf-8"))
    return str(rel).replace(os.sep, "/")


def make_task(vault: Path, reqs: list, conflict_rel: str, reason: str):
    """-> (ok, message). Through the vault's gt_task.py: one store, one format, and its own
    Core-rule-1 check on the README it writes."""
    tool = vault / TOOLS_REL / "gt_task.py"
    where = _task_project(vault, reqs[0]["path"])
    if not tool.is_file() or where is None:
        return False, "no gt_task.py or no README/INBOX to put the task in"
    first = reqs[0]
    text = "#conflict write broker held %d write(s) to %s%s -- %s; review the versions" % (
        len(reqs), _clean(first["path"], 90),
        (" section " + _clean(first["section"], 40)) if first.get("section") else "",
        _clean(reason, 90))
    cmd = [sys.executable, str(tool), "add", text, "--vault", str(vault),
           "--ref", conflict_rel]
    cmd += ["--inbox"] if where[0] == "--inbox" else ["--project", where[1], "--p", "1",
                                                      "--waiting", "user"]
    # GT_BROKER_APPLYING: gt_task.py queues its own writes (0.17.11) and drains them -- but this
    # drain holds the lock, so a conflict task queued from here would wait for the NEXT drain.
    # The broker is the writer of record, so the task it raises is written directly.
    env = dict(os.environ, GT_BROKER_APPLYING="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "gt_task.py could not run (%s)" % exc.__class__.__name__
    if p.returncode != 0:
        return False, "gt_task.py refused: %s" % (p.stderr.strip().splitlines() or ["?"])[-1]
    return True, (p.stdout.strip().splitlines() or [""])[0]


# ---------------------------------------------------------------------------- drain ----

class Drain:
    def __init__(self, vault: Path, dry_run: bool):
        self.vault, self.dry = vault, dry_run
        self.results = []          # dicts, one per request
        self.left = 0

    # -- bookkeeping
    def record(self, req, decision, reason="", **extra):
        row = {"ts": _now().isoformat(timespec="seconds"), "request": req.get("id"),
               "path": req.get("path"), "section": req.get("section"), "op": req.get("op"),
               "session": req.get("session"), "origin": req.get("origin"),
               "decision": decision, "reason": reason}
        row.update(extra)
        self.results.append(row)
        if decision == "held":
            self.left += 1
        if self.dry:
            return
        d = self.vault / BROKER_REL
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ("log-%s.jsonl" % _now().strftime("%Y-%m-%d")), "a", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def done(self, req):
        if not self.dry:
            try:
                os.unlink(req["_file"])
            except FileNotFoundError:
                pass

    def reject(self, path: Path, req, why):
        self.record(req if isinstance(req, dict) else {"id": path.stem}, "reject", why)
        if not self.dry:
            dest = self.vault / BROKER_REL / "rejected"
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest / path.name)

    def escalate(self, reqs, reason, current=None):
        if self.dry:
            for r in reqs:
                self.record(r, "escalate", reason)
            return
        crel = write_conflict(self.vault, reqs, reason, current)
        ok, msg = make_task(self.vault, reqs, crel, reason)
        for r in reqs:
            if ok:
                self.record(r, "escalate", reason, conflict=crel, task=msg)
                self.done(r)
            else:
                self.record(r, "held", "escalation failed, retried next drain: %s" % msg,
                            conflict=crel)

    # -- the pass
    def load(self):
        reqs = []
        for p in wq.pending(self.vault):
            try:
                req = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self.reject(p, None, "unreadable request (%s)" % exc.__class__.__name__)
                continue
            why = wq.validate(req, self.vault)
            if why:
                self.reject(p, req, why)
                continue
            req["_file"] = p
            reqs.append(req)
        reqs.sort(key=lambda r: (r["submitted"], r["_file"].name))
        return reqs

    def held_by_claim(self, req) -> str | None:
        # M4 (0.20.x review): a request from the sandbox inbox speaks for no session -- the broker
        # stamped it INBOX_SESSION -- so no claim holder's permission covers it, not even that
        # of the session running this drain. Only a request a trusted writer queued (a session's
        # own gt_write_queue run, or the vault MCP server, which stamps its own session id) may
        # pass the claim of the session it names.
        if req.get("origin") == "inbox" or req.get("session") == wq.INBOX_SESSION:
            allowed = set()
        else:
            allowed = {req["session"]} | ({my_session()} if my_session() else set())
        holders, err = claim_holders(self.vault, req["path"], req["session"], allowed)
        if err:
            return "could not check claims, so not writing: %s" % err
        if holders:
            return ("claimed by live session %s (Core rule 1) -- left queued"
                    % ", ".join(sorted(set(holders))))
        return None

    def run(self):
        reqs = self.load()
        groups = {}
        for r in reqs:
            if r["op"] == "replace-section":
                groups.setdefault((r["path"], r["section"].strip().lower()), []).append(r)
        seen = set()
        for r in reqs:
            if r["id"] in seen:
                continue
            group = [r]
            if r["op"] == "replace-section":
                group = groups[(r["path"], r["section"].strip().lower())]
            seen.update(x["id"] for x in group)
            self.one(group)
        return self.results

    def one(self, group):
        r = group[0]
        rel = r["path"]
        name = rel.split("/")[-1]
        if name in REVIEW_NAMES or rel.startswith(REVIEW_PREFIXES):
            return self.escalate(group, "%s is a review target; the broker never writes it"
                                 % ("global-memory/" if rel.startswith(REVIEW_PREFIXES) else name))
        if any(x["origin"] == "farm" for x in group) and r["op"] != "create":
            return self.escalate(group, "a farmed (external) result may only create a new file; "
                                        "free text into existing content is never auto-applied "
                                        "(ADR-8)")
        why = self.held_by_claim(r)
        if why:
            for x in group:
                self.record(x, "held", why)
            return
        if r["op"] == "replace-section" and len(group) > 1:
            return self.replace_group(group)
        return self.apply(r)

    def replace_group(self, group):
        sessions = {x["session"] for x in group}
        if len(sessions) == 1:
            winner = group[-1]
            losers = group[:-1]
            why = "superseded by a later replace-section from the same session"
        else:
            sets = [_lineset(x["content"]) for x in group]
            winner = None
            for i in range(len(group) - 1, -1, -1):
                if all(sets[i] >= s for s in sets):
                    winner = group[i]
                    break
            if winner is None:
                path = self.vault / group[0]["path"]
                text, _ = _read(path)
                cur = wq.section_body(wq.split_lines(text), group[0]["section"]) if text else None
                return self.escalate(group, "%d sessions replaced the same section and no "
                                            "version contains the others" % len(sessions), cur)
            losers = [x for x in group if x is not winner]
            why = "contained in full by the winning replace-section %s" % winner["id"]
        if self.apply(winner, peers=group):
            for x in losers:
                self.record(x, "deduplicate", why)
                self.done(x)
        else:
            for x in losers:
                if not any(row["request"] == x["id"] for row in self.results):
                    self.record(x, "held", "waits on %s" % winner["id"])

    def apply(self, r, peers=None) -> bool:
        """Decide and write one request. `peers` is its replace-section group: if the winner
        has to be escalated, every version in the group goes to the owner together."""
        path = self.vault / r["path"]
        try:
            text, fsha = _read(path)
        except (OSError, UnicodeDecodeError) as exc:
            self.record(r, "held", "target unreadable (%s)" % exc.__class__.__name__)
            return False
        if text is None and r.get("target_existed") and r["op"] != "create":
            self.escalate([r], "%s existed when this %s was queued and is gone now -- moved or "
                          "deleted meanwhile; recreating it at the old path would split the note"
                          % (r["path"], r["op"]), None)
            return False
        lines = wq.split_lines(text) if text is not None else []
        if lines and lines[-1] == "":
            lines = lines[:-1]
        sec = r.get("section")
        if r["op"] == "create":
            if text is not None:
                if text.strip() == r["content"].strip():
                    self.record(r, "deduplicate", "the file already holds exactly this content")
                    self.done(r)
                    return True
                self.escalate([r], "create of a file that already exists with other content",
                              text)
                return False
            new = r["content"].rstrip("\n") + "\n"
        elif r["op"] == "append":
            body = (wq.section_body(lines, sec) if sec else text) or ""
            if near_duplicate(r["content"], body):
                self.record(r, "deduplicate", ">= %d%% token-identical to text already in the %s"
                            % (NEAR_DUP * 100, "section" if sec else "file"))
                self.done(r)
                return True
            new = "\n".join(_render_append(lines, sec, r["content"])) + "\n"
        elif r["op"] == "replace-file":
            if text is not None and text.strip() == r["content"].strip():
                self.record(r, "deduplicate", "the file already holds exactly this content")
                self.done(r)
                return True
            if wq.sha(text) != r.get("base_sha256"):
                self.escalate([r], "the file changed after this replace-file was requested, so "
                              "applying it would overwrite someone's edit", text)
                return False
            new = r["content"] if r["content"].endswith("\n") else r["content"] + "\n"
        elif r["op"] == "set-property":
            has_fm, cur = wq.frontmatter_get(text, r["key"])
            if wq.frontmatter_is_block(text, r["key"]):
                self.escalate([r], "%s became a YAML block after this request was made; "
                              "set-property cannot rewrite a block" % r["key"], text)
                return False
            if text is None or not has_fm:
                self.escalate([r], "set-property needs an existing file with frontmatter", text)
                return False
            if cur is not None and cur.strip() == r["content"].strip():
                self.record(r, "deduplicate", "%s is already %s" % (r["key"], r["content"]))
                self.done(r)
                return True
            if wq.sha(cur) != r.get("base_sha256"):
                self.escalate([r], "%s changed after this request was made, so setting it would "
                              "overwrite someone's edit" % r["key"], cur)
                return False
            new = wq.frontmatter_set(text, r["key"], r["content"].strip())
            if not new.endswith("\n"):
                new += "\n"
        else:
            cur = wq.section_body(lines, sec) if text is not None else None
            base = r.get("base_sha256")
            if cur is not None and cur.strip() == r["content"].strip():
                self.record(r, "deduplicate", "the section already holds exactly this content")
                self.done(r)
                return True
            if wq.sha(cur) != base:
                self.escalate(peers or [r], "the section changed after this replace was "
                              "requested, so applying it would overwrite someone's edit", cur)
                return False
            new = "\n".join(_render_replace(lines, sec, r["content"])) + "\n"
        if self.dry:
            self.record(r, "apply", "would write")
            return True
        try:
            written = _atomic_replace(path, new, fsha)
        except OSError as exc:
            if getattr(exc, "errno", None) not in (errno.EPERM, errno.EACCES):
                raise
            # The OS refused this interpreter (0.20.0): HELD, with one line naming it and the
            # fix -- never a traceback, and the request stays queued for the next drain.
            self.record(r, "held", _refusal(path, exc))
            return False
        if not written:
            self.record(r, "held", "the file changed while the broker was writing it")
            return False
        self.record(r, "apply", "written")
        self.done(r)
        return True


def _lock(vault: Path):
    q = wq.queue_dir(vault)
    q.mkdir(parents=True, exist_ok=True)
    fh = open(q / ".drain.lock", "a", encoding="utf-8", newline="\n")
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
    return fh


def _line(row, dry):
    verb = row["decision"]
    if dry:
        verb = "would " + {"held": "hold"}.get(verb, verb)
    where = row["path"] or row["request"]
    if row.get("section"):
        where += " § " + row["section"]
    tail = "  -> %s" % row["conflict"] if row.get("conflict") else ""
    return "%-12s %s  (%s, %s) %s%s" % (verb, where, row.get("op"), row.get("session"),
                                        row["reason"], tail)


# ------------------------------------------------------------------ the sandbox inbox ----
#
# gt sandbox mode (0.20.0): a command in Claude Code's sandbox cannot write the vault, so
# gt_write_queue.py leaves its request in ~/.gt-inbox/queue/ instead (deposit_inbox). That folder
# is the one place the sandbox lets Claude's shell write, which makes every file in it DATA from
# an untrusted writer. Each drain, which runs outside the sandbox, moves into this vault's queue
# only what passes every check below, and the request is then validated AGAIN, decided and
# logged like any other: claims hold it, conflicts escalate it, design.md / global-memory go to
# the owner. A file naming another vault is left for that vault's drain.

INBOX_MAX_BYTES = 2 * wq.MAX_CONTENT
# L3 (0.20.x review): a writer in the sandbox can make any number of bad requests, and every one
# used to be copied into the vault. At most INBOX_REJECT_KEEP rejected inbox copies are kept in
# spool/broker/rejected/; past that a rejected request is logged and deleted, not copied.
INBOX_REJECT_KEEP = 50
LINK_WHY = "not a regular file (a symlink, hard link, device or pipe is never read)"
# Test hook (0.20.x review H1): called as _INBOX_RACE_HOOK(stage, name) at the two points where a
# racing writer would swap something -- "dir" before the inbox folder is opened, "file" before
# each entry is opened -- so a regression test can make the swap happen exactly there instead of
# by luck. None in production.
_INBOX_RACE_HOOK = None


def _hook(stage, name=None):
    if _INBOX_RACE_HOOK is not None:
        _INBOX_RACE_HOOK(stage, name)


class _InboxEntry:
    __slots__ = ("name", "data", "body", "why", "link")

    def __init__(self, name, data=None, body=None, why=None, link=False):
        self.name, self.data, self.body, self.why, self.link = name, data, body, why, link


class _Inbox:
    """The queue inbox, opened so that nothing in it can be swapped under the broker (H1).

    POSIX: ~/.gt-inbox and its queue/ folder are each opened O_DIRECTORY|O_NOFOLLOW (a symlinked
    folder is refused, not followed); every entry is opened relative to that folder's fd with
    O_NOFOLLOW|O_NONBLOCK (a symlink fails to open, a FIFO cannot block), fstat'ed on the open fd
    (regular file, exactly one link -- a hard link to a read-denied file is refused unread --
    and within the size cap), and read at most cap+1 bytes from that fd. Those bytes are the only
    ones ever used, for the accept and the reject path alike, and the entry is unlinked through
    the folder fd. Nothing is ever re-opened by path.

    Windows has no O_NOFOLLOW and no dir_fd. There the folders are refused when they are links
    or reparse points, each entry is lstat'ed, opened, and its open handle must be the SAME file
    (st_dev/st_ino) as the lstat saw, a regular file with one link, whose final path
    (GetFinalPathNameByHandleW) is inside the inbox folder; anything else is refused unread.
    Native Windows has no Claude Code sandbox, so the inbox there is not a security boundary:
    Claude's shell can open any of your files directly (SECURITY.md)."""

    def __init__(self):
        self.q = wq.inbox_dir()
        self.dfd = None
        self.real = None
        self.ok = False

    def __enter__(self):
        _hook("dir")
        if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            self.ok = self._open_windows()
        else:
            self.ok = self._open_posix()
        return self

    def __exit__(self, *exc):
        if self.dfd is not None:
            os.close(self.dfd)
            self.dfd = None
        return False

    # -- opening the folder
    def _open_posix(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            pfd = os.open(str(self.q.parent), flags)
        except OSError:
            return False                              # missing, or ~/.gt-inbox is a symlink
        try:
            self.dfd = os.open(self.q.name, flags, dir_fd=pfd)
        except OSError:
            return False                              # missing, or queue/ is a symlink
        finally:
            os.close(pfd)
        return True

    def _open_windows(self):
        import stat as _st
        for d in (self.q.parent, self.q):
            try:
                st = os.lstat(str(d))
            except OSError:
                return False
            if not _st.S_ISDIR(st.st_mode) or _is_reparse(st):
                return False
        try:
            self.real = os.path.realpath(str(self.q))
        except OSError:
            return False
        return True

    def names(self):
        if not self.ok:
            return []
        try:
            got = os.listdir(self.dfd) if self.dfd is not None else os.listdir(str(self.q))
        except OSError:
            return []
        return sorted(n for n in got if n.endswith(".json") and not n.startswith(".")
                      and "/" not in n and "\\" not in n)

    # -- reading one entry
    def read(self, name) -> _InboxEntry:
        _hook("file", name)
        if self.dfd is not None:
            return self._read_posix(name)
        return self._read_windows(name)

    def _read_posix(self, name):
        import errno
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, dir_fd=self.dfd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1), getattr(errno, "EFTYPE", -1)):
                return _InboxEntry(name, why=LINK_WHY, link=True)
            if exc.errno == errno.ENOENT:
                return _InboxEntry(name, why="gone")
            return _InboxEntry(name, why="unreadable (%s)" % exc.__class__.__name__)
        try:
            return self._from_fd(name, fd, None)
        finally:
            os.close(fd)

    def _read_windows(self, name):
        import stat as _st
        p = os.path.join(str(self.q), name)
        try:
            lst = os.lstat(p)
        except OSError:
            return _InboxEntry(name, why="gone")
        if not _st.S_ISREG(lst.st_mode) or _is_reparse(lst):
            return _InboxEntry(name, why=LINK_WHY, link=True)
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
        except OSError as exc:
            return _InboxEntry(name, why="unreadable (%s)" % exc.__class__.__name__)
        try:
            return self._from_fd(name, fd, lst)
        finally:
            os.close(fd)

    def _from_fd(self, name, fd, lst):
        import stat as _st
        try:
            st = os.fstat(fd)
        except OSError as exc:
            return _InboxEntry(name, why="unreadable (%s)" % exc.__class__.__name__)
        if lst is not None:
            if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
                return _InboxEntry(name, why=LINK_WHY + "; it changed while being opened",
                                   link=True)
            fin = _final_path(fd)
            if fin is None or os.path.normcase(os.path.dirname(fin)) != os.path.normcase(self.real):
                return _InboxEntry(name, why=LINK_WHY + "; it resolves outside the inbox",
                                   link=True)
        if not _st.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return _InboxEntry(name, why=LINK_WHY, link=True)
        if st.st_size > INBOX_MAX_BYTES:
            return _InboxEntry(name, why="larger than %d bytes" % INBOX_MAX_BYTES)
        chunks, got = [], 0
        while got <= INBOX_MAX_BYTES:
            try:
                b = os.read(fd, INBOX_MAX_BYTES + 1 - got)
            except OSError as exc:
                return _InboxEntry(name, why="unreadable (%s)" % exc.__class__.__name__)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        data = b"".join(chunks)
        if len(data) > INBOX_MAX_BYTES:
            return _InboxEntry(name, why="larger than %d bytes" % INBOX_MAX_BYTES)
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return _InboxEntry(name, data=data, why="unreadable (%s)" % exc.__class__.__name__)
        return _InboxEntry(name, data=data, body=body)

    def unlink(self, name):
        try:
            if self.dfd is not None:
                os.unlink(name, dir_fd=self.dfd)
            else:
                os.unlink(os.path.join(str(self.q), name))
            return True
        except OSError:
            return False

    def where(self, name):
        return self.q / name


def _is_reparse(st) -> bool:
    import stat as _st
    if _st.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT


def _final_path(fd):
    """Windows: the path the OPEN handle really refers to, or None if it cannot be had."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        h = msvcrt.get_osfhandle(fd)
        f = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        f.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        f.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = f(h, buf, 1024, 0)
        if not n or n >= 1024:
            return None
        out = buf.value
        if out.startswith("\\\\?\\UNC\\"):
            out = "\\\\" + out[8:]
        elif out.startswith("\\\\?\\"):
            out = out[4:]
        return os.path.realpath(out)
    except Exception:                                           # noqa: BLE001
        return None


def _inbox_entries(vault: Path, box: _Inbox) -> list:
    """-> [(entry, why)] for inbox files addressed to THIS vault, plus the refused ones. An
    entry another vault's request is skipped (left for that vault's drain)."""
    try:
        mine = os.path.realpath(str(vault))
    except OSError:
        return []
    out = []
    for n in box.names():
        e = box.read(n)
        if e.why == "gone":
            continue
        if e.why is None:
            body = e.body
            if not (isinstance(body, dict) and body.get("gt_inbox") == 1
                    and isinstance(body.get("vault"), str) and isinstance(body.get("request"), dict)):
                e.why, e.body = "not a gt inbox request", None
            else:
                try:
                    if os.path.realpath(body["vault"]) != mine:
                        continue                        # another vault's: left for its own drain
                except (OSError, ValueError):
                    continue
        out.append(e)
    return out


def inbox_candidates(vault: Path) -> list:
    """-> [(path, body or None, why or None)] for inbox files addressed to THIS vault, plus the
    unreadable ones (body None). Reads only, through _Inbox: never follows a symlink."""
    with _Inbox() as box:
        return [(box.where(e.name), e.body, e.why) for e in _inbox_entries(vault, box)]


def _stamp_inbox(req: dict) -> dict:
    """M4 (0.20.x review): session and origin are the broker's to say, not the writer's. Any
    body value is replaced; a body that claimed "farm" keeps it, because that is only ever
    stricter (a farmed result may only create)."""
    req["origin"] = "farm" if req.get("origin") == "farm" else "inbox"
    req["session"] = wq.INBOX_SESSION
    return req


def _kept_rejects(dest: Path) -> int:
    try:
        return sum(1 for n in os.listdir(str(dest)) if n.startswith("inbox-"))
    except OSError:
        return 0


def pickup_inbox(vault: Path, dry_run: bool = False) -> list:
    """Move this vault's valid inbox requests into its queue; reject the rest into
    spool/broker/rejected/ (up to INBOX_REJECT_KEEP copies; past that, logged and deleted).
    -> [row] for what was refused, so the caller can report it; accepted requests are decided by
    the drain that follows. Every byte used is the one bounded read _Inbox made from the file
    it opened -- nothing here re-opens an inbox path."""
    rows = []
    with _Inbox() as box:
        for e in _inbox_entries(vault, box):
            req = e.body.get("request") if e.body else None
            why = e.why
            if why is None:
                _stamp_inbox(req)
                why = wq.validate(req, vault)
            if why is None:
                if not dry_run:
                    req.pop("_file", None)
                    wq._deposit_queue(vault, req)
                    box.unlink(e.name)
                continue
            if e.link:
                # Never followed and never read: the LINK is removed (its target is untouched).
                if not dry_run:
                    box.unlink(e.name)
                rows.append({"request": Path(e.name).stem, "path": None, "op": None,
                             "decision": "reject", "reason": "inbox: " + why +
                             "; the link was removed", "session": None, "origin": None})
                continue
            if e.data is None:
                # Too large (or unreadable): never copied into the vault; left for the owner.
                rows.append({"request": Path(e.name).stem, "path": None, "op": None,
                             "decision": "reject",
                             "reason": "inbox: %s; left in %s for you to remove"
                                       % (why, box.where(e.name)),
                             "session": None, "origin": None})
                continue
            isreq = isinstance(req, dict)
            def _s(v, n):                           # writer-chosen: bounded in the log
                return v[:n] if isinstance(v, str) else None
            row = {"ts": _now().isoformat(timespec="seconds"),
                   "request": _s(req.get("id"), 120) if isreq else Path(e.name).stem,
                   "path": _s(req.get("path"), 300) if isreq else None,
                   "section": None, "op": _s(req.get("op"), 40) if isreq else None,
                   "session": wq.INBOX_SESSION, "origin": "inbox",
                   "decision": "reject", "reason": "inbox: %s" % why}
            rows.append(row)
            if dry_run:
                continue
            d = vault / BROKER_REL
            dest = d / "rejected"
            dest.mkdir(parents=True, exist_ok=True)
            if _kept_rejects(dest) < INBOX_REJECT_KEEP:
                try:
                    (dest / ("inbox-" + e.name)).write_bytes(e.data)
                except OSError:
                    continue
            else:
                row["reason"] += ("; not kept -- %d rejected inbox requests are already in %s"
                                  % (INBOX_REJECT_KEEP, dest.relative_to(vault).as_posix()))
            box.unlink(e.name)
            with open(d / ("log-%s.jsonl" % _now().strftime("%Y-%m-%d")), "a", encoding="utf-8",
                      newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def cmd_drain(a, vault: Path) -> int:
    # An oversized inbox file is reported by `status`, never acted on, so it does not count.
    inbox = [c for c in inbox_candidates(vault) if not (c[2] or "").startswith("larger than")]
    if not wq.pending(vault) and not inbox:
        if a.json:
            print(json.dumps({"results": [], "left": 0}))
        return 0                                   # an empty queue drains silently
    lock = None
    if not a.dry_run:
        lock = _lock(vault)
        if lock is None:
            print("gt_broker: another drain is running; nothing done", file=sys.stderr)
            return 1
    try:
        inbox_rows = pickup_inbox(vault, a.dry_run)
        d = Drain(vault, a.dry_run)
        rows = inbox_rows + d.run()
    finally:
        if lock is not None:
            lock.close()
    if a.json:
        print(json.dumps({"results": rows, "left": d.left}, indent=1))
    else:
        for row in rows:
            print(_line(row, a.dry_run))
        counts = {}
        for row in rows:
            counts[row["decision"]] = counts.get(row["decision"], 0) + 1
        print("gt_broker: %d request(s): %s%s" % (
            len(rows), ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())),
            " (dry run: nothing written)" if a.dry_run else ""))
    return 1 if d.left else 0


def cmd_status(a, vault: Path) -> int:
    files = wq.pending(vault)
    oldest = files[0].name[:8] if files else None
    inbox = len(inbox_candidates(vault))
    if a.json:
        print(json.dumps({"pending": len(files), "oldest": oldest, "inbox": inbox,
                          "files": [f.name for f in files]}))
        return 0
    if inbox:
        print("%d request(s) waiting in the sandbox inbox %s (moved into the queue by the next "
              "drain)" % (inbox, wq.inbox_dir()))
    if files:
        print("%d write request(s) queued, oldest %s-%s-%s" % (
            len(files), oldest[:4], oldest[4:6], oldest[6:8]))
        for f in files:
            print("  " + f.name)
    else:
        print("write queue empty")
    return 0


# ---------------------------------------------------------------------------- audit ----

AUDIT_TOLERANCE = 5                     # seconds between a broker apply row and the file's mtime
AUDIT_EXEMPT_PREFIXES = ("Sources/", "core-rules/", "Projects/golden-thread/core-rules/",
                         "Projects/golden-thread/spool/", "Projects/golden-thread/sessions/",
                         "Projects/golden-thread/tools/")
AUDIT_GENERATED_NAMES = {"decisions.md", "review-queue.md"}
AUDIT_GENERATED_EXACT = {"log.md", "TASKS.md"}
AUDIT_HEADER = ("vault .md files changed outside the write broker -- the writer cannot be "
                "identified, so the owner's own Obsidian edits appear here too; a list to glance "
                "at, not an alarm")


def _audit_governed(rel: str) -> bool:
    parts = rel.split("/")
    if any(x.startswith(".") for x in parts):                   # .obsidian .git .gt .trash
        return False
    if rel.startswith(AUDIT_EXEMPT_PREFIXES) or rel in AUDIT_GENERATED_EXACT:
        return False
    return parts[-1] not in AUDIT_GENERATED_NAMES


def _parse_ts(ts):
    try:
        t = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.timestamp()


def _apply_times(vault: Path, since: float) -> dict:
    """path -> [epoch seconds of every broker `apply` row], from log days the window touches."""
    first_day = datetime.datetime.fromtimestamp(since - 86400, datetime.timezone.utc).strftime(
        "%Y-%m-%d")
    out = {}
    for f in sorted((vault / BROKER_REL).glob("log-*.jsonl")):
        if f.stem[4:] < first_day:
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("decision") != "apply":
                continue
            t = _parse_ts(row.get("ts"))
            if t is not None and row.get("path"):
                out.setdefault(row["path"], []).append(t)
    return out


def _git_states(vault: Path):
    """-> {rel: "modified" | "untracked" | ...} for changed files, or None when no git."""
    if not (vault / ".git").exists():
        return None
    try:
        p = subprocess.run(["git", "-C", str(vault), "status", "--porcelain", "-z",
                            "--untracked-files=all"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    states = {}
    entries = p.stdout.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        code, rel = e[:2], e[3:]
        if code[0] in "RC":
            i += 1                                              # skip the rename's source
        states[rel] = ("untracked" if code == "??" else "added" if "A" in code
                       else "deleted" if "D" in code else "modified")
    return states


def audit(vault: Path, since_hours: float, now: float | None = None) -> list:
    now = time.time() if now is None else now
    since = now - since_hours * 3600
    applies = _apply_times(vault, since)
    git = _git_states(vault)
    found = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = Path(root) / name
            rel = str(full.relative_to(vault)).replace(os.sep, "/")
            if not _audit_governed(rel):
                continue
            try:
                mtime = full.stat().st_mtime
            except OSError:
                continue
            if mtime < since:
                continue
            if any(abs(mtime - t) <= AUDIT_TOLERANCE for t in applies.get(rel, ())):
                continue
            found.append({"path": rel,
                          "mtime": datetime.datetime.fromtimestamp(
                              mtime, datetime.timezone.utc).isoformat(timespec="seconds"),
                          "git": None if git is None else git.get(rel, "clean")})
    found.sort(key=lambda r: r["mtime"], reverse=True)
    return found


def cmd_audit(a, vault: Path) -> int:
    rows = audit(vault, a.since)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    print("gt_broker audit: " + AUDIT_HEADER)
    for r in rows:
        print("  %s  %s%s" % (r["mtime"], r["path"],
                              "" if r["git"] is None else "  [git: %s]" % r["git"]))
    print("gt_broker audit: %d file(s) changed in the last %g h without a broker apply "
          "(read-only report)" % (len(rows), a.since))
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS,
                        help="the vault (default: $GT_VAULT, else vault-config.json)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="decide and report; write, log and remove nothing")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(prog="gt_broker.py", description=__doc__.split("\n\n")[0],
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("drain", help="apply every queued write once, then exit", parents=[common])
    sub.add_parser("status", help="how many writes are queued (read-only)", parents=[common])
    au = sub.add_parser("audit", parents=[common],
                        help="list vault .md files changed without a broker apply (read-only; "
                             "the owner's own edits appear too)")
    au.add_argument("--since", type=float, default=24.0, metavar="HOURS",
                    help="look back this many hours (default 24)")
    a = ap.parse_args(argv)
    a.vault = getattr(a, "vault", None)
    a.dry_run = getattr(a, "dry_run", False)
    a.json = getattr(a, "json", False)
    vault = wq.find_vault(a.vault)
    if vault is None:
        print("gt_broker: no vault found; pass --vault", file=sys.stderr)
        return 2
    if a.cmd == "audit":
        return cmd_audit(a, vault)
    return cmd_drain(a, vault) if a.cmd == "drain" else cmd_status(a, vault)


if __name__ == "__main__":
    raise SystemExit(main())
