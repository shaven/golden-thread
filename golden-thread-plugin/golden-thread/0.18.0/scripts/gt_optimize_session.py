#!/usr/bin/env python3
"""gt_optimize_session.py -- the `session` member of gt-optimize: what prompt-cache WRITES cost.

    gt_optimize_session.py [--days N] [--projects-dir DIR] [--json]
    gt_optimize_session.py --brief [--days N] [--projects-dir DIR]
    gt_optimize_session.py --check [PCT] [--days N] [--projects-dir DIR]

`gt_optimize.py --only vault` asks what the VAULT stores that a session pays for. This asks
what a SESSION carries: every cache write in the Claude Code transcripts
(`~/.claude/projects/**/*.jsonl`, which already record per-turn cache reads, cache writes and the
TTL each write used), classified by cause. Report-only: it reads transcripts and changes nothing.

THE FOUR CAUSES. Each turn's cache write is split, in tokens, between:

    cold      the first turn of a stream (a session, a subagent, or the first turn after a
              compaction): there was no prefix to reuse
    growth    the conversation genuinely grew: tokens beyond the prefix the previous turn left
    expiry    AVOIDABLE. The gap since the previous turn outlived the TTL that turn's prefix was
              written at, so the whole prefix was rebuilt. The TTL is read from what was last
              WRITTEN (ephemeral_5m vs ephemeral_1h), never a constant: a 17-minute gap expires
              a 5-minute entry and not a 1-hour one
    invalid   AVOIDABLE. Inside the TTL, but the turn read less from cache than the previous turn
              left there: the cached prefix changed and had to be rewritten

Only `expiry` and `invalid` are reported as avoidable. A rebuild is capped at the prefix that
existed; anything written beyond it is `growth`, because it would have been written anyway.

THE TRAPS, each a defect found while prototyping this on real data (2026-09-23):

  * ONE API REQUEST WRITES SEVERAL TRANSCRIPT ROWS -- one per streamed content block, each
    repeating the same cumulative usage. Keyed on timestamp, one request counted up to five
    times, inflated the turn count 2.2x and reported prefix invalidation at 50.7% when it was
    3.4%. Turns are deduplicated on (requestId, message.id).
  * Turns from different streams never affect each other: a stream is one transcript file and
    one sessionId and sidechain flag, so interleaved sessions each start `cold`.
  * A transcript whose last line is half-written (a live session) is read up to that line.

DOLLARS ARE LIST-PRICE EQUIVALENTS, NOT A BILL. A subscription is not billed per token. They rank
findings against each other: base input price per model x 1.25 (5-minute write), 2.0 (1-hour
write), 0.1 (cache read). An unknown model is priced at the default and counted, never raised.

SHARING SAFETY: no output names a session by more than 8 characters, and none prints an
absolute path -- transcript directory names encode the user's home path, so they are not shown.

Settings (registered in gt_settings.SETTINGS, listed by `/gt:gt-settings show`):
    optimize_session_days   default window in days (default 30)
    optimize_avoidable_pct  the --check threshold when PCT is omitted (default 50)

Exit: 0 nothing avoidable | 1 avoidable writes found | 2 usage
      3 --check: avoidable share exceeds PCT | 4 could not run (no transcripts with usage)
"""
import argparse
import datetime
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CLEAN, FINDINGS, USAGE, OVER, NO_ANSWER = 0, 1, 2, 3, 4

TTL_5M = 5 * 60
TTL_1H = 60 * 60
# A turn that reads within this many tokens of what the previous turn left is a full hit: the
# last few tokens of a prefix are routinely re-sent uncached (the newest user message).
READ_SLACK = 0.02

WRITE_5M, WRITE_1H, READ = 1.25, 2.0, 0.1
# USD per million INPUT tokens, list price, matched by substring in this order. The first match
# wins, so the specific spellings come before the family names.
PRICES = (
    ("claude-3-opus", 15.0), ("claude-opus-4-0", 15.0), ("claude-opus-4-1", 15.0),
    ("claude-opus-4-20", 15.0),
    ("opus", 5.0),
    ("claude-3-haiku", 0.25), ("claude-3-5-haiku", 0.8), ("haiku", 1.0),
    ("sonnet", 3.0),
)
DEFAULT_PRICE = 3.0

CAUSES = ("cold", "growth", "expiry", "invalid")
AVOIDABLE = ("expiry", "invalid")


def setting(name, fallback):
    try:
        import gt_settings                                       # noqa: PLC0415
        v = gt_settings.get(name)
        return v if v else fallback
    except Exception:                                            # noqa: BLE001
        return fallback


def price_for(model):
    """-> (USD per MTok input, known?). Unknown models fall back, never raise."""
    m = (model or "").lower()
    for needle, price in PRICES:
        if needle in m:
            return price, True
    return DEFAULT_PRICE, False


def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def transcript_files(projects_dir):
    return sorted(glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True))


