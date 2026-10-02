#!/usr/bin/env python3
"""gt_minimize.py -- measure what THIS session is carrying, and whether its cache is still warm.

    gt_minimize.py [--session ID | --transcript FILE | --latest] [--projects-dir DIR] [--json]

The measuring half of /gt:gt-minimize. Read-only: it reads the session's own transcript and
writes nothing anywhere. The skill does the rest -- triage what is worth keeping (promoted to
Knowledge/ or INBOX.md through the usual tools), drop the rest, and tell the user to cut NOW.

WHY "NOW". A session idled past its cache lifetime rebuilds its whole prefix on the next turn,
and the price is set by how large the conversation was, not how long the break. Compacting while
the cache is still warm reads the prefix from cache and costs a fraction of the context size;
compacting after it expired pays the full rebuild first. So this reports, beside the size, how
many minutes of cache are left, from the TTL the last turn actually WROTE at (5 minutes or 1
hour), never a constant.

WHAT IT REPORTS

  context      billed context of the LAST turn: input + cache read + cache write, from the
               transcript's usage row -- a measurement, not a character estimate. The peak
               turn since the last compaction is shown beside it.
  breakdown    an ESTIMATE, labelled as one: characters / 4 for the conversation text, tool
               calls, tool output, and injected context (`attachment` and `system` rows carry
               hook output and skill bodies that ARE in context). Whatever the transcript does
               not account for -- the system prompt, tool definitions -- is reported as
               UNATTRIBUTED, never assigned to a guess. If the estimate exceeds the billed size,
               the residual is clamped at zero and said so.
  cache        minutes since the last turn against the TTL it wrote at: warm (N min left) or
               expired.

Only rows after the last `compact_boundary` count: before it is history the session no longer
carries.

FINDING THE SESSION. --transcript names the file; --session (or $CLAUDE_CODE_SESSION_ID /
$CLAUDE_SESSION_ID) finds <projects-dir>/*/<id>.jsonl; --latest takes the newest transcript and
says so. With none of those it refuses rather than guess which session "this" is.

NO IN-FLIGHT NOTE. An earlier design also wrote Projects/<slug>/inflight/<session>.md. The owner
ruled (2026-10-01) that carry-forward is /gt:gt-handoff's job; this measures and the skill prunes.

Output names a session by 8 characters at most and prints no absolute path.

Exit: 0 measured | 2 usage | 4 could not run (no transcript, or no usage in it)
"""
import argparse
import datetime
import glob
import json
import os
import sys

TTL_5M, TTL_1H = 5 * 60, 60 * 60
CHARS_PER_TOKEN = 4


def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _chars(value):
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def find_transcript(args):
    root = os.path.expanduser(args.projects_dir or "~/.claude/projects")
    if args.transcript:
        return args.transcript if os.path.isfile(args.transcript) else None, "named"
    sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") \
        or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        hits = glob.glob(os.path.join(root, "*", "%s.jsonl" % sid))
        return (hits[0] if hits else None), "session"
    if args.latest:
        files = glob.glob(os.path.join(root, "*", "*.jsonl"))
        return (max(files, key=os.path.getmtime) if files else None), "latest"
    return None, "none"


