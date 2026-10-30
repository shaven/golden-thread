#!/usr/bin/env python3
"""gt_cut_advisor.py -- tells the user when a cut (compact, or clear plus a handoff) would pay back (0.22.0).

    gt_cut_advisor.py stop --hook   Stop hook: after a reply, ONE `systemMessage` when a cut would pay back
    gt_cut_advisor.py status        runs, mean milliseconds, suggestions; "model tokens used: 0"
    gt_cut_advisor.py why           the last decision with its inputs and the table row it used

SUGGEST-ONLY and off by default (`cut_advisor`: off | suggest). It never cuts, never calls a model and
never adds anything to the model's context: it prints `{"systemMessage": ...}`, which only the user sees.

The numbers are a lookup table (gt_cut_lookup.json) measured by the savings campaign: Sonnet 5.5, short
prompt and short response, ONE run per cell, a refit and a held-out check still pending. A suggestion
says so. Rules, from the request (research/2026-10-21-savings/CUT_ADVISOR_REQUEST.md):
  * under 5% of the window, or 2 or fewer prompts likely left: nothing;
  * the table row nearest the context size (beyond the last row, the last row, and it says so);
  * the horizon is the largest measured prompt count not above the prompts likely left (at most 10);
  * only levels H4 (clear, handoff plus key facts, fresh session) and H1 (handoff, then compact) are offered,
    whichever saves more dollars; a gain under 5% is nothing;
  * a model other than the measured one: nothing, and the reason is kept for `why`;
  * three or more compactions already in the session: nothing (handoffs decay over repeated cuts);
  * the same suggestion is not repeated within 8 Stop events.
Prompts likely left is a HEURISTIC (`unverified`): the user prompts in the last 30 minutes, capped at 10.
Reads only the last 512 KB of the transcript. Fails open: any error prints nothing and exits 0.
"""
import json
import os
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TAIL_BYTES = 512 * 1024
MIN_PCT = 5.0
MIN_LEFT = 3
MIN_GAIN = 0.05
MAX_CUTS = 3
REPEAT_STOPS = 8
PACE_SECONDS = 1800
MAX_LEFT = 10
OFFERED = ("H4", "H1")
LEVEL_WORDS = {
    "H4": "clear the session and continue from a handoff with the key facts",
    "H1": "write a handoff, then compact",
}
HOW = {"H4": "/gt:gt-minimize (clear with a handoff)", "H1": "/gt:gt-minimize compact"}


def home():
    return os.path.expanduser("~")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def setting():
    d = _read_json(os.path.join(home(), ".claude", "vault-config.json")) or {}
    v = str(d.get("cut_advisor") or "off").strip().lower()
    return v if v in ("off", "suggest") else "off"


_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        dirs = [HERE, os.path.join(home(), ".claude", "golden-thread", "hooks")]
        cache = os.path.join(home(), ".claude", "plugins", "cache", "golden-thread-plugin", "gt")
        try:
            dirs += [os.path.join(cache, v, "scripts") for v in sorted(os.listdir(cache), reverse=True)]
        except OSError:
            pass
        for d in dirs:
            t = _read_json(os.path.join(d, "gt_cut_lookup.json"))
            if t and isinstance(t.get("rows"), list):
                _TABLE = t
                break
        else:
            raise RuntimeError("gt_cut_lookup.json not found")
    return _TABLE


def _measured(model):
    t = table()
    want = str(t.get("model") or "").lower()
    return bool(want) and want in (model or "").lower()


def _model_words(t):
    m = str(t.get("model") or "")
    return m.replace("claude-", "").replace("-", " ").title().replace("5 5", "5.5") if m else "one model"