def read_turns(projects_dir, since):
    """-> (streams, files_read). streams: {key: [turn, ...]} in file order, deduplicated.

    A turn is {ts, session, model, read, write, write_5m, write_1h, input, after_compact}."""
    seen = set()
    streams = {}
    files = transcript_files(projects_dir)
    for n, path in enumerate(files):
        compact_pending = {}
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                 # a half-written last line: keep going
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("sessionId") or row.get("session_id") or "")
                key = (n, sid, bool(row.get("isSidechain")))
                if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    compact_pending[key] = True
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message") if isinstance(row.get("message"), dict) else {}
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                if not usage:
                    continue
                dedup = (row.get("requestId"), msg.get("id"))
                if dedup == (None, None):
                    dedup = ("uuid", row.get("uuid"))
                if dedup in seen:
                    continue                 # the same request, another streamed block
                seen.add(dedup)
                ts = _ts(row.get("timestamp"))
                if ts is None or (since is not None and ts < since):
                    continue

                def num(d, k):
                    try:
                        return max(0, int(d.get(k) or 0))
                    except (TypeError, ValueError):
                        return 0
                write = num(usage, "cache_creation_input_tokens")
                cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"),
                                                               dict) else {}
                w1h, w5m = num(cc, "ephemeral_1h_input_tokens"), num(cc, "ephemeral_5m_input_tokens")
                if write and not (w1h or w5m):
                    w5m = write                  # no breakdown: the API default TTL
                streams.setdefault(key, []).append({
                    "ts": ts, "session": sid, "model": msg.get("model") or "",
                    "read": num(usage, "cache_read_input_tokens"), "write": write,
                    "write_5m": w5m, "write_1h": w1h, "input": num(usage, "input_tokens"),
                    "after_compact": compact_pending.pop(key, False)})
    return streams, len(files)


def classify(turns):
    """-> one {cause: tokens} dict per turn, in time order. Pure; the tests drive it."""
    out = []
    prev = None
    ttl = TTL_5M
    for t in sorted(turns, key=lambda x: x["ts"]):
        split = dict.fromkeys(CAUSES, 0)
        w = t["write"]
        if prev is None or t.get("after_compact"):
            split["cold"] = w
        else:
            prefix = prev["read"] + prev["write"]
            gap = (t["ts"] - prev["ts"]).total_seconds()
            if gap > ttl:
                rebuilt = min(w, prefix)
                split["expiry"], split["growth"] = rebuilt, w - rebuilt
            elif t["read"] < prefix * (1 - READ_SLACK):
                lost = min(w, prefix - t["read"])
                split["invalid"], split["growth"] = lost, w - lost
            else:
                split["growth"] = w
        out.append(split)
        # The TTL that decides the NEXT gap is the one this turn's prefix was last written at.
        # A read refreshes an entry at its own TTL, so a turn that wrote nothing keeps it.
        if t["write_1h"]:
            ttl = TTL_1H
        elif t["write_5m"]:
            ttl = TTL_5M
        prev = t
    return out


