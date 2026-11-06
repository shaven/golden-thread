#!/usr/bin/env python3
"""gt_rules_once.py -- say the Core rules once, then a line (setting rules_injection, gt 0.21.4).

    gt_rules_once.py status [--session ID] [--json]     what the marker for a session holds

inject_core_rules.sh adds the full text of the Core rules (about 2.2K characters, 550 tokens) to
EVERY prompt, and each copy stays in the transcript. With `rules_injection compact` the hook calls
`decide()` here, and the full text goes out only when it has to:

    the first prompt of a session                          (no marker for the session id)
    a prompt whose predecessor never reached Claude        (the transcript holds no copy of the full text:
                                                            a blocked prompt is erased, and the rules it
                                                            carried went with it)
    the first prompt after a compaction                    (the transcript's newest compact boundary is
                                                            newer than its newest copy of the full text;
                                                            the SessionStart "compact" run also resets the
                                                            marker)
    every REFRESH_EVERY-th prompt                          (a rule said 20 prompts ago is not the
                                                            same as a rule in view; refresh bounds it)
    whenever the hook cannot tell                          (unreadable marker or transcript, no session id)
                                                            -- the safe direction is always the full text

Otherwise it sends one line: "Core rules unchanged (N rules, hash XXXX) — the full text is above; re-read if
unsure". The hash is the first four hex digits of a SHA-256 over every rule's rendered text, so a changed
rule changes it, and the full text carries it in its header in this mode (`CORE RULES (always on, ...;
hash XXXX):`). When the newest copy in the transcript carries a different hash than the rules have now
(a rule edited in the vault, a `parallel_max` that moved) the prompt gets the FULL text again. (The first design sent
the one line plus only the changed rules; but "which rules has Claude seen" is exactly what a blocked
prompt makes unknowable from a marker, and the transcript's own copy can say it exactly.)

THE EVIDENCE IS THE TRANSCRIPT, NOT THE MARKER. "The full text is above" must be TRUE when the line
says it. Claude Code records what a hook added as a `hook_additional_context` attachment row, with a
timestamp, beside the prompt that carried it; a prompt another hook blocked (the paste intake, the
expiry guard's backstop) leaves no such row. So `decide()` sends one line only when the transcript's
tail (the last 4 MB) holds a row with the full text that is newer than its newest compact boundary.
The marker keeps what the transcript cannot: how many one-line prompts since the full text.

WHAT THIS DOES NOT TOUCH (ADR-9). Core-rule ENFORCEMENT is not a setting and is not changed here.
The Stop validator (timestamp, secrets), the PreToolUse guards and the hook itself run on every turn
exactly as before; this changes only how often the REMINDER TEXT of the rules is repeated. For the
rules that have a validator that is irrelevant to whether they hold; for a Reminder-tier rule the
reminder IS the mechanism, which is why the default stays `full` and the choice is decided by a
measurement (dev/ctx_ab/, compact against full), not by this docstring. The hook also states the
mode on its one line so the state is visible each turn.

THE MARKER is one small JSON file per session id under ~/.claude/golden-thread/rules-seen/
($GT_RULES_SEEN_DIR overrides it for tests and the benchmark): when the full text was last sent, how
many compact lines since, and the set's hash. It holds no rule text and no prompt text. Files older
than 14 days are removed when a new one is written. Windows-safe, LF, owner-only (gt_private).

REFRESH_EVERY is 20 ($GT_RULES_REFRESH overrides it; the benchmark uses it to vary the interval).
Stdlib only; Python 3.8+. Nothing here raises into the hook: the caller wraps `decide` and falls back
to the full text on any exception.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_private  # noqa: E402

REFRESH_EVERY = 20
TAIL_BYTES = 4 * 1024 * 1024
MARKER_DAYS = 14
UNCHANGED = "Core rules unchanged (%d rules, hash %s) — the full text is above; re-read if unsure."
# The timestamp rule, spelled out on every compact line (owner, 2026-10-15): in the 90-run
# dev/ctx_ab A/B compact cost 0.969x full per correct result, but replies opening with the
# timestamp fell from 93% to 84%. A deliberate exception to "never copy rule text into a hook":
# one clause, the rule whose drop the benchmark measured.
RULE5 = ("Rule 5: begin this reply — including any line before the first tool call — with the "
         "timestamp above.")


def seen_dir():
    return os.environ.get("GT_RULES_SEEN_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "golden-thread", "rules-seen")


def refresh_every():
    try:
        return max(1, int(os.environ.get("GT_RULES_REFRESH", REFRESH_EVERY)))
    except ValueError:
        return REFRESH_EVERY


def marker_path(session_id):
    token = gt_private.safe_token(session_id, 80)
    return os.path.join(seen_dir(), token + ".json") if token else None


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def rule_hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def set_hash(hashes):
    """-> four hex digits naming the whole set of rendered rules, order-independent."""
    blob = "\n".join("%s=%s" % (k, hashes[k]) for k in sorted(hashes))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:4]


def _iso(ts):
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    except (TypeError, ValueError):
        return None


FULL_MARKER = "CORE RULES (always on"            # the header inject_core_rules.sh opens the full text with


HASH_TAG = re.compile(r"CORE RULES \(always on[^)]*?hash ([0-9a-f]{4})\)")


def transcript_state(transcript_path):
    """-> (newest compact boundary, newest copy of the full text, that copy's hash): the first two an
    epoch time or None, the hash four hex digits or None; read from the last TAIL_BYTES of the
    transcript. None when it cannot be read. A copy of the full text is a `hook_additional_context`
    attachment row whose content holds FULL_MARKER."""
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()                       # drop the partial first line
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    boundary = full = tag = None
    for line in raw.splitlines():
        is_full = FULL_MARKER in line and "hook_additional_context" in line
        is_boundary = "compact_boundary" in line or "compactMetadata" in line
        if not (is_full or is_boundary):
            continue                                # most lines: no parse at all
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = _iso(row.get("timestamp")) if isinstance(row, dict) else None
        if ts is None:
            continue
        if is_boundary and (row.get("subtype") == "compact_boundary" or row.get("compactMetadata")):
            boundary = ts if boundary is None else max(boundary, ts)
        att = row.get("attachment")
        if is_full and isinstance(att, dict) and att.get("type") == "hook_additional_context":
            body = json.dumps(att.get("content"))
            if FULL_MARKER in body and (full is None or ts >= full):
                full = ts
                m = HASH_TAG.search(body)
                tag = m.group(1) if m else None
    return boundary, full, tag


def _save(path, doc):
    gt_private.write_private(path, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    gt_private.prune(os.path.dirname(path), max_age_days=MARKER_DAYS, max_bytes=8 * 1024 * 1024)


def context_tokens(transcript_path, tail_bytes=524288):
    """-> the session's current context size in tokens (the newest assistant reply's input + cache read +
    cache creation tokens), or None when the transcript is missing or holds no such reply. rules_injection
    `auto` (0.22.0) reads it; None means the full form."""
    try:
        with open(transcript_path, "rb") as fh:
            try:
                fh.seek(-tail_bytes, os.SEEK_END)
            except OSError:
                fh.seek(0)
            text = fh.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(text.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        u = ((row.get("message") or {}).get("usage")) if isinstance(row, dict) and row.get("type") == "assistant" else None
        if isinstance(u, dict):
            try:
                n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError):
                return None
            if n > 0:
                return n
    return None


def decide(payload, event, blocks, now=None):
    """Choose the form of this prompt's rules.

    payload  the hook's stdin JSON as a dict (session_id, transcript_path)
    event    "UserPromptSubmit" or "SessionStart"
    blocks   [(name, [line, ...])] -- each rule's rendered lines, in the order the full text lists them
    -> {"form": "full" | "compact", "lines": [...compact lines...], "why": str, "hash": str, "n": int}
    (`lines` is empty for "full"; `hash` is the tag the full text's header carries in this mode)"""
    now = time.time() if now is None else now
    hashes = {name: rule_hash("\n".join(lines)) for name, lines in blocks}
    digest = set_hash(hashes)
    out = {"form": "full", "lines": [], "why": "", "hash": digest, "n": len(blocks)}
    sid = str((payload or {}).get("session_id") or "")
    path = marker_path(sid) if sid else None
    if path is None:
        out["why"] = "no session id"
        return out

    def full(why):
        out["why"] = why
        _save(path, {"at": now, "prompts": 0, "hash": digest})
        return out

    if event != "UserPromptSubmit":
        return full("session start (the rules are re-sent after a compaction)")
    marker = _read(path)
    if not marker:
        return full("first prompt of the session")
    try:
        count = int(marker.get("prompts", 0))
    except (TypeError, ValueError):
        return full("unreadable marker")
    state = transcript_state((payload or {}).get("transcript_path"))
    if state is None:
        return full("cannot read the transcript")
    boundary, last_full, last_hash = state
    if last_full is None:
        return full("the transcript holds no copy of the full text (a blocked prompt, or a long gap)")
    if boundary is not None and boundary >= last_full:
        return full("first prompt after a compaction")
    if last_hash != digest:
        return full("the rules changed since the copy in the transcript")
    if count + 1 >= refresh_every():
        return full("refresh (%d prompts since the full text)" % (count + 1))
    marker.update(prompts=count + 1)
    _save(path, marker)
    out.update(form="compact", why="unchanged",
               lines=[UNCHANGED % (len(blocks), digest) + " (rules_injection: compact) " + RULE5])
    return out


def status(session_id, as_json=False):
    path = marker_path(session_id) if session_id else None
    doc = _read(path) if path else None
    if as_json:
        print(json.dumps({"session": session_id, "marker": doc}, sort_keys=True))
    elif not doc:
        print("no marker for this session: the next prompt carries the full Core rules")
    else:
        print("full text sent %s; %d compact line(s) since; hash %s; refresh every %d prompts"
              % (datetime.datetime.fromtimestamp(doc.get("at", 0)).strftime("%Y-%m-%d %H:%M"),
                 int(doc.get("prompts", 0)), doc.get("hash", "?"), refresh_every()))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_rules_once.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("status")
    p.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    p.add_argument("--json", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if a.cmd == "status":
        return status(a.session, a.json)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
