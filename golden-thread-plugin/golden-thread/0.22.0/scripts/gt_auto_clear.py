#!/usr/bin/env python3
"""gt_auto_clear.py -- the auto-clear policy (the "421 test"): shadow mode, the `on` gate and the default matrix (0.22.0, phase 143).

    gt_auto_clear.py status       the setting, whether `on` is allowed yet, and what it means
    gt_auto_clear.py matrix [--model M --shape S --pct P]
                                  the default matrix (Setting 1): what the measured sets say, or "not measured"
    gt_auto_clear.py stop --hook  Stop hook: in shadow mode, one decision line to the log; prints nothing
    gt_auto_clear.py replay [--home DIR] [--save]
                                  score the logged decisions against hindsight (the oracle): shadow regret
    gt_auto_clear.py calibrate [--home DIR] [--dry-run]
                                  learn your own (floor, margin) from the log; kept only if it saves more

Rule (online, blind to the end of the session): clear when the next prompt's marginal cost exceeds the
cycle's running average cost including the clear. It needs context at or above a floor (100K, a safety
choice: nothing below it is measured) and at least two growth observations.

Cost model per prompt: alpha + beta * tokens processed; the cold start after a clear (W) counts as handoff
cost. Ported from the cut-savings session's blindlib.decide and oracle (stdlib only, no numpy).
Figures that session reports (live blind, Sonnet 5.5, n=1-3 per seed) are its own and not re-measured here.

`auto_clear`: off | shadow | on. Shadow advises and logs and never clears. `on` is refused until a saved shadow
regret score exists (`replay --save`) over at least ON_MIN_SESSIONS sessions with a mean regret of at most
ON_MAX_REGRET_PTS points; until then it reads as shadow. `on` also needs the record the typed word leaves
(auto-clear/confirmed.json, from `gt_settings.py set auto_clear on`): `on` hand-edited into vault-config.json without
it reads as shadow, and the record is dropped at the first reply with the setting off or shadow. When it is allowed, the
Stop hook writes a decision file (autoclear-<session>.json, beside the expiry guard's mod files) whenever the rule says
clear, and appends the decision to clears.jsonl labelled "event": "decided" (the hook cannot see whether the mod acts:
the clears made are the mod's done-<turn>.json records). decisions.jsonl and clears.jsonl rotate to <name>.1 past 1 MB. The mod (hooks/guard_mod.js, policyClear) does the cut: the same save-then-clear path as
auto_compact, kind clear whatever cut_kind says, never with cut_handoff none, never while a background task runs.
The hook cannot know when a session ends, so it passes no end; decide with `left` (prompts remaining) and the
policy never clears in the last 2. The floor is a setting too (`auto_clear_floor`, never below 100,000).
Replay: each logged session is rebuilt from its rows (the context at every prompt) and scored in points of
the no-clear cost: what the logged clears spent beyond the best cuts in hindsight. Shadow never clears, so
it keeps saying "clear" on later prompts; the scorer keeps one clear per cycle (at least 3 prompts apart,
the policy's own minimum). It uses the priors' cost line, not billing (unverified against real spend).
Personal matrix (Setting 2): the default (Setting 1) is floor 100K, margin 1.0. `calibrate` searches a
small grid of floors (never below 100K) and margins over your logged history and stores the best, with its
date, data size and predicted saving, only when it saves more than the one in force on that same history.
Not known: behaviour below 100K context, handoff levels H0-H3, Opus, whether W is stable out of sample.
"""
import argparse
import json
import math
import os
import re
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gt_model_records  # noqa: E402  (the one model record table)

FLOOR = 100000
TAIL_BYTES = 512 * 1024
# The priors are DATA on a model's row in the model record table (gt_model_prices.json `models`,
# `auto_clear_priors`): ac/bc = cost of a clear, al/ub = per-prompt cost line, c0 = context after a clear,
# W = cold start. Self-verified by the cut-savings session on Sonnet 5.5 only, n=1-3, and they assume Claude's
# prompt-cache cost shape. A model is measured only when its EXACT id (or a declared alias) has a row with
# priors; every other id is skipped as "not measured", never given another model's numbers.
MIN_PROMPTS = 4                # a logged session shorter than this is not scored
MAX_PROMPTS = 300              # the oracle is cubic in the prompt count: score the first 300 prompts
MIN_GAP = 3                    # prompts between two clears of one session (decide needs 2 growth steps)
MIN_SESSIONS = 5               # calibrate refuses to learn from fewer
GRID_FLOORS = (100000, 125000, 150000, 200000)
GRID_MARGINS = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5)
DEFAULT_MARGIN = 1.0
END_GUARD = 2                  # no clear in the last 2 prompts when the end of the session is known
ON_MIN_SESSIONS = MIN_SESSIONS  # the saved score must cover at least this many scored sessions before `on`
ON_MAX_REGRET_PTS = 4.0        # ... and its mean regret must be at most this many points (owner ruling 2026-10-27; the
#                                live blind runs gave 0.4, 3.9 and 0.9, mean 1.7). Exactly this passes; anything above fails.
#                                It must be a finite number from 0 up: NaN, infinity or a negative value is not a score.
LOG_MAX_BYTES = 1000000        # decisions.jsonl and clears.jsonl move to <name>.1 past this (one older generation kept)
CONFIRM_WORD = "clear"
CONFIRM_TEXT = (
    "gt auto_clear: clear this session's context by itself\n"
    "\n"
    "When the rule says clearing would pay, gt will clear this session's\n"
    "context by itself, after saving a handoff with the facts. It works\n"
    "only on the model it was measured on:\n"
    "  %s\n"
    "Not measured: behaviour below 100K tokens, handoff levels H0 to H3,\n"
    "Opus live blind runs. Turn it off: gt_settings.py set auto_clear off\n"
    "\n"
    "Type clear to continue (anything else cancels): ")