def measure(path, now=None):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue                     # a live session's half-written last line
            if isinstance(row, dict):
                rows.append(row)
    last_compact = max((i for i, r in enumerate(rows)
                        if r.get("type") == "system" and r.get("subtype") == "compact_boundary"),
                       default=-1)
    rows = rows[last_compact + 1:]

    est = {"conversation": 0, "tool_calls": 0, "tool_output": 0, "injected": 0}
    turns, seen = [], set()
    session = ""
    for r in rows:
        session = session or str(r.get("sessionId") or "")
        t = r.get("type")
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        if t in ("attachment", "system"):
            est["injected"] += _chars(r.get("attachment") or r.get("content"))
            continue
        content = msg.get("content")
        if t in ("user", "assistant"):
            if isinstance(content, str):
                est["conversation"] += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "tool_result":
                        est["tool_output"] += _chars(block.get("content"))
                    elif kind == "tool_use":
                        est["tool_calls"] += _chars(block.get("input"))
                    else:
                        est["conversation"] += _chars(block.get("text") or block.get("thinking"))
        if t == "assistant" and isinstance(msg.get("usage"), dict):
            key = (r.get("requestId"), msg.get("id"))
            if key in seen and key != (None, None):
                continue
            seen.add(key)
            u = msg["usage"]
            cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
            turns.append({"ts": _ts(r.get("timestamp")),
                          "context": int(u.get("input_tokens") or 0)
                          + int(u.get("cache_read_input_tokens") or 0)
                          + int(u.get("cache_creation_input_tokens") or 0),
                          "w1h": int(cc.get("ephemeral_1h_input_tokens") or 0),
                          "w5m": int(cc.get("ephemeral_5m_input_tokens") or 0)})
    if not turns:
        return None
    last = turns[-1]
    billed = last["context"]
    tokens = {k: v // CHARS_PER_TOKEN for k, v in est.items()}
    attributed = sum(tokens.values())
    clamped = attributed > billed
    ttl = TTL_5M
    for t in turns:
        if t["w1h"]:
            ttl = TTL_1H
        elif t["w5m"]:
            ttl = TTL_5M
    now = now or datetime.datetime.now(datetime.timezone.utc)
    idle = (now - last["ts"]).total_seconds() if last["ts"] else None
    left = None if idle is None else max(0.0, ttl - idle)
    return {
        "session": session[:8],
        "turns_since_compaction": len(turns),
        "context_tokens": billed,
        "peak_context_tokens": max(t["context"] for t in turns),
        "estimate": {"label": "estimate: characters / %d" % CHARS_PER_TOKEN, **tokens,
                     "unattributed": max(0, billed - attributed),
                     "residual_clamped": clamped},
        "cache": {"ttl_minutes": ttl // 60,
                  "idle_minutes": None if idle is None else round(idle / 60, 1),
                  "minutes_left": None if left is None else round(left / 60, 1),
                  "warm": bool(left)},
    }


def render(m, how):
    e, c = m["estimate"], m["cache"]
    k = lambda n: "%.1fK" % (n / 1e3) if n >= 1000 else str(n)   # noqa: E731
    lines = ["session %s%s: %s tokens of context now (peak %s since the last compaction, "
             "%d turn(s))" % (m["session"] or "?", " (newest transcript)" if how == "latest"
                               else "", k(m["context_tokens"]),
                               k(m["peak_context_tokens"]), m["turns_since_compaction"]),
             "  where it goes -- an %s:" % e["label"]]
    for name in ("conversation", "tool_output", "tool_calls", "injected"):
        lines.append("    %-14s ~%s" % (name.replace("_", " "), k(e[name])))
    lines.append("    %-14s ~%s  (system prompt, tool definitions, anything the transcript "
                 "does not show)" % ("unattributed", k(e["unattributed"])))
    if e["residual_clamped"]:
        lines.append("    (the estimate exceeds the billed size; unattributed clamped at 0)")
    if c["warm"]:
        lines.append("  cache: WARM -- %.0f of %d minutes left. Cutting now reads the prefix "
                     "from cache: cheap." % (c["minutes_left"], c["ttl_minutes"]))
    else:
        lines.append("  cache: EXPIRED (idle %s min, TTL %d) -- the next turn rebuilds the whole "
                     "prefix whatever you do; cut before you step away next time."
                     % (c["idle_minutes"], c["ttl_minutes"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--session", help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    src.add_argument("--transcript", help="the transcript file itself")
    src.add_argument("--latest", action="store_true", help="the newest transcript (says so)")
    ap.add_argument("--projects-dir", help="transcripts root (default ~/.claude/projects)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path, how = find_transcript(args)
    if how == "none":
        print("which session? pass --session ID, --transcript FILE or --latest (no "
              "CLAUDE_CODE_SESSION_ID in the environment)", file=sys.stderr)
        return 2
    if not path:
        print("could not run: no transcript found for this session", file=sys.stderr)
        return 4
    m = measure(path)
    if m is None:
        print("could not run: the transcript has no usage rows since its last compaction",
              file=sys.stderr)
        return 4
    print(json.dumps({"version": 1, "how": how, **m}, indent=2) if args.json else render(m, how))
    return 0


if __name__ == "__main__":
    sys.exit(main())
