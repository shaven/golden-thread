#!/usr/bin/env python3
"""gt_batch_ledger.py -- how many tool calls the model issued together (0.22.0, GAP-1 follow-up).

    gt_batch_ledger.py record --hook   PostToolBatch hook: appends ONE line per batch (size and tool names)
    gt_batch_ledger.py summary [--json] [--days N]   batches, calls, mean batch size, size histogram, turns
    gt_batch_ledger.py status          where the ledger is and how much it holds

THIS MEASURES ONLY. It does not enforce Core rule 8 (parallelise independent work). `PostToolBatch` fires once
after every call in a batch has resolved, so the cost of a serial batch is already paid when this runs; only a
pre-dispatch event could repair one, and Claude Code has none (open-gaps.md, GAP-1, re-checked 2026-10-27 against
Claude Code 2.1.312). What it gives is the number that was missing: "Core rule 8 is not working" was an anecdote
because nothing counted batch sizes. A turn of ten messages with one call each has a mean batch size of 1.0, a
fact that needs no judgement about whether it could have been higher.

What a line holds: the time (whole seconds), the number of calls, the tool NAMES (cleaned, at most 64 names of at
most 64 characters), whether the batch ran in the main thread or in a subagent, and a short hash of the prompt id
so batches of one turn can be grouped. It never holds a tool's input or response, a path, a command, the session id
or the agent id; the session file is named by a hash of the session id. Nothing is sent anywhere, and no model is
called.

The switch (owner ruling 2026-10-27): setting `batch_ledger`, `on` (default) or `off`, in ~/.claude/vault-config.json,
or the environment variable GT_BATCH_LEDGER=on|off, which wins. Off records nothing and creates no folder; the hook
still reads and throws away its input so the writer never meets a closed pipe, and exits 0 with no output. What was
recorded earlier is left alone (delete the folder to remove it). `status` and `summary` still work and say the ledger
is off. Only a clear `off` turns it off: a missing, unreadable or odd config means the default, on. Turning it off is
a privacy choice that needs no presence check; the read costs one small file read per batch.

Fails open: any error prints nothing and exits 0, so a broken ledger can never block a tool call. A session file
stops growing at MAX_FILE_BYTES. The hook's input carries every tool's response, so its size follows the work the
batch did; input over MAX_INPUT_BYTES is skipped rather than parsed, and the rest of it is still read and thrown
away (up to MAX_DRAIN_BYTES) so the writer never meets a closed pipe. Parsing 16 MB of JSON took about 20 ms here
(self-verified 2026-10-27), so the cap stays at 16 MB: a lower one would leave the biggest batches uncounted.

The folder does not grow without end: at most once a day (the mtime of a stamp file; stat calls only, no ledger
file is opened) `record` removes session files not written for KEEP_DAYS days and keeps at most MAX_FILES, the
newest. Tool names come from MCP servers, so a name that looks like a token (a run of over 40 letters and digits,
or a run of over 40 with separators that holds 8 or more digits) is dropped; the call still counts.
"""
import hashlib
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True

MAX_FILE_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_DRAIN_BYTES = 512 * 1024 * 1024     # over the cap, read and discard this much at most, then stop
KEEP_DAYS = 90
MAX_FILES = 2000
PRUNE_STAMP = ".pruned"
MAX_TOOLS = 64
MAX_NAME = 64
DEFAULT_DAYS = 30
NAME_JUNK = re.compile(r"[^A-Za-z0-9_.:-]")
TOKEN_RUN = re.compile(r"[A-Za-z0-9]{41,}")            # hex, or base64 once + / = are cleaned away
TOKEN_SEP_RUN = re.compile(r"[A-Za-z0-9_-]{41,}")      # base64url keeps - and _: then it is the digits that tell


SETTING = "batch_ledger"
ENV = "GT_BATCH_LEDGER"


def home():
    return os.path.expanduser("~")


def enabled():
    """False only when the ledger is clearly switched off: GT_BATCH_LEDGER=off, else `batch_ledger` = "off" in
    ~/.claude/vault-config.json (case and surrounding spaces ignored). A missing or unreadable file, a value that
    is not a string and any other word all mean the default, on. Never raises; one small file read."""
    try:
        v = os.environ.get(ENV)
        if v is None:
            with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
                cfg = json.load(fh)
            v = cfg.get(SETTING) if isinstance(cfg, dict) else None
        return not (isinstance(v, str) and v.strip().lower() == "off")
    except Exception:                                    # noqa: BLE001 -- a ledger must never block a tool call
        return True


