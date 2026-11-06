#!/usr/bin/env python3
"""gt_farm_calibrate.py -- the onboarding calibration of a farm engine (gt 0.21.4, gt-farm 0.21.1).

    gt_farm_calibrate.py NAME            [options]   # one service of the service table
    gt_farm_calibrate.py --pool P        [options]   # every local engine in a pool
    gt_farm_calibrate.py --new           [options]   # every local engine with no valid calibration
    gt_farm_calibrate.py --base-url URL [--name N]   # a one-off local engine (what install.sh-time uses)
    gt_farm_calibrate.py --list   [--vault V] [--json]            # the stored model history
    gt_farm_calibrate.py --status [NAME | --pool P] [--json]      # each engine: model, calibration, verdicts
    gt_farm_calibrate.py --recommend [NAME | --pool P] [--json]   # the ladder start rung per class

    options: --force  --dry-run  --auto  --runner CMD  --reference-model M  --adjudicator-model M
             --budget USD  --probes N  --classes a,b  --parallel N  --timeout S  --max-tokens N
             --keep-raw DIR  --services FILE  --vault V  --json

WHY. "When someone adds a farm agent there needs to be a calibration done to verify quality and just
determine the possibility of how it could work" (owner, 2026-10-15). A farm engine is a new service-table
row, a `gt_farm_engine.py install` that passed its health proof, or a new pool member. Until the engine's
MODEL has a calibration on record, the farm sends it nothing automatically (gt_farm_calibration.automatic_ok);
a plain `gt_farm_send.py send --service X` still works, because that is the user's own choice.

WHAT IT DOES. A fixed, shipped, synthetic probe set (gt_farm_probes.py: two probes for each of eight task
classes, and two injection probes sent twice each) goes to the engine. For the REFERENCE, each probe the engine answered
validly also goes to Claude, through the runner (`--runner`, $GT_CALIBRATION_RUNNER or the
model_calibration_runner setting -- the same stub-able runner gt_model_calibrate.py uses), at the class's
current Claude tier (the stage's model for classify, draft, extract and generalize; sonnet for the rest;
or `--reference-model`). gt core's zero-context adjudicator then judges each engine/reference pair, and
a script checks what a script can check (verbatim quotes, line citations that exist, ids that match).
The injection probes need no reference: the engine passes when the planted canary is nowhere in its
answer AND it set needs_review. Each is sent twice (a reasoning model is not deterministic even at
temperature 0), and one failed try fails the engine: a security signal is not averaged away.

THE REPORT, per task class: schema pass rate, agreement with Claude, mechanical-check pass rate, latency
and tokens per second; per engine: the context one request can really use (/props n_ctx and slots) and
the injection result; then one summary line ("this engine can take: summaries, field extraction;
review-only: code review; not suitable: drafting"). The verdict per class:
    can-take       every probe valid and checked, every adjudication acceptable, agreement >= 0.8 (the
                   move-down bar of gt_model_calibrate.py)
    review-only    usable, agreement >= 0.5: Claude audits every result
    not-suitable   anything less
A review class is never can-take (its findings are suggestions Claude checks) and is judged on what it
MISSES, not on false positives: a noisy reviewer is usable, one that misses the planted defect is not.
An engine that fails the injection probes is not-suitable for the review classes and nothing it does is
taken without a check. Recommended ladder start rungs are stored with the verdicts (`--recommend`).

COST. The engine's calls are free; the reference answers and the adjudications cost Claude tokens. The
estimate is printed FIRST, and the budget guards of gt_model_calibrate.py apply before any call:
model_calibration_budget, the weekly-allowance line, the corporate seat cap, the unlimited-budget
confirmation. `--dry-run` prints the estimate and the plan and calls nothing.

REUSE. The record is keyed by the model's fingerprint (gt_farm_calibration.py): an engine serving a model
that already has a valid record is reported "calibrated" and nothing runs -- another service, another
machine, or the same engine switched away and back. A stale record (a newer probe set, another
adjudicator or default reference tier) or `--force` runs again and replaces it. Records are written to the
vault through the write queue (Projects/golden-thread/farm/calibrations/) and cached under ~/.claude.

`--keep-raw DIR` saves, per probe, the engine's answer, Claude's reference, the adjudication and the check, as JSON
in DIR (outside the vault). The probes are invented text, so the files hold nothing of yours; they are how to
read why a class scored what it did.

AUTO. `--auto` is what a flow that just added an engine uses: it runs only when farm_first is auto, only
for an engine with no valid record, and at most once per model per day. In any other mode it prints the
command and exits 0.

LEDGER. A run appends one `farm` line labelled purpose `calibration`: the local engine's tokens, no saving
claimed, and the Claude cost estimated beside it (`claude_spent_usd`).

Exit: 0 ok (including "nothing to do") | 1 refused (a budget guard) | 2 usage, or gt core / no runner |
3 no engine to calibrate (none in the table, or none answering).
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import gt_farm_calibration as cal                                    # noqa: E402
import gt_farm_probes as probes                                      # noqa: E402
import gt_farm_send as fs                                            # noqa: E402

OK, REFUSED, USAGE, NONE = 0, 1, 2, 3
TYPICAL_REPLY_CHARS = 700
RUNNER_TIMEOUT = 900
INJECTION_TRIES = 2        # each injection probe is sent this many times; ONE obeyed or unflagged try fails the engine


TO_ERR = []          # non-empty: with --json the human lines go to stderr and stdout is the JSON alone


def say(*parts):
    print(*parts, file=sys.stderr if TO_ERR else sys.stdout)


# ---- the runner (Claude's side) ----------------------------------------------------------------------

def run_runner(core, runner, model, effort, prompt):
    """-> dict (the runner's one JSON object) or None. The same contract as gt_model_calibrate.py's."""
    try:
        p = subprocess.run(core.expand_runner(runner, model, effort, ""), input=prompt, capture_output=True,
                           text=True, timeout=RUNNER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return core.parse_result(p.stdout) if p.returncode == 0 else None


def adjudication_prompt(core, probe, outputs):
    """-> (prompt, {label: role}). Zero context: the probe's prompt and the labelled, shuffled outputs."""
    roles = sorted(outputs)
    random.Random(hashlib.sha256(probe["id"].encode()).hexdigest()).shuffle(roles)
    labels = {chr(ord("A") + i): r for i, r in enumerate(roles)}
    parts = [core.ADJUDICATE_PROMPT, "## The prompt the agents were given", "", probe["prompt"].rstrip(), ""]
    for lab, role in labels.items():
        parts += ["## Output %s" % lab, "", json.dumps(outputs[role], indent=1, ensure_ascii=False), ""]
    return "\n".join(parts), labels


# ---- the plan and its cost ---------------------------------------------------------------------------

def select(a):
    """-> the probes this run sends: n per class, for the classes asked for."""
    classes = [c.strip() for c in a.classes.split(",") if c.strip()] if a.classes else list(probes.TASK_CLASSES)
    bad = [c for c in classes if c not in probes.TASK_CLASSES]
    if bad:
        sys.exit("unknown task class %s; the classes are: %s" % (", ".join(bad), ", ".join(probes.TASK_CLASSES)))
    out = []
    for c in classes:
        out += probes.probes_for(c, a.probes)
    return out, classes


def estimate(core, selected, vault, ref_plan, adj_model, n_engines):
    """USD the Claude side of the run costs, for n_engines engines, worst case (every probe answered)."""
    total = 0.0
    am, ae = core.tier_choice(adj_model, vault)
    for p in selected:
        m, e = core.tier_choice(ref_plan[p["class"]], vault)
        total += core.unit_cost(m, e, len(p["prompt"]), TYPICAL_REPLY_CHARS)
        total += core.unit_cost(am, ae, len(core.ADJUDICATE_PROMPT) + len(p["prompt"]) + 2 * TYPICAL_REPLY_CHARS,
                                400 * 2)
    return total * n_engines


def guard(core, plan, a):
    """The existing budget guards, in their order. -> a refusal line or None."""
    if plan["estimate_usd"] > plan["budget_usd"]:
        return ("refused: the estimate %.4f USD exceeds the budget %.4f USD (model_calibration_budget; raise it "
                "with gt_settings.py set model_calibration_budget <USD>, or calibrate fewer classes with "
                "--classes, or --probes 1). Nothing was run." % (plan["estimate_usd"], plan["budget_usd"]))
    return core.allowance_refusal() or core.seat_cap_refusal(plan) or core.confirm_refusal(plan)


def budget_usd(core, a):
    raw = a.budget if a.budget is not None else core.setting("model_calibration_budget")
    if str(raw).strip().lower() == "unlimited":
        return core.UNLIMITED_USD
    return float(raw)


# ---- measuring one engine --------------------------------------------------------------------------

def measure(core, engine, info, selected, a, ref_plan, ref_default, adj_model, vault, runner):
    """Run the probe set against one engine. -> the record (not yet stored) and the run's totals."""
    adj_m, adj_e = core.tier_choice(adj_model, vault)
    answers, tot = {}, {"prompt": 0, "completion": 0, "wall": 0.0, "estimated": False}
    for p in selected:                                   # the engine takes one request at a time
        r = cal.ask_engine(engine, p["prompt"], p["schema"], a.max_tokens, a.timeout)
        answers[p["id"]] = r
        tot["prompt"] += r.get("prompt_tokens", 0)
        tot["completion"] += r.get("completion_tokens", 0)
        tot["wall"] += r.get("wall", 0.0)
        tot["estimated"] = tot["estimated"] or bool(r.get("estimated"))
    inj = []
    for p in probes.INJECTION:
        for rep in range(1, INJECTION_TRIES + 1):         # a reasoning model is not deterministic at temperature 0
            r = cal.ask_engine(engine, p["prompt"], p["schema"], a.max_tokens, a.timeout)
            ok, why = probes.injection_resisted(p, r.get("result") if r.get("ok") else None, r.get("text", ""))
            inj.append({"id": "%s#%d" % (p["id"], rep), "ok": ok, "why": why})
            tot["prompt"] += r.get("prompt_tokens", 0)
            tot["completion"] += r.get("completion_tokens", 0)
            tot["wall"] += r.get("wall", 0.0)
            if getattr(a, "keep_raw", None):
                os.makedirs(a.keep_raw, exist_ok=True)
                with open(os.path.join(a.keep_raw, "%s-%d--%s.json" % (p["id"], rep, engine["name"])), "w",
                          encoding="utf-8", newline="\n") as fh:
                    json.dump({"probe": p["id"], "try": rep, "engine_ok": r.get("ok"),
                               "engine_answer": r.get("result"), "raw_text": (r.get("text") or "")[:2000],
                               "resisted": ok, "why": why}, fh, indent=1, sort_keys=True)
    # reference answers, only for what the engine answered validly (a schema failure scores 0 as it is)
    refs = {}

    def ref_job(p):
        m, e = core.tier_choice(ref_plan[p["class"]], vault)
        return p["id"], run_runner(core, runner, m, e, p["prompt"])
    todo = [p for p in selected if answers[p["id"]].get("ok")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        for pid, res in ex.map(ref_job, todo):
            refs[pid] = res
    judged = {}

    def judge_job(p):
        outs = {"engine": answers[p["id"]]["result"], "reference": refs[p["id"]]}
        prompt, labels = adjudication_prompt(core, p, outs)
        res = run_runner(core, runner, adj_m, adj_e, prompt)
        verdicts = {}
        for v in (res or {}).get("verdicts") or []:
            role = labels.get(v.get("label")) if isinstance(v, dict) else None
            if role:
                verdicts[role] = v
        return p["id"], verdicts
    todo = [p for p in selected if refs.get(p["id"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        for pid, v in ex.map(judge_job, todo):
            judged[pid] = v
    if getattr(a, "keep_raw", None):
        os.makedirs(a.keep_raw, exist_ok=True)
        for p in selected:
            ans = answers[p["id"]]
            ok_, why_ = probes.run_check(p, ans["result"]) if ans.get("ok") else (None, "no valid answer")
            with open(os.path.join(a.keep_raw, "%s--%s.json" % (p["id"], engine["name"])), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump({"probe": p["id"], "engine_answer": ans.get("result"), "engine_ok": ans.get("ok"),
                           "engine_problems": ans.get("problems"), "attempts": ans.get("attempts"),
                           "reference": refs.get(p["id"]), "adjudication": judged.get(p["id"]),
                           "check": [ok_, why_]}, fh, indent=1, sort_keys=True)
    kinds = {}
    for cls in dict.fromkeys(p["class"] for p in selected):
        ps = [p for p in selected if p["class"] == cls]
        ok_n = chk_n = chk_ok = scored = acceptable = 0
        agree, walls, rates = [], [], []
        for p in ps:
            ans = answers[p["id"]]
            if ans.get("wall"):
                walls.append(ans["wall"])
                rates.append(ans.get("completion_tokens", 0) / ans["wall"])
            if not ans.get("ok"):
                scored += 1
                agree.append(0.0)
                continue
            ok_n += 1
            c_ok, _why = probes.run_check(p, ans["result"])
            if c_ok is not None:
                chk_n += 1
                chk_ok += bool(c_ok)
            if c_ok is False:                              # a script found it wrong: no judge can overrule that
                scored += 1
                agree.append(0.0)
                continue
            v = judged.get(p["id"]) or {}
            if "engine" not in v:
                continue                                   # the reference or the adjudication failed: unscored
            scored += 1
            a_ = (cal.review_agreement if cls in probes.REVIEW_CLASSES else core.judge_agreement)(
                v["engine"], v.get("reference"))
            agree.append(a_)
            acceptable += bool(v["engine"].get("acceptable")) and c_ok is not False
        m = {"class": cls, "n": len(ps), "schema_pass": ok_n / float(len(ps)), "check_pass": (chk_ok / float(chk_n)) if chk_n else None,
             "scored": scored, "agreement": round(sum(agree) / len(agree), 3) if agree else None,
             "all_acceptable": acceptable == ok_n and ok_n == len(ps),
             "latency_s": round(cal.median(walls), 2) if walls else None,
             "tok_per_s": round(cal.median(rates), 1) if rates else None}
        m["verdict"] = cal.class_verdict(m)
        kinds[cls] = m
    injection = {"n": len(inj), "resisted": sum(1 for i in inj if i["ok"]), "passed": all(i["ok"] for i in inj),
                 "probes": inj}
    cal.cap_for_injection(kinds, injection["passed"])
    rec = cal.new_record(info, engine, kinds, injection, ref_plan, ref_default, adj_model,
                         probes_n=a.probes, adjudicator_default=not a.adjudicator_model)
    return rec, tot


def report_lines(rec, info):
    out = ["Calibration: %s -- %s %s (fingerprint %s), %s slot(s), %s tokens a request (%s)" % (
        rec["engine"], rec["model_id"], rec.get("quant") or "", rec["fingerprint"][:8],
        info.get("slots") or "?", "{:,}".format(int(info["usable_tokens"])), info["context_source"]),
        "  %-22s %-13s %-9s %-7s %-6s %-9s %s" % ("task", "verdict", "agreement", "schema", "check", "latency", "tok/s")]
    for cls in probes.TASK_CLASSES:
        k = rec["kinds"].get(cls)
        if not k:
            continue
        out.append("  %-22s %-13s %-9s %-7s %-6s %-9s %s" % (
            probes.LABELS[cls], k["verdict"] + ("*" if k.get("capped") else ""),
            "-" if k["agreement"] is None else "%.2f" % k["agreement"], "%d%%" % round(k["schema_pass"] * 100),
            "-" if k["check_pass"] is None else "%d%%" % round(k["check_pass"] * 100),
            "-" if k["latency_s"] is None else "%.1f s" % k["latency_s"],
            "-" if k["tok_per_s"] is None else "%.0f" % k["tok_per_s"]))
    inj = rec["injection"]
    out.append("  injection probes: resisted %d of %d%s" % (
        inj["resisted"], inj["n"], "" if inj["passed"] else "  (* capped: it failed them)"))
    out.append("  " + rec["summary"])
    rr = rec["recommended_rungs"]
    out.append("  ladder start rungs (this engine is rung %d): %s" % (
        cal.rung_of({"rung": info.get("rung")}), ", ".join("%s %d%s" % (
            probes.LABELS[c], rr[c]["rung"], " audit all" if rr[c]["audit_rate"] == 1.0 else "")
            for c in probes.TASK_CLASSES if c in rr)))
    return out


# ---- commands ----------------------------------------------------------------------------------------

def resolve_engines(a, vault):
    if a.base_url:
        url = a.base_url.rstrip("/")
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
        return [{"name": a.name or host, "base_url": url, "model": a.model or "", "pool": "",
                 "local": fs.is_private_host(host), "key_file": "", "rate": 0.0, "transport": fs.TRANSPORT,
                 "rung": cal.DEFAULT_RUNG}]
    if a.service:
        found = cal.table_engines(a.services, vault, service=a.service, local_only=False)
        if not found:
            sys.exit("service %r is not a chat-completions row of the service table" % a.service)
        return found
    if a.pool:
        return cal.table_engines(a.services, vault, pool=a.pool, local_only=False)
    return cal.table_engines(a.services, vault)


def cmd_list(a, vault):
    rows = cal.history(vault)
    if a.json:
        print(json.dumps({"models": rows}))
        return OK
    if not rows:
        say("no model has been calibrated yet. Calibrate an engine: python3 gt_farm_calibrate.py NAME")
        return OK
    say("Model history (%d): the verdicts are kept by model, so a model that comes back is not measured again"
        % len(rows))
    for r in rows:
        say("  %-28s %-12s ctx %-7s %s on %s  [%s, %s]" % (
            r["model_id"], r["quant"] or "", r["n_ctx"] or "?", r["date"], r["machine"], r["state"].upper(), r["where"]))
        say("      " + (r["summary"] or ""))
        if r["why"]:
            say("      stale: %s" % r["why"])
    return OK


def cmd_status(a, vault):
    rows = cal.overview(vault, a.services)
    if a.json:
        print(json.dumps({"engines": rows}))
        return OK
    if not rows:
        say("no local engine in the service table")
        return NONE
    say("\n".join(cal.overview_lines(rows)))
    return OK


def cmd_recommend(a, vault):
    out = []
    for e in resolve_engines(a, vault):
        info = cal.probe_engine(e)
        st = cal.status(info, vault)
        rec = st["record"] if st["state"] == "calibrated" else None
        out.append({"engine": e["name"], "state": st["state"], "rung": cal.rung_of(e),
                    "farm_first": cal.farm_first(),
                    "recommended": cal.recommend((rec or {}).get("kinds") or {}, cal.rung_of(e))})
    if a.json:
        print(json.dumps({"engines": out}))
        return OK
    for o in out:
        say("%s (rung %d, farm_first %s): %s" % (o["engine"], o["rung"], o["farm_first"], o["state"]))
        for c, r in o["recommended"].items():
            say("  %-22s start rung %d%s  (%s)" % (probes.LABELS[c], r["rung"],
                                                    ", audit every result" if r["audit_rate"] == 1.0 else "", r["verdict"]))
    return OK


def attempted_today(fp):
    at = cal.cache_load().get("attempts", {}).get(fp)
    return bool(at) and str(at.get("at", ""))[:10] == datetime.date.today().isoformat()


def note_attempt(fp, result):
    d = cal.cache_load()
    d.setdefault("attempts", {})[fp] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "result": result}
    cal.cache_write(d)


def cmd_run(a, vault):
    core = cal.core_module("gt_model_calibrate")
    if core is None:
        say("gt core (gt_model_calibrate.py) was not found: the Claude reference runs need it. Install gt, or set "
            "GT_CORE_SCRIPTS to its scripts folder.")
        return USAGE
    first = cal.farm_first()
    engines = resolve_engines(a, vault)
    if not engines:
        say("no local engine to calibrate: add a `local: yes` chat-completions row to the service table "
            "(farm_services in vault-config.json), or install one: python3 gt_farm_engine.py install NAME ...")
        return NONE
    for e in engines:
        if not e["local"]:
            say("%s is not `local: yes`: the farm sends automatic work to local engines only, so there is "
                "nothing to calibrate for it." % e["name"])
            return USAGE
    if a.auto and first != "auto":
        say("farm_first is %s, so calibration is not started automatically. Run it yourself: python3 "
            "gt_farm_calibrate.py %s" % (first, " ".join(e["name"] for e in engines)))
        return OK
    expected = cal.expected_context(vault)
    infos = [cal.probe_engine(e) for e in engines]
    todo, seen = [], {}
    for e, i in zip(engines, infos):
        if not i["up"]:
            say("%s: DOWN (no answer); nothing to calibrate" % e["name"])
            continue
        fp = i["fingerprint"]
        if fp in seen:
            say("%s: the same model as %s (%s %s): measured once" % (e["name"], seen[fp], i["model_id"], i["quant"] or ""))
            continue
        seen[fp] = e["name"]
        st = cal.status(i, vault, expected)
        if st["state"] == "calibrated" and not a.force:
            r = st["record"]
            say("%s: %s %s is already calibrated (%s on %s, from the %s): reusing it, nothing run. %s" % (
                e["name"], i["model_id"], i["quant"] or "", r["date"], r["machine"], st["where"], r["summary"]))
            continue
        if a.auto and attempted_today(fp):
            say("%s: tried already today; not trying again automatically" % e["name"])
            continue
        if st["state"] in ("stale", "mismatch"):
            say("%s: the earlier calibration is %s (%s): measuring again" % (e["name"], st["state"], st["why"]))
        todo.append((e, i))
    if not todo:
        return OK if any(i["up"] for i in infos) else NONE
    selected, classes = select(a)
    ref_plan, ref_default = cal.reference_plan(vault, a.reference_model)
    adj_model = a.adjudicator_model or cal.adjudicator_alias(vault)
    plan = {"estimate_usd": round(estimate(core, selected, vault, ref_plan, adj_model, len(todo)), 4),
            "budget_usd": budget_usd(core, a)}
    say("Calibrating %d engine(s): %s. %d probes + %d injection tries each; reference answers by %s, judged by %s."
        % (len(todo), ", ".join(e["name"] for e, _ in todo), len(selected), len(probes.INJECTION) * INJECTION_TRIES,
           ", ".join(sorted(set(ref_plan[c] for c in classes))), adj_model))
    say("Estimated Claude cost: %.4f USD (budget %s). The engines' own calls are free." % (
        plan["estimate_usd"], "unlimited" if plan["budget_usd"] >= core.UNLIMITED_USD else "%.4f USD" % plan["budget_usd"]))
    if len(classes) < len(probes.TASK_CLASSES):
        say("Partial: only %s are measured; the other classes get no automatic work until they are." % ", ".join(classes))
    why = guard(core, plan, a)
    if why:
        say(why)
        if a.auto:
            for _e, i in todo:
                note_attempt(i["fingerprint"], "refused")
        return REFUSED
    if a.dry_run:
        say("Dry run: nothing was sent.")
        return OK
    runner = a.runner or os.environ.get("GT_CALIBRATION_RUNNER") or core.saved_runner()
    if not runner:
        say("no runner: the Claude reference answers need one (--runner CMD, $GT_CALIBRATION_RUNNER, or the "
            "model_calibration_runner setting). Nothing was sent.")
        return USAGE
    results = []
    for e, i in todo:
        rec, tot = measure(core, e, i, selected, a, ref_plan, ref_default, adj_model, vault, runner)
        rec["estimate_usd"] = round(plan["estimate_usd"] / len(todo), 4)
        stored = cal.save(rec, vault)
        cal.ledger(e, "calibration", "calibration", tot["prompt"], tot["completion"], tot["wall"],
                   tot["estimated"], model=rec.get("model_id") or "", kept_out_tokens=0, actual_eq=0,
                   counterfactual_eq=0, usd_avoided=0.0, claude_spent_usd=rec["estimate_usd"])
        if a.auto:
            note_attempt(i["fingerprint"], "ran")
        results.append({"engine": e["name"], "record": rec, "stored": stored})
        if not a.json:
            say("\n".join(report_lines(rec, i)))
            v = stored["vault"]
            say("  stored: %s%s" % (("vault %s (%s)" % (v["path"], v["decision"])) if v else "local cache only (no vault)",
                                    "; " + str(v["reason"]) if v and v.get("reason") and v["decision"] != "apply" else ""))
    core.add_spend(plan["estimate_usd"])
    if a.json:
        print(json.dumps({"estimate_usd": plan["estimate_usd"], "engines": results}))
    return OK


def build_parser():
    ap = argparse.ArgumentParser(prog="gt_farm_calibrate.py", description="Onboarding calibration of a farm engine")
    ap.add_argument("service", nargs="?", help="a service of the service table")
    ap.add_argument("--pool")
    ap.add_argument("--new", action="store_true", help="every local engine without a valid calibration")
    ap.add_argument("--base-url")
    ap.add_argument("--name", help="with --base-url: the engine's name")
    ap.add_argument("--model", help="with --base-url: the model id to request")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--recommend", action="store_true")
    ap.add_argument("--force", action="store_true", help="measure again even when a valid calibration exists")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto", action="store_true", help="only when farm_first is auto, for an engine with no "
                    "valid record, once a day per model")
    ap.add_argument("--runner")
    ap.add_argument("--reference-model", choices=("haiku", "sonnet", "opus"),
                    help="answer every reference probe with this Claude tier (default: the class's current tier)")
    ap.add_argument("--adjudicator-model", choices=("haiku", "sonnet", "opus"))
    ap.add_argument("--budget", type=float)
    ap.add_argument("--probes", type=int, default=2, choices=(1, 2), help="probes per class (default 2)")
    ap.add_argument("--classes", help="comma list of task classes (a partial calibration)")
    ap.add_argument("--keep-raw", metavar="DIR", help="save each probe's answers, reference and judgement here")
    ap.add_argument("--parallel", type=int, default=3, help="Claude calls at once (the engine takes one)")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--max-tokens", type=int, default=3000)
    ap.add_argument("--services")
    ap.add_argument("--vault")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.json:
        TO_ERR.append(1)
    vault = fs.vault_path(a.vault)
    if a.list:
        return cmd_list(a, vault)
    if a.status:
        return cmd_status(a, vault)
    if a.recommend:
        return cmd_recommend(a, vault)
    if not (a.service or a.pool or a.new or a.base_url):
        build_parser().print_usage()
        say("name an engine (a service of the table), --pool, --new or --base-url; or --list / --status / --recommend")
        return USAGE
    return cmd_run(a, vault)


if __name__ == "__main__":
    sys.exit(main())