MATRIX_NAMES = ("Sonnet 5.5", "Sonnet 4.6", "Opus 5.5", "Opus 4.8")
PRIORS = gt_model_records.TABLE.priors_table()      # {exact model id or alias: priors}


def priors_for(model, table=None):
    """-> the priors measured on exactly this model, or None."""
    p = (PRIORS if table is None else table).get(model) if isinstance(model, str) else None
    return dict(p) if p else None


def home():
    return os.path.expanduser("~")


def _cfg(h=None):
    try:
        with open(os.path.join(h or home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_score(h=None):
    """-> the saved shadow regret score ({at, sessions, mean_regret_pts, ...}) or None."""
    try:
        with open(score_path(h), encoding="utf-8") as fh:
            d = json.load(fh)
        int(d["sessions"])
        if d.get("mean_regret_pts") is not None:
            float(d["mean_regret_pts"])
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest_model(h=None):
    """the model of the newest logged decision, or None: the model the person's last session ran."""
    last = None
    for path in _log_files(log_path(h)):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        m = json.loads(line).get("model")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(m, str) and m:
                        last = m
        except OSError:
            continue
    return last


def _model_ok(rec):
    """-> (ok, text) for one model's {sessions, mean_regret_pts} in the saved score."""
    try:
        n, r = int(rec["sessions"]), rec.get("mean_regret_pts")
        r = None if r is None else float(r)
    except (KeyError, TypeError, ValueError):
        return False, "the saved score for this model is unreadable. Run `gt_auto_clear.py replay --save` again."
    if r is not None and not (math.isfinite(r) and 0.0 <= r):
        return False, ("the saved score for this model has a regret of %r, which is not a score (it must be a number "
                       "from 0 up). Run `gt_auto_clear.py replay --save` again." % (r,))
    if n < ON_MIN_SESSIONS or r is None:
        return False, ("the saved shadow score covers %d scored session(s) on this model; `on` needs at least %d. "
                       "Keep shadow on, then run `gt_auto_clear.py replay --save` again." % (n, ON_MIN_SESSIONS))
    if not r <= ON_MAX_REGRET_PTS:
        return False, ("shadow regret is %.1f points over %d sessions on this model; `on` needs %.1f or less. Keep "
                       "shadow on and run `gt_auto_clear.py calibrate`, then `replay --save`."
                       % (r, n, ON_MAX_REGRET_PTS))
    return True, "shadow regret %.1f points over %d sessions (limit %.1f)" % (r, n, ON_MAX_REGRET_PTS)


def eligibility(h=None, model=None):
    """-> (allowed, text): may `auto_clear` be `on`? Only with a saved shadow regret score, BOUND to the model it was
    measured on, over enough scored sessions and a mean regret within ON_MAX_REGRET_PTS. The score never expires:
    it holds for the same model at any age. With `model`, the score must be for that model (a score for another
    model counts as absent); with none, any model the score passes for will do. The text says what to run."""
    d = read_score(h)
    if d is None:
        return False, ("no shadow regret score yet. Turn on shadow (`gt_settings.py set auto_clear shadow`), use gt "
                       "for at least %d sessions, then run `gt_auto_clear.py replay --save`." % ON_MIN_SESSIONS)
    bm = d.get("by_model")
    if not isinstance(bm, dict) or not bm:
        return False, ("the saved score has no model recorded (it is unbound), so it cannot show `on` is safe for "
                       "any model. Run `gt_auto_clear.py replay --save` again.")
    if model is not None:
        if model not in bm:
            return False, ("the saved score was measured on %s; this session runs %s; run `gt_auto_clear.py replay "
                           "--save` after shadow has logged sessions on this model."
                           % (", ".join(sorted(bm)), model))
        return _model_ok(bm[model])
    results = [(m, _model_ok(v)) for m, v in sorted(bm.items())]
    for m, (ok, why) in results:
        if ok:
            return True, "%s: %s" % (m, why)
    return results[0][1]


def scored_models(h=None):
    """the models the saved score passes for."""
    d = read_score(h) or {}
    bm = d.get("by_model") if isinstance(d.get("by_model"), dict) else {}
    return [m for m, v in sorted(bm.items()) if _model_ok(v)[0]]


def confirm_path(h=None):
    """the record the typed word leaves: `on` in vault-config.json without it reads as shadow (a hand-edit skips the word)."""
    return os.path.join(h or home(), ".claude", "golden-thread", "auto-clear", "confirmed.json")


def confirmed(h=None):
    try:
        with open(confirm_path(h), encoding="utf-8") as fh:
            return json.load(fh).get("how") == "typed"
    except (OSError, ValueError, AttributeError):
        return False


def drop_confirmation(h=None):
    try:
        os.remove(confirm_path(h))
    except OSError:
        pass


def confirm_on(models, stdin=None, h=None):
    """Show what `auto_clear on` does and ask for the typed word, once. No terminal, no turning it on. -> True only
    on exactly CONFIRM_WORD (no default), and only once the record of it (confirm_path) is written."""
    stdin = stdin or sys.stdin
    try:
        import gt_tty
        tty = gt_tty.has_terminal(stdin)
    except Exception:                                  # noqa: BLE001 -- unknown means no terminal
        tty = False
    if not tty:
        sys.stderr.write("gt auto_clear: turning it on needs a person at a terminal; this process has none. "
                         "Nothing was changed.\n")
        return False
    sys.stderr.write("\n" + CONFIRM_TEXT % ("\n  ".join(models) or "no model"))
    sys.stderr.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError):
        answer = ""
    if answer.strip() != CONFIRM_WORD:
        sys.stderr.write("gt auto_clear: not confirmed. Nothing was changed.\n")
        return False
    try:
        _write_atomic(confirm_path(h), json.dumps({"at": int(time.time()), "models": list(models), "how": "typed"}))
    except OSError as e:
        sys.stderr.write("gt auto_clear: could not record the confirmation (%s). Nothing was changed.\n"
                         % type(e).__name__)
        return False
    return True


def setting(h=None):
    """off | shadow | on. `on` needs the saved score (eligibility) AND the typed word's record (confirmed): a value
    hand-edited into vault-config.json without the word reads as shadow."""
    v = str(_cfg(h).get("auto_clear") or "off").strip().lower()
    if v == "on":
        return "on" if confirmed(h) and eligibility(h)[0] else "shadow"
    return v if v in ("off", "shadow") else "off"


def floor_setting(h=None):
    """the user's `auto_clear_floor` in tokens, or None. Never below FLOOR: nothing under it is measured."""
    try:
        v = int(str(_cfg(h).get("auto_clear_floor") or "").strip())
    except ValueError:
        return None
    return v if v >= FLOOR else None


def linfit(pts):
    """least squares line through (x, y) pairs -> (slope, intercept); flat x gives slope 0."""
    n = float(len(pts))
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    d = n * sxx - sx * sx
    if abs(d) < 1e-12:
        return 0.0, sy / n
    sl = (n * sxy - sx * sy) / d
    return sl, (sy - sl * sx) / n


def ccost(P, cur):
    return P["ac"] + P["bc"] * cur + P["W"] + P["p_fix"] * P["r_fix"]


def decide(P, seen, incs, cur, S, m, floor, nn, margin=1.0, minseen=2):
    """seen: [(tokens processed, cost)]; incs: growth of recent plain prompts; cur: context now;
    S, m: cost and count since the last clear. True = clear now."""
    if len(incs) < minseen or cur < floor:
        return False
    if len(seen) >= 3:
        sl, al = linfit(seen)
        ub = max(sl, 1e-9)
    else:
        ub, al = P["ub"], P["al"]
    g = sum(incs[-4:]) / float(len(incs[-4:]))
    marg = al + ub * nn * (cur + g / 2)
    avg = (S + ccost(P, cur)) / m
    return marg > margin * avg


def oracle(f, P, floor):
    """hindsight best: f has al, be, n[], inc[], cur0, N -> (best cost, cut positions)."""
    N = f["N"]
    best = [0] + [1e18] * N
    arg = [0] * (N + 1)

    def pc(q, cur):
        return f["al"] + f["be"] * f["n"][q] * (cur + f["inc"][q] / 2)

    for j in range(1, N + 1):
        for i in range(j):
            cur = f["cur0"] if i == 0 else P["c0"]
            s = 0
            for q in range(i, j):
                s += pc(q, cur)
                cur += f["inc"][q]
            if j < N:
                if cur < floor:
                    continue
                s += ccost(P, cur)
            if best[i] + s < best[j]:
                best[j] = best[i] + s
                arg[j] = i
    cuts = []
    j = N
    while j > 0:
        i = arg[j]
        if i > 0:
            cuts.append(i)
        j = i
    return best[N], sorted(cuts)


def model_total(f, P, cuts):
    cur = f["cur0"]
    tot = 0
    for q in range(f["N"]):
        tot += f["al"] + f["be"] * f["n"][q] * (cur + f["inc"][q] / 2)
        cur += f["inc"][q]
        if (q + 1) in cuts and q + 1 < f["N"]:
            tot += ccost(P, cur)
            cur = P["c0"]
    return tot


def regret_pts(f, P, cuts, best_cuts, floor):
    """points of the no-cut cost that `cuts` spends beyond the hindsight best `best_cuts`."""
    base = model_total(f, P, [])
    return 100.0 * (model_total(f, P, cuts) - model_total(f, P, best_cuts)) / base if base else 0.0


def record(session, cur, marginal, average, clear, reason, model=None):
    """one decision, with its inputs, so regret can be scored later. Shadow: never acted on. `model` is the id
    it was decided for: replay needs it to pick that model's priors."""
    rec = {"session": session, "context": cur, "marginal": round(marginal, 6), "average": round(average, 6),
           "clear": bool(clear), "reason": reason, "acted": False, "at": int(time.time())}
    if model:
        rec["model"] = str(model)
    return rec


def log_path(h=None):
    return os.path.join(h or home(), ".claude", "golden-thread", "auto-clear", "decisions.jsonl")


def contexts(path):
    """-> (model, [context size at the end of each prompt since the last compaction]) or None."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            raw = fh.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return None
    lines = raw.split("\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    ctxs, model, open_prompt = [], None, False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        typ = row.get("type")
        if typ == "system" and (row.get("subtype") == "compact_boundary" or row.get("compactMetadata")):
            ctxs, open_prompt = [], False
        elif typ == "user" and not row.get("isCompactSummary"):
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            c = msg.get("content")
            real = bool(c.strip()) if isinstance(c, str) else (
                isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in c)
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c))
            if real:
                ctxs.append(None)
                open_prompt = True
        elif typ == "assistant" and open_prompt:
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            m = str(msg.get("model") or "")
            if u and not m.startswith("<"):
                ctxs[-1] = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                model = m
    ctxs = [c for c in ctxs if c]
    return (model, ctxs) if model and ctxs else None


def shadow_decision(table, model, ctxs, session="-", floor=FLOOR, margin=DEFAULT_MARGIN, left=None):
    """the policy's call on the prompts seen so far. `table` is {exact model id: priors} (PRIORS); a model not in
    it is not measured and is never clear. Cost comes from the priors, not billing (unverified against real
    spend); the decision is logged and never acted on."""
    cur = ctxs[-1]
    P = priors_for(model, table)
    if P is None:
        return record(session, cur, 0.0, 0.0, False,
                      "model not measured (priors exist only for: %s)" % (", ".join(table) or "no model"), model)
    incs = [b - a for a, b in zip(ctxs, ctxs[1:]) if b > a]
    S = sum(P["al"] + P["ub"] * c for c in ctxs)
    m = len(ctxs)
    g = sum(incs[-4:]) / len(incs[-4:]) if incs else 0.0
    marg = P["al"] + P["ub"] * (cur + g / 2)
    avg = (S + ccost(P, cur)) / m
    clear = decide(P, [], incs, cur, S, m, floor, 1, margin)
    if clear and left is not None and left <= END_GUARD:
        return record(session, cur, marg, avg, False, "session end within %d prompts" % END_GUARD, model)
    if cur < floor:
        why = "context below the %d floor" % floor
    elif len(incs) < 2:
        why = "fewer than 2 growth observations"
    else:
        why = "marginal above the running average" if clear else "marginal at or below the running average"
    return record(session, cur, marg, avg, clear, why, model)


def safe_id(sid):
    """the same file-name form guard_mod.js `safe` gives a session id."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(sid or ""))[:80]


def mod_dir(h=None):
    return os.path.join(h or home(), ".claude", "golden-thread", "expiry-guard", "mod")


def scratch_dir(h=None):
    """where gt_compact.py writes when sandbox mode denies the folder above; the mod reads both."""
    return os.path.join(h or home(), ".gt-scratch", "compact")


def clears_path(h=None):
    return os.path.join(h or home(), ".claude", "golden-thread", "auto-clear", "clears.jsonl")


def _append(path, line):
    """one line to a log, moving the log to <path>.1 first once it is past LOG_MAX_BYTES (that older generation is
    replaced, so the pair stays under about twice the cap)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def _log_files(path):
    """the older generation first, then the current log."""
    return [path + ".1", path]


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def decision_file(sid, h=None, scratch=False):
    return os.path.join(scratch_dir(h) if scratch else mod_dir(h), "autoclear-%s.json" % safe_id(sid))


def _live_decision(h, sid, rec, floor, margin):
    """`on` and allowed: hand a clear to the mod, or take back a stale one. The mod acts at the end of the turn;
    nothing here clears anything."""
    if not safe_id(sid):
        return
    if not rec["clear"]:
        for scratch in (False, True):
            try:
                os.remove(decision_file(sid, h, scratch))
            except OSError:
                pass
        return
    # `at` to the millisecond: the mod takes only a decision written since its turn started (guard_mod.js policyClear)
    body = json.dumps({"session": str(sid), "at": round(time.time(), 3), "clear": True, "context": rec["context"],
                       "marginal": rec["marginal"], "average": rec["average"], "model": rec.get("model"),
                       "reason": rec["reason"]})
    for scratch in (False, True):
        try:
            _write_atomic(decision_file(sid, h, scratch), body)
            break
        except OSError:
            continue
    else:
        return
    # A decision handed to the mod, not a clear made: the mod may not act (busy, a turn started, no handoff, a later
    # turn's decision replaced it). The clears it made are its done-<turn>.json records with status "cleared".
    one = {k: rec.get(k) for k in ("at", "session", "context", "marginal", "average", "model", "reason")}
    one.update({"floor": floor, "margin": margin, "event": "decided", "acted": None})
    try:
        _append(clears_path(h), json.dumps(one))
    except OSError:
        pass


def run_stop(payload, h=None):
    """the Stop hook body. Shadow: appends one line to the decision log. `on` (allowed by the gate): the same, plus a
    decision file for the mod and a per-clear record. Returns None (no message)."""
    if str(_cfg(h).get("auto_clear") or "off").strip().lower() != "on":
        drop_confirmation(h)                           # off or shadow: turning it on again needs the typed word again
    mode = setting(h)
    if mode not in ("shadow", "on") or not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return None
    got = contexts(payload.get("transcript_path"))
    if got is None:
        return None
    floor, margin = active_matrix(h)
    sid = str(payload.get("session_id") or "-")
    gate = None
    if mode == "on":                                   # the score must be for the model this session runs
        ok, why = eligibility(h, got[0])
        if not ok:
            mode, gate = "shadow", why
    rec = shadow_decision(PRIORS, got[0], got[1], sid[:36], floor, margin)
    rec["prompts"] = len(got[1])
    if mode == "on":
        rec["live"] = True
    if gate:
        rec["gate"] = gate
    _append(log_path(h), json.dumps(rec))
    if mode == "on":
        _live_decision(h, sid, rec, floor, margin)
    return None


def score_path(h=None):
    return os.path.join(h or home(), ".claude", "golden-thread", "auto-clear", "score.json")


def matrix_path(h=None):
    return os.path.join(h or home(), ".claude", "golden-thread", "auto-clear", "matrix.json")


def load_matrix(h=None):
    """the personal matrix in force ({floor, margin, set_at, ...}) or None: Setting 1, the default."""
    try:
        with open(matrix_path(h), encoding="utf-8") as fh:
            m = json.load(fh)
        if (isinstance(m, dict) and math.isfinite(float(m["floor"])) and math.isfinite(float(m["margin"]))
                and int(m["floor"]) >= FLOOR and float(m["margin"]) > 0):
            return m
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        pass
    return None


def active_matrix(h=None):
    """(floor, margin) in force: the user's `auto_clear_floor` if set, else the learned floor, else 100K; the margin
    is the learned one or 1.0."""
    m = load_matrix(h)
    fl = floor_setting(h)
    floor = fl if fl else (int(m["floor"]) if m else FLOOR)
    return floor, (float(m["margin"]) if m else DEFAULT_MARGIN)


# ---- replay: score the logged decisions against hindsight ---------------------------------

def read_log(h=None):
    """-> {session: [rows in time order]} from the decision log; unreadable lines are skipped."""
    by = {}
    for path in _log_files(log_path(h)):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        int(r["context"])
                        str(r["session"])
                    except (ValueError, KeyError, TypeError, OverflowError):
                        continue
                    if isinstance(r, dict):
                        by.setdefault(str(r["session"]), []).append(r)
        except OSError:
            continue
    for rows in by.values():
        rows.sort(key=lambda r: r.get("at") or 0)
    return by


def session_features(rows, P):
    """the oracle's input for one logged session. The context at prompt q is rows[q]; growth of prompt q
    is the next row's minus this one (a drop, a compaction, counts as no growth); the last growth is not
    known, so it is the mean of the others."""
    ctx = [int(r["context"]) for r in rows[:MAX_PROMPTS]]
    inc = [max(0, b - a) for a, b in zip(ctx, ctx[1:])]
    inc.append(sum(inc) // len(inc) if inc else 0)
    return {"al": P["al"], "be": P["ub"], "n": [1] * len(ctx), "inc": inc, "cur0": ctx[0], "N": len(ctx)}


def logged_cuts(rows, n):
    """the 1-based prompt counts after which the log says "clear", one per cycle and never after the last
    prompt (a clear there saves nothing)."""
    cuts = []
    for i, r in enumerate(rows[:n]):
        p = i + 1
        if r.get("clear") and p < n and (not cuts or p - cuts[-1] >= MIN_GAP):
            cuts.append(p)
    return cuts


def session_priors(rows, table=None):
    """-> the priors of the one model a logged session ran on, or None when its rows name no model, name more than
    one, or name one without priors (a row logged before the model was recorded is skipped, never assumed)."""
    models = {r.get("model") for r in rows}
    return priors_for(models.pop(), table) if len(models) == 1 else None


def _usable(rows, table=None):
    return (len(rows) >= MIN_PROMPTS and not any("not measured" in str(r.get("reason")) for r in rows)
            and session_priors(rows, table) is not None)


def replay(h=None, table=None, floor=FLOOR):
    """score every usable logged session: regret in points of its no-clear cost, per session and the mean."""
    out, skipped = [], 0
    for sid, rows in sorted(read_log(h).items()):
        if not _usable(rows, table):
            skipped += 1
            continue
        P = session_priors(rows, table)
        f = session_features(rows, P)
        _, best = oracle(f, P, floor)
        cuts = logged_cuts(rows, f["N"])
        out.append({"session": sid, "prompts": f["N"], "logged_cuts": cuts, "oracle_cuts": best,
                    "regret_pts": round(regret_pts(f, P, cuts, best, floor), 3),
                    "model": rows[0].get("model")})
    mean = round(sum(r["regret_pts"] for r in out) / len(out), 3) if out else None
    by = {}
    for r in out:
        by.setdefault(r["model"], []).append(r["regret_pts"])
    by_model = {m: {"sessions": len(v), "mean_regret_pts": round(sum(v) / len(v), 3)} for m, v in sorted(by.items())}
    return {"sessions": len(out), "skipped": skipped, "mean_regret_pts": mean, "by_model": by_model, "rows": out,
            "at": int(time.time())}


# ---- calibrate: the personal matrix (Setting 2) -------------------------------------------

def simulate(f, P, floor, margin):
    """the cuts the online rule would make over this session's growth, with these parameters."""
    cuts, cur, cycle = [], f["cur0"], [f["cur0"]]
    for q in range(f["N"] - 1):
        cur += f["inc"][q]
        cycle.append(cur)
        incs = [b - a for a, b in zip(cycle, cycle[1:]) if b > a]
        S = sum(P["al"] + P["ub"] * c for c in cycle)
        if decide(P, [], incs, cur, S, len(cycle), floor, 1, margin):
            cuts.append(q + 1)
            cur = P["c0"]
            cycle = [cur]
    return cuts


def saving_pts(feats, floor, margin):
    """points of the total no-clear cost this (floor, margin) saves over all the sessions: `feats` is a list of
    (features, that session's own model priors)."""
    base = sum(model_total(f, P, []) for f, P in feats)
    got = sum(model_total(f, P, set(simulate(f, P, floor, margin))) for f, P in feats)
    return 100.0 * (base - got) / base if base else 0.0


def maybe_store(h, cand, saving, sessions, prompts, in_force=None):
    """write `cand` as the personal matrix only if `saving` beats the one in force; -> stored or not.
    `in_force` is that one's saving measured on the same history; without it the stored prediction
    (made on the history of its day) is the bar."""
    cur = load_matrix(h)
    if in_force is None and cur is not None:
        in_force = float(cur.get("predicted_saving_pts", -1e9))
    if in_force is not None and saving <= in_force + 1e-9:
        return False
    doc = {"floor": int(cand["floor"]), "margin": float(cand["margin"]), "set_at": time.strftime("%Y-%m-%d"),
           "sessions": sessions, "prompts": prompts, "predicted_saving_pts": round(saving, 3)}
    p = matrix_path(h)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return True


def calibrate(h=None, table=None, dry_run=False):
    feats = [(session_features(rows, session_priors(rows, table)), session_priors(rows, table))
             for rows in read_log(h).values() if _usable(rows, table)]
    res = {"sessions": len(feats), "replaced": False, "current_saving_pts": 0.0, "best_saving_pts": 0.0,
           "best": None}
    if len(feats) < MIN_SESSIONS:
        res["note"] = "needs at least %d logged sessions of 4+ prompts; have %d" % (MIN_SESSIONS, len(feats))
        return res
    cur_f, cur_m = active_matrix(h)
    cur_s = saving_pts(feats, cur_f, cur_m)
    res["current_saving_pts"] = round(cur_s, 3)
    best, best_s = None, None
    for fl in GRID_FLOORS:
        for mg in GRID_MARGINS:
            s = saving_pts(feats, fl, mg)
            if best_s is None or s > best_s + 1e-9:
                best, best_s = {"floor": fl, "margin": mg}, s
    res["best"], res["best_saving_pts"] = best, round(best_s, 3)
    if best_s > cur_s + 1e-9 and not dry_run:
        res["replaced"] = maybe_store(h, best, best_s, len(feats), sum(f["N"] for f, _p in feats), cur_s)
    return res


# ---- the default matrix (Setting 1): what the measured sets say, and where they stop -------------

def matrix_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "auto-clear",
                        "default-matrix.json")