def _drain():
    """Read and throw away what is left of stdin (up to MAX_DRAIN_BYTES) so the writer never meets a closed pipe."""
    try:
        drained = 0
        while drained < MAX_DRAIN_BYTES:
            got = sys.stdin.buffer.read(1024 * 1024)
            if not got:
                break
            drained += len(got)
    except Exception:                                    # noqa: BLE001
        pass


def folder():
    return os.path.join(home(), ".claude", "golden-thread", "state", "batch-ledger")


def _hash(text, n):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:n]


def _name(value):
    if not isinstance(value, str):
        return None
    cleaned = NAME_JUNK.sub("", value)
    if _token_shaped(cleaned):
        return None
    return cleaned[:MAX_NAME] or None


def _token_shaped(name):
    """True when a (cleaned) tool name holds what looks like a token rather than words: an unbroken run of over 40
    letters and digits, or a run of over 40 with - and _ in it that carries 8 or more digits. MCP tool names are
    words joined by _ and - (`mcp__plugin_gt-lotr_gt-lotr__call_consent`), so they pass."""
    if TOKEN_RUN.search(name):
        return True
    return any(sum(c.isdigit() for c in m.group(0)) >= 8 for m in TOKEN_SEP_RUN.finditer(name))


def row_for(ev, now=None):
    """-> the ledger line for a PostToolBatch input, or None when it is not one. Pure."""
    if not isinstance(ev, dict):
        return None
    calls = ev.get("tool_calls")
    if not isinstance(calls, list):
        return None
    calls = [c for c in calls if isinstance(c, dict) and isinstance(c.get("tool_name"), str)]   # a call has a name
    if not calls:
        return None
    names = []
    for c in calls:
        n = _name(c["tool_name"])
        if n and len(names) < MAX_TOOLS:
            names.append(n)
    prompt = ev.get("prompt_id")
    return {"ts": int(now if now is not None else time.time()), "n": len(calls), "tools": names,
            "agent": "sub" if ev.get("agent_id") else "main",
            "turn": _hash(prompt, 8) if isinstance(prompt, str) and prompt else ""}


def _file_for(ev):
    sid = ev.get("session_id") if isinstance(ev, dict) else None
    key = _hash(sid, 12) if isinstance(sid, str) and sid else "unknown"
    return os.path.join(folder(), key + ".jsonl")


def record(ev):
    """Append the line for one PostToolBatch input. -> True when a line was written. Never raises. Off writes nothing."""
    try:
        if not enabled():
            return False
        row = row_for(ev)
        if row is None:
            return False
        path = _file_for(ev)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            full = os.fstat(fd).st_size >= MAX_FILE_BYTES
            if not full:
                os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        _prune(os.path.dirname(path))                    # after the write, so this session's file is the newest
        return not full
    except Exception:                                    # noqa: BLE001 -- a ledger must never block a tool call
        return False


def _prune(d, now=None):
    """At most once a day: remove *.jsonl session files not written for KEEP_DAYS days, and keep at most MAX_FILES
    (the newest). Stat calls only; the stamp file's mtime says when it last ran. Never raises."""
    now = time.time() if now is None else now
    stamp = os.path.join(d, PRUNE_STAMP)
    try:
        if 0 <= now - os.stat(stamp).st_mtime < 86400:
            return False
    except OSError:
        pass
    try:
        os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600))
        os.utime(stamp, (now, now))
        found = []
        with os.scandir(d) as it:
            for e in it:
                if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                    found.append((e.stat(follow_symlinks=False).st_mtime, e.path))
        found.sort(reverse=True)
        cutoff = now - KEEP_DAYS * 86400
        for i, (mtime, p) in enumerate(found):
            if mtime < cutoff or i >= MAX_FILES:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return True
    except Exception:                                    # noqa: BLE001 -- a ledger must never block a tool call
        return False


def _rows(days=DEFAULT_DAYS):
    """-> [(file key, row)] for every readable line newer than `days`; damaged lines are skipped."""
    out = []
    cutoff = time.time() - days * 86400 if days else 0
    try:
        names = sorted(os.listdir(folder()))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(folder(), fn), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(r, dict) and isinstance(r.get("n"), int) and not isinstance(r.get("n"), bool)
                            and r["n"] > 0 and isinstance(r.get("ts"), int) and r["ts"] >= cutoff):
                        out.append((fn[:-6], r))
        except OSError:
            continue
    return out


