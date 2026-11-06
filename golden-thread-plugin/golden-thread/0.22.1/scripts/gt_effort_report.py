#!/usr/bin/env python3
"""gt_effort_report.py -- where does effort (thinking) go, and what did a build cost? (0.21.4)

    gt_effort_report.py effort [--projects-dir DIR] [--days N] [--json]
    gt_effort_report.py builds --session-file FILE [--gap SECONDS] [--json]

READ-ONLY, ADVISORY, AGGREGATE ONLY. It reads Claude Code's own transcripts (~/.claude/projects/
<project>/<session>.jsonl and <session>/subagents/agent-*.jsonl with their .meta.json) and prints
counts and ratios: never a message, a prompt, a tool result or a file's text. Nothing is written
and nothing is applied. Every figure is labelled MEASURED (read from a transcript row) or
ESTIMATED (derived with a stated rule).

`effort` -- per task kind (a skill for a main session; agent type + a keyword class of the
agent's description for a subagent): model, the effort the rows record (`effort` /
`perTurnEffort`), turns, input / cache / output tokens, THINKING tokens (usage.output_tokens_
details.thinking_tokens) and the share of output that is thinking. Where the share is high on a
routine kind (a merge, a doc edit, a status check, a reader) it suggests ONE effort lower: for a
user-spawned agent an agent definition with `effort:` in its frontmatter (Claude Code agent
definitions take `model:` and `effort:`; gt's own agents get theirs from gt_model_policy.py), for
the main session `/effort <level>` or the effort setting. gt_model_calibrate.py is what shows a
lower effort is SAFE for gt's own stages and skills; this shows where the tokens are.

`builds` -- the subagents of one session, grouped into build runs by launch time (a gap of more
than --gap seconds, default 900, starts a new run): per agent total tokens, duration, tool calls
and outcome markers; per run wall time, total tokens, peak concurrency and DUPLICATED READING
(the same file read by several agents: counted on identical Read paths, sized from the result's
length). It then ESTIMATES a sequential run of the same work: one agent whose context grows by
the measured per-turn growth of each agent in launch order, with each duplicate read removed
once, priced with the weights gt_savings.py uses. The estimate is a model, not a measurement.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import shlex
import sys

sys.dont_write_bytecode = True
OK, PROBLEMS, USAGE = 0, 1, 2
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_COST = {None: 1.0, "low": 1.0, "medium": 1.3, "high": 1.7, "xhigh": 2.5, "max": 3.5}
W_IN, W_OUT, W_READ, W_5M, W_1H = 1.0, 5.0, 0.1, 1.25, 2.0     # as gt_savings.py / gt_skill_runlog.py
ROUTINE = ("merge", "doc", "status", "read", "test", "lint", "check")
SHARE_BAR = 0.4                                  # thinking share of output that triggers a suggestion
MIN_TURNS = 5
CLASSES = (("merge", ("merge", "rebase", "cherry")), ("doc", ("doc", "manual", "readme",
           "changelog", "release note")), ("test", ("test", "pytest")),
           ("status", ("status", "check", "verify", "validate")),
           ("read", ("read", "explore", "find", "search", "survey", "investigate")),
           ("review", ("review", "audit")), ("build", ("build", "implement", "add ", "create",
           "write", "fix")))


def alias_of(model):
    m = str(model or "").lower()
    for a in ("haiku", "sonnet", "opus", "fable"):
        if a in m:
            return a
    return m or "?"


def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _num(d, k):
    try:
        return max(0, int(d.get(k) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def class_of(text):
    t = str(text or "").lower()
    for name, keys in CLASSES:
        if any(k in t for k in keys):
            return name
    return "other"


READERS = ("cat", "head", "tail", "nl", "sed", "bat")


def norm_path(p):
    """One file under several worktrees or checkouts is one file: key it from the plugin root."""
    p = str(p).replace("\\", "/")
    for mark in ("golden-thread-plugin/", "/Obsidian/"):
        if mark in p:
            return p.split(mark, 1)[1]
    return p


def bash_read(cmd):
    """-> the one file a read-only `cat|head|tail|sed -n|nl FILE` command shows, else None. Only a
    plain single-file read counts (no pipes, redirects or lists), so a duplicate is a real one."""
    if not isinstance(cmd, str) or any(c in cmd for c in "|;&<>`$\n"):
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if not toks or toks[0] not in READERS or (toks[0] == "sed" and "-n" not in toks):
        return None
    files = [t for t in toks[1:] if not t.startswith("-") and "/" in t and "." in t.rsplit("/", 1)[-1]]
    return files[0] if len(files) == 1 else None


def _len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_len(b.get("text") if isinstance(b, dict) and "text" in b else
                        (b.get("content") if isinstance(b, dict) else b)) for b in content)
    return 0


def scan(path):
    """-> one aggregate record for a transcript file (no text kept), or None."""
    rec = {"model": collections.Counter(), "effort": collections.Counter(), "turns": 0, "input": 0,
           "output": 0, "thinking": 0, "cache_read": 0, "cache_write": 0, "first": None,
           "last": None, "tools": 0, "errors": 0, "stop": None, "ctx": [], "reads": {},
           "skills": [], "read_chars": {}}
    best, ids, order = {}, {}, []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            ts = _ts(row.get("timestamp"))
            if ts is not None:
                rec["first"] = ts if rec["first"] is None else min(rec["first"], ts)
                rec["last"] = ts if rec["last"] is None else max(rec["last"], ts)
            content = msg.get("content")
            if row.get("type") == "user" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        if b.get("is_error"):
                            rec["errors"] += 1
                        tid = b.get("tool_use_id")
                        if tid in ids:
                            rec["read_chars"][tid] = _len(b.get("content"))
            elif row.get("type") == "user" and isinstance(content, str) and \
                    "<command-name>" in content and not rec["skills"]:
                s = content.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
                rec["skills"].append(s.strip().lstrip("/"))
            if row.get("type") != "assistant":
                continue
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in ids:
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    rec["tools"] += 1
                    if b.get("name") == "Read" and inp.get("file_path"):
                        ids[b["id"]] = str(inp["file_path"])
                    elif b.get("name") == "Bash" and bash_read(inp.get("command")):
                        ids[b["id"]] = bash_read(inp.get("command"))
                    else:
                        ids[b["id"]] = None
                    if b.get("name") == "Skill" and inp.get("skill") and not rec["skills"]:
                        rec["skills"].append(str(inp["skill"]))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if not usage:
                continue
            key = (row.get("requestId"), msg.get("id"))
            if key == (None, None):
                key = ("uuid", row.get("uuid"))
            if key not in best:
                order.append(key)
            if key not in best or _num(usage, "output_tokens") >= _num(best[key][0], "output_tokens"):
                best[key] = (usage, msg, row)
    for key in order:
        usage, msg, row = best[key]
        det = usage.get("output_tokens_details") if isinstance(
            usage.get("output_tokens_details"), dict) else {}
        rec["turns"] += 1
        rec["input"] += _num(usage, "input_tokens")
        rec["output"] += _num(usage, "output_tokens")
        rec["thinking"] += _num(det, "thinking_tokens")
        rec["cache_read"] += _num(usage, "cache_read_input_tokens")
        rec["cache_write"] += _num(usage, "cache_creation_input_tokens")
        rec["ctx"].append(_num(usage, "input_tokens") + _num(usage, "cache_read_input_tokens")
                          + _num(usage, "cache_creation_input_tokens"))
        rec["model"][alias_of(msg.get("model"))] += 1
        eff = row.get("perTurnEffort") or row.get("effort")
        if eff:
            rec["effort"][str(eff)] += 1
        rec["stop"] = msg.get("stop_reason") or rec["stop"]
    rec["reads"] = {}
    for tid, path_ in ids.items():
        if path_:
            rec["reads"].setdefault(norm_path(path_), []).append(rec["read_chars"].get(tid, 0) // 4)
    return rec if rec["turns"] else None


def meta_of(path):
    m = path[:-len(".jsonl")] + ".meta.json"
    try:
        with open(m, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def modal(counter, default=None):
    return counter.most_common(1)[0][0] if counter else default


def weighted(r):
    return (r["input"] * W_IN + r["output"] * W_OUT + r["cache_read"] * W_READ
            + r["cache_write"] * W_5M)


def share(r):
    return round(r["thinking"] / r["output"], 3) if r["output"] else None


def lower(effort):
    if effort in EFFORTS and EFFORTS.index(effort) > 0:
        return EFFORTS[EFFORTS.index(effort) - 1]
    return None


# -- the effort view ---------------------------------------------------------------------------
def effort_groups(projects_dir, days):
    cutoff = None if not days else datetime.datetime.now().timestamp() - days * 86400
    groups = {}
    files = glob.glob(os.path.join(projects_dir, "*", "*.jsonl")) + \
        glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))
    for f in files:
        try:
            if cutoff and os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        r = scan(f)
        if not r:
            continue
        sub = os.sep + "subagents" + os.sep in f
        meta = meta_of(f) if sub else {}
        kind = ("agent:%s:%s" % (meta.get("agentType") or "?", class_of(meta.get("description")))
                if sub else "session:%s" % (r["skills"][0] if r["skills"] else "(no skill)"))
        eff = modal(r["effort"]) or meta.get("effort")
        g = groups.setdefault((kind, modal(r["model"], "?"), eff or "-"), {
            "kind": kind, "model": modal(r["model"], "?"), "effort": eff or "-", "files": 0,
            "turns": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "thinking": 0})
        g["files"] += 1
        for k in ("turns", "input", "cache_read", "cache_write", "output", "thinking"):
            g[k] += r[k]
    return list(groups.values())


def recommend(groups):
    out = []
    for g in groups:
        sh = share(g)
        cls = g["kind"].rsplit(":", 1)[-1]
        routine = g["kind"].startswith("agent:") and cls in ROUTINE
        low = lower(g["effort"])
        if sh is None or sh < SHARE_BAR or g["turns"] < MIN_TURNS or not low or g["output"] < 1000:
            continue
        if g["kind"].startswith("session:") and g["kind"] != "session:(no skill)":
            routine = g["kind"].split(":", 1)[1].rsplit(":", 1)[-1] in (
                "gt-open", "gt-list", "gt-sync", "gt-doctor", "gt-help", "gt-lint", "gt-settings")
        if not routine and not (g["kind"] == "session:(no skill)" and g["effort"] in ("xhigh", "max")):
            continue
        saved = int(g["thinking"] * (1 - EFFORT_COST[low] / EFFORT_COST[g["effort"]]))
        what = ("a main session on effort %s: try `/effort %s` (or the effort setting) for routine "
                "turns" % (g["effort"], low)) if g["kind"].startswith("session:") else (
                "an agent definition with `effort: %s` in its frontmatter (now %s)" % (low, g["effort"]))
        out.append({"kind": g["kind"], "model": g["model"], "from": g["effort"], "to": low,
                    "thinking_share": sh, "estimated_thinking_tokens_saved": saved,
                    "suggest": what, "basis": "ESTIMATED: thinking tokens x (1 - cost[to]/cost[from]) "
                    "with the calibration cost multipliers; prove it with gt_model_calibrate.py"})
    return sorted(out, key=lambda x: -x["estimated_thinking_tokens_saved"])


def cmd_effort(a):
    groups = effort_groups(a.projects_dir, a.days)
    for g in groups:
        g["thinking_share"] = share(g)
    groups.sort(key=lambda g: -g["output"])
    recs = recommend(groups)
    if a.json:
        print(json.dumps({"basis": "MEASURED from transcript rows; recommendations ESTIMATED",
                          "groups": groups, "recommendations": recs}, indent=1))
        return OK
    print("EFFORT REPORT -- MEASURED from local transcripts (aggregates only, nothing applied)")
    print("  %-34s %-6s %-6s %5s %9s %9s %8s  thinking" % (
        "kind", "model", "effort", "turns", "in+cache", "output", "think"))
    for g in groups[:a.top]:
        print("  %-34s %-6s %-6s %5d %9d %9d %8d  %s" % (
            g["kind"][:34], g["model"], g["effort"], g["turns"],
            g["input"] + g["cache_read"] + g["cache_write"], g["output"], g["thinking"],
            "-" if g["thinking_share"] is None else "%.0f%%" % (g["thinking_share"] * 100)))
    print("SUGGESTIONS (advisory; ESTIMATED)")
    for r in recs[:a.top] or [None]:
        if r is None:
            print("  none: no routine kind spends %.0f%% or more of its output thinking" % (SHARE_BAR * 100))
            break
        print("  %s (%s, thinking %.0f%%): %s -- about %d thinking tokens" % (
            r["kind"], r["model"], r["thinking_share"] * 100, r["suggest"],
            r["estimated_thinking_tokens_saved"]))
    return OK


# -- the build-cost view -----------------------------------------------------------------------
def build_agents(session_file):
    sdir = session_file[:-len(".jsonl")] if session_file.endswith(".jsonl") else session_file
    agents = []
    for f in sorted(glob.glob(os.path.join(sdir, "subagents", "*.jsonl"))):
        r = scan(f)
        if r:
            m = meta_of(f)
            r.update(meta=m, label=str(m.get("description") or "")[:48],
                     cls=class_of(m.get("description")), eff=modal(r["effort"]) or m.get("effort"))
            agents.append(r)
    agents.sort(key=lambda r: r["first"] or 0)
    return agents


def runs_of(agents, gap):
    runs, cur, end = [], [], None
    for r in agents:
        if cur and r["first"] is not None and end is not None and r["first"] - end > gap:
            runs.append(cur)
            cur, end = [], None
        cur.append(r)
        if r["last"] is not None:
            end = max(end or 0, r["last"])
    if cur:
        runs.append(cur)
    return runs


def shares(agents):
    """Where the weighted cost of these agents goes: cache re-reads, cache writes, output, input."""
    parts = {"cache_read": sum(r["cache_read"] for r in agents) * W_READ,
             "cache_write": sum(r["cache_write"] for r in agents) * W_5M,
             "output": sum(r["output"] for r in agents) * W_OUT,
             "input": sum(r["input"] for r in agents) * W_IN}
    tot = sum(parts.values()) or 1
    out = {k: round(v / tot, 3) for k, v in parts.items()}
    out["thinking_of_total"] = round(sum(r["thinking"] for r in agents) * W_OUT / tot, 3)
    return out


def peak_concurrency(agents):
    ev = []
    for r in agents:
        if r["first"] is not None and r["last"] is not None:
            ev += [(r["first"], 1), (r["last"], -1)]
    cur = best = 0
    for _t, d in sorted(ev, key=lambda e: (e[0], e[1])):
        cur += d
        best = max(best, cur)
    return best


def duplicate_reads(agents):
    """-> (extra read calls, estimated tokens re-read) over paths read by more than one agent."""
    seen = {}
    for i, r in enumerate(agents):
        for p, sizes in r["reads"].items():
            seen.setdefault(p, []).append((i, sum(sizes) // max(1, len(sizes)), len(sizes)))
    calls = toks = 0
    for p, who in seen.items():
        if len({w[0] for w in who}) > 1:
            first = min(who, key=lambda w: w[0])
            for w in who:
                if w is not first:
                    calls += w[2]
                    toks += w[1] * w[2]
    return calls, toks


def sequential_estimate(agents, dup_tokens):
    """ESTIMATE one agent doing all the work in launch order. The shared base (smallest first-turn
    context) is paid once; each turn adds the measured growth of its own agent's context, with the
    duplicate-read tokens removed pro rata; every turn re-reads the whole accumulated context."""
    base = min((r["ctx"][0] for r in agents if r["ctx"]), default=0)
    deltas = []
    for r in agents:
        prev = r["ctx"][0] if r["ctx"] else 0
        for c in r["ctx"][1:]:
            deltas.append(max(0, c - prev))
            prev = c
        deltas.append(0)
    growth = sum(deltas)
    keep = max(0.0, 1 - dup_tokens / growth) if growth else 1.0
    ctx, read, peak = base, 0, base
    for d in deltas:
        read += ctx
        ctx += d * keep
        peak = max(peak, ctx)
    out = sum(r["output"] for r in agents)
    turns = sum(r["turns"] for r in agents)
    write = base + growth * keep
    w = read * W_READ + write * W_5M + out * W_OUT + turns * 2 * W_IN
    return {"turns": turns, "cache_read": int(read), "cache_write": int(write), "output": out,
            "peak_context": int(peak), "weighted": int(w)}


def cmd_builds(a):
    if not os.path.isfile(a.session_file):
        print("gt_effort_report: no such transcript: %s" % a.session_file, file=sys.stderr)
        return USAGE
    agents = build_agents(a.session_file)
    main = scan(a.session_file)
    if not agents:
        print("gt_effort_report: that session has no subagent transcripts", file=sys.stderr)
        return PROBLEMS
    runs = []
    for run in runs_of(agents, a.gap):
        calls, dup = duplicate_reads(run)
        par_w = sum(weighted(r) for r in run)
        seq = sequential_estimate(run, dup)
        starts = [r["first"] for r in run if r["first"] is not None]
        ends = [r["last"] for r in run if r["last"] is not None]
        runs.append({
            "agents": len(run),
            "wall_s": int(max(ends) - min(starts)) if starts and ends else None,
            "summed_agent_s": int(sum((r["last"] or 0) - (r["first"] or 0) for r in run)),
            "peak_concurrency": peak_concurrency(run),
            "total_tokens": sum(r["input"] + r["cache_read"] + r["cache_write"] + r["output"] for r in run),
            "output_tokens": sum(r["output"] for r in run),
            "thinking_tokens": sum(r["thinking"] for r in run),
            "duplicate_read_calls": calls, "duplicate_read_tokens_estimated": dup,
            "parallel_weighted_measured": int(par_w),
            "weighted_share_measured": shares(run),
            "sequential_estimate": seq,
            "sequential_separate_agents": {
                "wall_s_estimated": int(sum((r["last"] or 0) - (r["first"] or 0) for r in run)),
                "weighted_estimated": int(par_w - dup * W_READ),
                "note": "same agents one after another: tokens equal the parallel run minus the "
                        "duplicate reads a later agent could be handed instead; only wall time grows"},
            "sequential_vs_parallel_weighted": round(seq["weighted"] / par_w, 2) if par_w else None,
            "per_agent": [{
                "label": r["label"], "class": r["cls"], "model": modal(r["model"], "?"),
                "effort": r["eff"], "turns": r["turns"], "tool_calls": r["tools"],
                "tokens": r["input"] + r["cache_read"] + r["cache_write"] + r["output"],
                "output": r["output"], "thinking": r["thinking"],
                "seconds": int((r["last"] or 0) - (r["first"] or 0)), "tool_errors": r["errors"],
                "ended": r["stop"], "reads": sum(len(v) for v in r["reads"].values())}
                for r in run]})
    doc = {"basis": "per-agent figures MEASURED; duplicate reads and the sequential run ESTIMATED",
           "main_session": None if not main else {
               "model": modal(main["model"], "?"), "effort": modal(main["effort"]),
               "turns": main["turns"], "output": main["output"], "thinking": main["thinking"],
               "thinking_share": share(main), "peak_context": max(main["ctx"] or [0])},
           "runs": runs}
    if a.json:
        print(json.dumps(doc, indent=1))
        return OK
    print("BUILD RUNS -- %s" % doc["basis"])
    for i, r in enumerate(runs, 1):
        s = r["sequential_estimate"]
        print("run %d: %d agents, wall %ss (summed %ss), peak concurrency %d, %d tokens "
              "(%d output, %d thinking)" % (i, r["agents"], r["wall_s"], r["summed_agent_s"],
                                           r["peak_concurrency"], r["total_tokens"],
                                           r["output_tokens"], r["thinking_tokens"]))
        print("   duplicate reads: %d extra Read calls, ~%d tokens (ESTIMATED)" % (
            r["duplicate_read_calls"], r["duplicate_read_tokens_estimated"]))
        print("   weighted cost shares (measured): %s" % ", ".join(
            "%s %.1f%%" % (k, v * 100) for k, v in r["weighted_share_measured"].items()))
        sp = r["sequential_separate_agents"]
        print("   one after another (separate agents): wall ~%ds, weighted ~%d (ESTIMATED; tokens "
              "~equal, only time changes)" % (sp["wall_s_estimated"], sp["weighted_estimated"]))
        print("   parallel weighted %d (measured) vs sequential ~%d (ESTIMATED, x%s), peak "
              "context ~%d" % (r["parallel_weighted_measured"], s["weighted"],
                              r["sequential_vs_parallel_weighted"], s["peak_context"]))
        for p in r["per_agent"]:
            print("   %-48s %-6s %-6s %4dt %6dk tok %5ds tools %-3d err %d" % (
                p["label"], p["model"], p["effort"] or "-", p["turns"], p["tokens"] // 1000,
                p["seconds"], p["tool_calls"], p["tool_errors"]))
    return OK


def build_parser():
    p = argparse.ArgumentParser(prog="gt_effort_report.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    e = sub.add_parser("effort", help="thinking vs output per task kind, with advisory suggestions")
    e.add_argument("--projects-dir", default=os.path.join(os.path.expanduser("~"), ".claude",
                                                          "projects"))
    e.add_argument("--days", type=int, default=14)
    e.add_argument("--top", type=int, default=15)
    e.add_argument("--json", action="store_true")
    b = sub.add_parser("builds", help="cost of one session's subagent build runs")
    b.add_argument("--session-file", required=True)
    b.add_argument("--gap", type=int, default=900)
    b.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "effort":
        return cmd_effort(a)
    if a.cmd == "builds":
        return cmd_builds(a)
    print("usage: gt_effort_report.py effort|builds (see --help)", file=sys.stderr)
    return USAGE


if __name__ == "__main__":
    sys.exit(main())