def advise(tokens, window, left, model, cuts=0):
    """Pure. -> {action: none|suggest, reason, context_pct, left, ...}; a suggestion also has level, horizon,
    saving (fraction of dollars against no cut), row_pct, beyond, break_even and words."""
    t = table()
    pct = 100.0 * tokens / window if window else 0.0
    out = {"action": "none", "context": tokens, "window": window, "context_pct": round(pct, 1),
           "left": left, "model": model, "cuts": cuts}
    if not _measured(model):
        out["reason"] = "the table was measured on %s only; this session's model is not covered" % _model_words(t)
        return out
    if cuts >= MAX_CUTS:
        out["reason"] = "already cut %d times this session; handoffs lose detail over repeated cuts" % cuts
        return out
    if pct < MIN_PCT:
        out["reason"] = "context is under %d%% of the window" % MIN_PCT
        return out
    if left < MIN_LEFT:
        out["reason"] = "only %d prompt(s) likely left; a cut loses money for the first 1 to 2 prompts" % left
        return out
    rows = sorted(t["rows"], key=lambda r: r["context_pct_of_window"])
    row = min(rows, key=lambda r: abs(r["context_pct_of_window"] - pct))
    beyond = pct > rows[-1]["context_pct_of_window"] * 1.1
    keys = sorted(int(k) for k in row["by_prompts_after_cut"])
    horizon = max([k for k in keys if k <= min(left, MAX_LEFT)] or [keys[0]])
    cell = row["by_prompts_after_cut"][str(horizon)]["usd_pct"]
    level = max(OFFERED, key=lambda l: cell.get(l, -9))
    gain = cell.get(level, -9)
    out.update({"row_pct": row["context_pct_of_window"], "beyond": beyond, "horizon": horizon, "level": level,
                "saving": gain, "break_even": (row.get("break_even_prompt_usd") or {}).get(level)})
    if gain < MIN_GAIN:
        out["reason"] = ("a cut would not pay back within %d prompts at this size (%+.0f%% in dollars, best of %s)"
                         % (horizon, 100 * gain, "/".join(OFFERED)))
        return out
    out["action"] = "suggest"
    out["reason"] = "a cut pays back"
    return out


def message(a):
    t = table()
    return ("gt cut advisor (suggest-only, an estimate): context is about %dK tokens (%.0f%% of the window) and about "
            "%d more prompts look likely, so a cut would %s. Measured saving over %d prompts: about %.0f%% of the "
            "dollars%s. Basis: %s, short prompts, one run per cell%s. Nothing was done; type %s if you want it. "
            "Turn this off: gt_settings.py set cut_advisor off."
            % (a["context"] // 1000, a["context_pct"], a["left"], LEVEL_WORDS[a["level"]], a["horizon"],
               100 * a["saving"], (", break-even at prompt %s" % a["break_even"]) if a.get("break_even") else "",
               _model_words(t), ("; beyond the measured sizes, so the %.1f%% row was used" % a["row_pct"])
               if a.get("beyond") else "", HOW[a["level"]]))


# ---- the transcript tail --------------------------------------------------------------------

def _ts(v):
    if not isinstance(v, str):
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_prompt(row):
    msg = row.get("message") if isinstance(row.get("message"), dict) else {}
    c = msg.get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in c) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return False


def read_tail(path, now=None):
    """-> {tokens, model, prompts_30, cuts} from the last TAIL_BYTES of the transcript, or None."""
    now = time.time() if now is None else now
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            raw = fh.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return None
    lines = raw.split("\n")
    if size > TAIL_BYTES:
        lines = lines[1:]                                # the first line is cut in half
    tokens = model = None
    prompts, cuts = 0, 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        typ, ts = row.get("type"), _ts(row.get("timestamp"))
        if typ == "system" and (row.get("subtype") == "compact_boundary" or row.get("compactMetadata")):
            cuts += 1
            tokens = None
        elif typ == "user" and not row.get("isCompactSummary") and _is_prompt(row):
            if ts is not None and now - ts <= PACE_SECONDS:
                prompts += 1
        elif typ == "assistant":
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            m = str(msg.get("model") or "")
            if u and not m.startswith("<"):
                n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
                tokens, model = n, m
    if tokens is None or not model:
        return None
    return {"tokens": tokens, "model": model, "prompts_30": prompts, "cuts": cuts}


# ---- state -----------------------------------------------------------------------------------