def load_default_matrix(path=None):
    try:
        with open(path or matrix_file(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and isinstance(d.get("sets"), list) else None
    except (OSError, ValueError):
        return None


def matrix_model(model):
    """A Claude Code model id (family, major and minor joined by dashes) or a matrix name such as 'Sonnet 5.5' -> the matrix
    name; a model with no row in the matrix -> None. No model id is spelled out here: ids live in the model mapping pack."""
    if not isinstance(model, str):
        return None
    if model in MATRIX_NAMES:
        return model
    m = re.match(r"^claude-(sonnet|opus)-(\d+)-(\d+)(?:\[[^\]]*\])?$", model.strip().lower())
    name = "%s %s.%s" % (m.group(1).title(), m.group(2), m.group(3)) if m else None
    return name if name in MATRIX_NAMES else None


def _levels(row):
    return {lv: dict(v.get("saved_pct_at_prompts_after_cut") or {}) for lv, v in (row.get("levels") or {}).items()}


def matrix_lookup(model, shape, pct, data=None):
    """-> {state: measured | interpolated | not measured, ...}. `measured`: a set at that size. `interpolated`:
    between two measured sets of the same model and shape, linear in the size, only where both have the prompt
    count. Anything outside the measured sizes, or for a model or shape with no set, is `not measured`: nothing is
    extrapolated. Saved % is the share of the no-cut cost, cut cost included (n=1 per cell)."""
    d = data or load_default_matrix()
    nm = matrix_model(model)
    shape = str(shape or "").upper()
    if d is None:
        return {"state": "not measured", "why": "the default matrix file is missing"}
    if nm is None:
        return {"state": "not measured", "why": "no measured set for model %r" % (model,)}
    sets = [s for s in d["sets"] if s.get("model") == nm and s.get("shape") == shape]
    if not sets:
        return {"state": "not measured", "why": "no measured set for %s %s" % (nm, shape)}
    sized = sorted((s for s in sets if s.get("cut_pct_of_window") is not None), key=lambda s: s["cut_pct_of_window"])
    if not sized:
        return {"state": "not measured", "why": "%s %s: the measured sets' size not recorded, so they cannot be "
                "placed on the context-size axis" % (nm, shape)}
    lo, hi = sized[0]["cut_pct_of_window"], sized[-1]["cut_pct_of_window"]
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return {"state": "not measured", "why": "no context size given"}
    if pct < lo or pct > hi:
        return {"state": "not measured", "why": "%.1f%% of the window is outside the measured %.1f%% to %.1f%% for "
                "%s %s; nothing is extrapolated" % (pct, lo, hi, nm, shape)}
    for s in sized:
        if abs(s["cut_pct_of_window"] - pct) < 0.05:
            return {"state": "measured", "model": nm, "shape": shape, "pct": pct, "set": s, "levels": _levels(s)}
    below = [s for s in sized if s["cut_pct_of_window"] < pct][-1]
    above = [s for s in sized if s["cut_pct_of_window"] > pct][0]
    w = (pct - below["cut_pct_of_window"]) / (above["cut_pct_of_window"] - below["cut_pct_of_window"])
    lb, la = _levels(below), _levels(above)
    out = {}
    for lv in lb:
        if lv in la:
            out[lv] = {k: round(lb[lv][k] + w * (la[lv][k] - lb[lv][k]), 1) for k in lb[lv] if k in la[lv]}
    return {"state": "interpolated", "model": nm, "shape": shape, "pct": pct, "lower": below, "upper": above,
            "levels": out}


def _print_lookup(r):
    if r["state"] == "not measured":
        print("not measured: " + r["why"])
        return
    if r["state"] == "measured":
        print("measured: %s %s at %.1f%% of the window (set %s, n=1)" % (r["model"], r["shape"], r["pct"],
                                                                         r["set"]["id"]))
    else:
        print("interpolated between measured sets %s (%.1f%%) and %s (%.1f%%) for %s %s at %.1f%%" % (
            r["lower"]["id"], r["lower"]["cut_pct_of_window"], r["upper"]["id"], r["upper"]["cut_pct_of_window"],
            r["model"], r["shape"], r["pct"]))
    print("saved % of the no-cut cost, cut cost included, by prompts after the cut (negative = behind no cut):")
    for lv in sorted(r["levels"]):
        print("  %s  %s" % (lv, "  ".join("@%s %+g" % (k, v) for k, v in sorted(r["levels"][lv].items(),
                                                                                key=lambda kv: int(kv[0])))))


def _parser():
    ap = argparse.ArgumentParser(prog="gt_auto_clear.py", description="the auto-clear policy")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="the setting and what it means")
    st = sub.add_parser("stop", help="Stop hook: in shadow mode, one decision line to the log")
    st.add_argument("--hook", action="store_true", help="read the Stop payload on stdin; print nothing")
    rp = sub.add_parser("replay", help="score the logged decisions against hindsight")
    rp.add_argument("--home")
    rp.add_argument("--save", action="store_true")
    ca = sub.add_parser("calibrate", help="learn your own (floor, margin) from the log")
    ca.add_argument("--home")
    ca.add_argument("--dry-run", action="store_true")
    mx = sub.add_parser("matrix", help="the default matrix: what the measured sets say, or not measured")
    mx.add_argument("--model")
    mx.add_argument("--shape", default="SPSR")
    mx.add_argument("--pct", type=float)
    mx.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    a = _parser().parse_args(argv)
    if a.cmd == "stop":
        try:
            run_stop(json.load(sys.stdin))
        except Exception:                                # noqa: BLE001 -- a hook that fails shows nothing
            pass
        return 0
    if a.cmd in ("replay", "calibrate"):
        if a.cmd == "replay":
            r = replay(a.home)
            print("replay: %d session(s) scored, %d skipped (short, or a model the priors do not cover)"
                  % (r["sessions"], r["skipped"]))
            for x in r["rows"]:
                print("  %s  %3d prompts  logged cuts %s  best cuts %s  regret %.2f pts"
                      % (x["session"][:12], x["prompts"], x["logged_cuts"], x["oracle_cuts"], x["regret_pts"]))
            print("shadow regret: %s" % ("none yet (no scored sessions)" if r["mean_regret_pts"] is None
                                         else "%.2f points of the no-clear cost, mean" % r["mean_regret_pts"]))
            for m, v in r["by_model"].items():
                print("  on %s: %d session(s), mean regret %.2f points" % (m, v["sessions"], v["mean_regret_pts"]))
            if a.save and r["sessions"]:
                p = score_path(a.home)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump({k: r[k] for k in ("at", "sessions", "skipped", "mean_regret_pts", "by_model")}, fh)
            return 0
        r = calibrate(a.home, dry_run=a.dry_run)
        if r.get("note"):
            print("calibrate: " + r["note"])
        else:
            print("calibrate: %d sessions; in force saves %.2f pts, best found (floor %d, margin %.2f) saves %.2f pts: %s"
                  % (r["sessions"], r["current_saving_pts"], r["best"]["floor"], r["best"]["margin"],
                     r["best_saving_pts"], "stored" if r["replaced"] else "kept what was in force"))
        return 0
    if a.cmd == "matrix":
        d = load_default_matrix()
        fl, mg = active_matrix()
        if a.model is None or a.pct is None:
            rng = (d or {}).get("measured_range_pct_of_window") or {}
            print("default matrix (Setting 1): floor %d, margin %.2f in force%s" % (
                fl, mg, " (your personal matrix or floor setting)" if (load_matrix() or floor_setting()) else ""))
            print("measured sizes: %s%% to %s%% of the window; %d sets, n=1 per cell" % (
                rng.get("min"), rng.get("max"), len((d or {}).get("sets") or [])))
            print("not measured: " + "; ".join((d or {}).get("not_measured") or ["(matrix file missing)"]))
            return 0
        r = matrix_lookup(a.model, a.shape, a.pct, d)
        if a.json:
            print(json.dumps(r))
        else:
            _print_lookup(r)
        return 0
    mode = setting()
    ok, why = eligibility(model=latest_model())
    asked = str(_cfg().get("auto_clear") or "off").strip().lower()
    print("auto_clear: %s   (shadow = log only; on = the mod clears when the rule says so, once allowed)" % mode)
    if asked == "on" and mode != "on":
        print("  `on` is set but not allowed yet: " + (why if not ok else
              "it was not turned on with the typed word (a hand-edit of vault-config.json); run "
              "`gt_settings.py set auto_clear on` at a terminal"))
    elif mode != "on":
        print("  `on` would be %s: %s" % ("allowed" if ok else "refused", why))
    print("  floor %d, margin %.2f in force" % active_matrix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