def summarize(days=DEFAULT_DAYS):
    rows = _rows(days)
    hist, by_agent, turns, sessions = {}, {}, set(), set()
    calls = singles = 0
    for key, r in rows:
        n = r["n"]
        calls += n
        singles += 1 if n == 1 else 0
        bucket = str(n) if n < 10 else "10+"
        hist[bucket] = hist.get(bucket, 0) + 1
        a = r.get("agent") if r.get("agent") in ("main", "sub") else "main"
        s = by_agent.setdefault(a, {"batches": 0, "calls": 0})
        s["batches"] += 1
        s["calls"] += n
        sessions.add(key)
        if r.get("turn"):
            turns.add((key, r["turn"]))
    batches = len(rows)
    order = sorted(hist, key=lambda k: 10 if k == "10+" else int(k))
    return {"batches": batches, "calls": calls,
            "mean_batch_size": round(calls / batches, 2) if batches else 0.0,
            "single_call_batches": singles, "histogram": {k: hist[k] for k in order},
            "turns": len(turns), "sessions": len(sessions),
            "batches_per_turn": round(batches / len(turns), 2) if turns else 0.0,
            "by_agent": by_agent, "days": days, "enforces": False, "enabled": enabled()}


OFF_NOTE = ("The batch ledger is OFF (setting batch_ledger, or GT_BATCH_LEDGER=off): nothing new is recorded and no "
            "folder is created. What was recorded before is still here. Turn it back on with "
            "gt_settings.py set batch_ledger on.")


def _summary_text(d):
    if not d.get("enabled", True) and not d["batches"]:
        return OFF_NOTE + " Nothing has been recorded."
    if not d["batches"]:
        return ("no batches recorded yet (the PostToolBatch hook writes one line per batch of tool calls; "
                "nothing here is sent anywhere)")
    lines = ["Tool-call batches, last %d days (Core rule 8 measurement)" % d["days"],
             "  batches %d, tool calls %d, sessions %d, turns %d" % (d["batches"], d["calls"], d["sessions"], d["turns"]),
             "  mean batch size %.2f (1.00 means every call went alone)" % d["mean_batch_size"],
             "  batches that held one call: %d of %d" % (d["single_call_batches"], d["batches"]),
             "  size histogram: " + ", ".join("%s call(s): %d" % (k, v) for k, v in d["histogram"].items())]
    if d["turns"]:
        lines.append("  batches per turn %.2f" % d["batches_per_turn"])
    for a in ("main", "sub"):
        if a in d["by_agent"]:
            s = d["by_agent"][a]
            lines.append("  %s: %d batches, %d calls" % ("main thread" if a == "main" else "subagents", s["batches"], s["calls"]))
    if not d.get("enabled", True):
        lines.append(OFF_NOTE)
    lines.append("This measures how many calls were issued together. It does not enforce Core rule 8 and does not "
                 "judge whether a batch could have been larger (PostToolBatch fires after the batch has run).")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    try:
        if cmd == "record":
            if "--hook" not in argv:
                print("usage: gt_batch_ledger.py record --hook   (reads a PostToolBatch input on stdin)", file=sys.stderr)
                return 2
            if not enabled():                                # off: record nothing, but never leave the writer a closed pipe
                _drain()
                return 0
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raw = None                                   # skipped, but read to the end: no EPIPE for the writer
                _drain()
                return 0
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                return 0
            record(ev)
            return 0
        if cmd == "summary":
            days = DEFAULT_DAYS
            if "--days" in argv:
                try:
                    days = max(0, int(argv[argv.index("--days") + 1]))
                except (ValueError, IndexError):
                    print("--days wants a whole number", file=sys.stderr)
                    return 2
            d = summarize(days)
            print(json.dumps(d, indent=2) if "--json" in argv else _summary_text(d))
            return 0
        if cmd == "status":
            rows = _rows(0)
            sessions = {k for k, _ in rows}
            print("batch-ledger folder: %s\n%d session(s), %d batch(es) recorded; local only, names and sizes only"
                  % (folder(), len(sessions), len(rows)))
            if not enabled():
                print("The ledger is off (setting %s, or %s=off): nothing new is recorded." % (SETTING, ENV))
            return 0
    except Exception:                                    # noqa: BLE001
        return 0 if cmd == "record" else 1
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