def state_path():
    return os.path.join(home(), ".claude", "golden-thread", "state", "cut-advisor.json")


def load_state():
    return _read_json(state_path()) or {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        tmp = state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(st, fh)
        os.replace(tmp, state_path())
    except OSError:
        pass


def window_for(model):
    t = table()
    return int(t.get("window_tokens") or 1_000_000)


def cmd_stop(argv):
    t0 = time.time()
    try:
        if setting() == "off":
            return 0
        try:
            p = json.load(sys.stdin)
        except ValueError:
            return 0
        if not isinstance(p, dict) or p.get("stop_hook_active"):
            return 0
        tail = read_tail(p.get("transcript_path"))
        if tail is None:
            return 0
        left = min(tail["prompts_30"], MAX_LEFT)
        a = advise(tail["tokens"], window_for(tail["model"]), left, tail["model"], tail["cuts"])
        st = load_state()
        sid = str(p.get("session_id") or "-")[:36]
        rec = st.get(sid) if isinstance(st.get(sid), dict) else {}
        rec["stops_since"] = int(rec.get("stops_since", REPEAT_STOPS)) + 1
        say = None
        if a["action"] == "suggest" and rec["stops_since"] >= REPEAT_STOPS:
            rec["stops_since"] = 0
            rec["suggestions"] = int(rec.get("suggestions", 0)) + 1
            say = message(a)
            a["shown"] = True
        ms = (time.time() - t0) * 1000.0
        rec["runs"] = int(rec.get("runs", 0)) + 1
        rec["ms"] = round(float(rec.get("ms", 0.0)) + ms, 2)
        rec["last"] = {"at": int(time.time()), "decision": a}
        st[sid] = rec
        if len(st) > 20:
            for k in sorted(st, key=lambda k: st[k].get("last", {}).get("at", 0))[:len(st) - 20]:
                st.pop(k, None)
        save_state(st)
        if say:
            print(json.dumps({"systemMessage": say}))
    except Exception:                                    # noqa: BLE001 -- a hook that fails shows nothing
        return 0
    return 0


def cmd_status(argv):
    st = load_state()
    runs = sum(int(r.get("runs", 0)) for r in st.values() if isinstance(r, dict))
    ms = sum(float(r.get("ms", 0)) for r in st.values() if isinstance(r, dict))
    sug = sum(int(r.get("suggestions", 0)) for r in st.values() if isinstance(r, dict))
    print("cut_advisor: %s" % setting())
    print("runs: %d   mean %.1f ms per run   suggestions: %d" % (runs, (ms / runs) if runs else 0.0, sug))
    print("model tokens used: 0   (a lookup in a table; the message goes to you, not to the model)")
    return 0


def cmd_why(argv):
    st = load_state()
    recs = [r for r in st.values() if isinstance(r, dict) and r.get("last")]
    if not recs:
        print("no decision recorded yet (cut_advisor is %s)" % setting())
        return 0
    last = max(recs, key=lambda r: r["last"].get("at", 0))["last"]
    a = last["decision"]
    print("decided %s ago: %s" % ("%ds" % (time.time() - last["at"]), a["action"]))
    print("context %d tokens (%.1f%% of a %d window), prompts likely left %s (last 30 minutes, a heuristic), cuts so far %s"
          % (a["context"], a["context_pct"], a["window"], a["left"], a["cuts"]))
    print("reason: %s" % a.get("reason"))
    if "row_pct" in a:
        print("table row %.1f%%, horizon %s prompts, best of %s: %s saving %+.0f%% in dollars; break-even prompt %s%s"
              % (a["row_pct"], a["horizon"], "/".join(OFFERED), a["level"], 100 * a["saving"], a.get("break_even"),
                 " (beyond the measured sizes)" if a.get("beyond") else ""))
    else:
        print("table row: not reached (the decision was made before a row was chosen)")
    t = table()
    print("table: %s -- %s" % (t.get("schema"), t.get("status")))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    return {"stop": cmd_stop, "status": cmd_status, "why": cmd_why}.get(cmd, cmd_status)(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