def analyse(streams):
    totals = dict.fromkeys(CAUSES, 0)
    dollars = dict.fromkeys(CAUSES, 0.0)
    read_tokens = 0
    read_dollars = 0.0
    unknown_models = set()
    per_session = {}
    turns_n = 0
    for key, turns in streams.items():
        ordered = sorted(turns, key=lambda x: x["ts"])
        for t, split in zip(ordered, classify(ordered)):
            turns_n += 1
            price, known = price_for(t["model"])
            if not known:
                unknown_models.add(t["model"] or "(none)")
            # A write's price depends on its TTL; spread the split proportionally.
            w = t["write"] or 1
            per_tok = (t["write_5m"] * WRITE_5M + t["write_1h"] * WRITE_1H) / w * price / 1e6
            for c in CAUSES:
                totals[c] += split[c]
                dollars[c] += split[c] * per_tok
            read_tokens += t["read"]
            read_dollars += t["read"] * READ * price / 1e6
            s = per_session.setdefault(t["session"][:8] or "unknown",
                                       {"avoidable": 0, "turns": 0, "peak_context": 0})
            s["avoidable"] += split["expiry"] + split["invalid"]
            s["turns"] += 1
            s["peak_context"] = max(s["peak_context"], t["read"] + t["write"] + t["input"])
    written = sum(totals.values())
    avoid = totals["expiry"] + totals["invalid"]
    peaks = sorted(s["peak_context"] for s in per_session.values())
    return {
        "turns": turns_n,
        "sessions": len(per_session),
        "tokens_read": read_tokens,
        "tokens_written": written,
        "write_split": totals,
        "avoidable_tokens": avoid,
        "avoidable_pct": round(100.0 * avoid / written, 1) if written else 0.0,
        "equivalent_usd": {"writes": {c: round(v, 2) for c, v in dollars.items()},
                           "avoidable": round(dollars["expiry"] + dollars["invalid"], 2),
                           "reads": round(read_dollars, 2)},
        "context_per_session": {"median_peak": peaks[len(peaks) // 2] if peaks else 0,
                                "max_peak": peaks[-1] if peaks else 0},
        "top_sessions": sorted(({"session": k, **v} for k, v in per_session.items()
                                if v["avoidable"]), key=lambda r: -r["avoidable"])[:5],
        "unpriced_models": sorted(unknown_models),
        "price_basis": {"write_5m": WRITE_5M, "write_1h": WRITE_1H, "read": READ,
                        "default_usd_per_mtok": DEFAULT_PRICE,
                        "label": "list-price equivalents, not a bill"},
    }


def _k(n):
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%.1fK" % (n / 1e3) if n >= 1e3 else str(n)


def brief(r, days):
    s = r["write_split"]
    w = r["tokens_written"] or 1
    return ("cache: %.1f%% of writes avoidable over %dd (expiry %.1f%%, invalid %.1f%%) "
            "~$%.2f list-price equivalent" % (r["avoidable_pct"], days,
                                              100.0 * s["expiry"] / w, 100.0 * s["invalid"] / w,
                                              r["equivalent_usd"]["avoidable"]))


def render(r, days, files):
    s = r["write_split"]
    w = r["tokens_written"] or 1
    lines = ["session: %d turn(s) in %d session(s) over the last %d day(s), from %d transcript "
             "file(s)" % (r["turns"], r["sessions"], days, files),
             "  tokens read from cache   %s" % _k(r["tokens_read"]),
             "  tokens written to cache  %s" % _k(r["tokens_written"])]
    for c in CAUSES:
        lines.append("    %-8s %10s  %5.1f%%  ~$%.2f%s" % (
            c, _k(s[c]), 100.0 * s[c] / w, r["equivalent_usd"]["writes"][c],
            "   avoidable" if c in AVOIDABLE else ""))
    lines.append("  avoidable share: %.1f%% of cache writes, ~$%.2f list-price equivalent "
                 "(not a bill)" % (r["avoidable_pct"], r["equivalent_usd"]["avoidable"]))
    cp = r["context_per_session"]
    lines.append("  context per session: median peak %s, largest %s"
                 % (_k(cp["median_peak"]), _k(cp["max_peak"])))
    if r["top_sessions"]:
        lines.append("  most avoidable: " + ", ".join(
            "%s %s" % (t["session"], _k(t["avoidable"])) for t in r["top_sessions"]))
    if r["unpriced_models"]:
        lines.append("  priced at the default (unknown model): %d model name(s)"
                     % len(r["unpriced_models"]))
    if s["expiry"] > s["invalid"]:
        lines.append("  The larger cause is expiry: a session left idle past its cache lifetime "
                     "is rebuilt in full. Cut (/compact or /clear) BEFORE stepping away -- "
                     "/gt:gt-minimize -- not after.")
    elif s["invalid"]:
        lines.append("  The larger cause is a changed prefix: something early in the context "
                     "(a hook's output, an edited CLAUDE.md, a switched model) changed mid-session.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=None,
                    help="window in days (default: setting optimize_session_days, 30)")
    ap.add_argument("--projects-dir", default=None,
                    help="transcripts root (default ~/.claude/projects)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--brief", action="store_true", help="one line, for a report card")
    ap.add_argument("--check", nargs="?", const="setting", default=None, metavar="PCT",
                    help="exit 3 when the avoidable share exceeds PCT (default: setting "
                         "optimize_avoidable_pct, 50), 0 otherwise")
    args = ap.parse_args(argv)

    days = args.days
    if days is None:
        try:
            days = int(setting("optimize_session_days", "30"))
        except ValueError:
            days = 30
    if days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return USAGE
    pct = None
    if args.check is not None:
        raw = setting("optimize_avoidable_pct", "50") if args.check == "setting" else args.check
        try:
            pct = float(raw)
        except ValueError:
            print("--check takes a percentage, got %r" % args.check, file=sys.stderr)
            return USAGE

    root = os.path.expanduser(args.projects_dir or "~/.claude/projects")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    streams, files = read_turns(root, since) if os.path.isdir(root) else ({}, 0)
    if not any(streams.values()):
        # Not "0% avoidable": nothing was measured, and a clean result here would be a pass for
        # work that never happened. No path in the message -- see SHARING SAFETY.
        print("could not run: no Claude Code transcripts with usage in the last %d day(s) "
              "(%d transcript file(s) found)" % (days, files), file=sys.stderr)
        return NO_ANSWER

    r = analyse(streams)
    r["days"] = days
    if args.json:
        print(json.dumps({"version": 1, "member": "session", **r}, indent=2))
    elif args.brief:
        print(brief(r, days))
    else:
        print(render(r, days, files))
    if pct is not None:
        return OVER if r["avoidable_pct"] > pct else CLEAN
    return FINDINGS if r["avoidable_tokens"] else CLEAN


if __name__ == "__main__":
    sys.exit(main())
