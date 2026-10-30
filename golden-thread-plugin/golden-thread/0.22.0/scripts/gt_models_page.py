#!/usr/bin/env python3
"""gt_models_page.py -- the Models and cost page: `/gt:gt-settings models` (0.21.0).

    gt_settings.py models [--json]
    gt_settings.py models set <agent-type> <model> [<effort>]     (--skill NAME | --plugin NAME for those)
    gt_settings.py models reset <agent-type>                      (--skill NAME | --plugin NAME)
    gt_settings.py models profile average|very-high|inherit
    gt_settings.py models apply-calibration [<agent-type>|all]
    gt_settings.py models advise [--agent T] [--days N] [--context N] [--json]   (suggest only, 0.22.0)
    gt_settings.py models farm | gt_settings.py farm              # only the local-engines view (0.21.4)

One table, one row per AGENT TYPE, so the user can see what each agent runs on and what it costs
instead of taking the defaults at face value:
  Pipeline stages   classify, draft, extract, place, reconcile, verify, generalize, then the job
                    types under them (extract-docs ...)
  Module agents     gt-lotr:*, gt:disk-reader, any other installed agent that declares a model_intent
  Skills by intent  fast, balanced, deep, with a count of skills in each
  Skills (measured) one row per installed skill: its model and effort, how many runs the skill run
                    log holds, and its calibration verdict and date, or "not calibrated" (0.21.4)
Columns: model · effort, WHY (override / profile / task tier / inherit), runs, tokens and an
estimated cost over the last 7 days, and the last calibration verdict (keep / move-down / move-up /
raise-effort, lower-effort, its date, its agreement) or "not calibrated" / "not enough runs".

LOCAL ENGINES (0.21.4, gt-farm). Under the tables the page shows, per local engine of the service table: its
current model, whether a calibration for THAT model exists (and from which date and machine), and the verdicts;
the model history (`gt_farm_calibrate.py --list`); and, for the user's persona, the routine work a local engine
can take for them (`gt_persona.py farm-tasks`). An engine whose model has no calibration is shown as getting
no automatic work. The view needs the gt-farm module; with `farm_first` off it is one line saying so.

Everything that changes a row goes through gt_model_policy.py (choose, apply, set, clear), which
applies at once. Nothing here picks a model itself, and `apply-calibration` applies only stored
verdicts, only those asked for, after printing each one; it never runs by itself.

USAGE. The savings ledger's agent-models events carry an `agent` field when the recorder knew the
agent type; otherwise the Claude Code subagent transcripts are read (gt_savings' reader; each
transcript's <name>.meta.json names its agentType). Cost is the tokens at list price: an
estimate, labelled one. The session's own model is Claude Code's (`/model`); gt never changes it.
"""
import datetime
import glob
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

import gt_model                                          # noqa: E402
import gt_model_policy as mp                             # noqa: E402

OK, REFUSED, USAGE = 0, 1, 2
DAYS = 7
G_STAGES, G_MODULE, G_SKILLS = "Pipeline stages", "Module agents", "Skills by intent"
G_SKILL_ROWS = "Skills (measured)"
CAL_SETTINGS = ("model_calibration", "model_calibration_sample", "model_calibration_budget",
                "model_calibration_runner", "model_calibration_min_runs",
                "model_calibration_remind_days")


def home():
    return os.path.expanduser("~")


def latest_path():
    return os.path.join(home(), ".claude", "golden-thread", "model-calibration", "latest.json")


def load_latest():
    try:
        with open(latest_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def find_vault():
    v = os.environ.get("GT_VAULT")
    if v:
        return v
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("vault_path")
        return v if v and os.path.isdir(v) else None
    except (OSError, ValueError, AttributeError):
        return None


def setting(name):
    try:
        import gt_settings
        return gt_settings.get(name)
    except Exception:                                    # noqa: BLE001
        return None


# -- usage -------------------------------------------------------------------------------------
def _load_savings():
    """gt_savings (the gt-usage module's reader), from this tree, the hooks dir or the installed
    plugin; None when it is not there (usage is then shown as unavailable)."""
    cands = []
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    # newest release first, compared as numbers: as strings 0.1.9 would beat 0.1.11
    cands += sorted(glob.glob(os.path.join(root, "golden-thread-usage", "*", "scripts", "gt_savings.py")),
                    key=lambda q: [int(x) if x.isdigit() else 0 for x in
                                   os.path.basename(os.path.dirname(os.path.dirname(q))).split(".")],
                    reverse=True)
    cands.append(os.path.join(home(), ".claude", "golden-thread", "hooks", "gt_savings.py"))
    try:
        with open(os.path.join(home(), ".claude", "plugins", "installed_plugins.json"),
                  encoding="utf-8") as fh:
            ent = (json.load(fh).get("plugins", {}).get("gt-usage@golden-thread-plugin") or [])
        for e in ent:
            cands.append(os.path.join(str(e.get("installPath") or ""), "scripts", "gt_savings.py"))
    except (OSError, ValueError, AttributeError):
        pass
    for path in cands:
        if os.path.isfile(path):
            # gt_savings puts script folders at the FRONT of sys.path (one of them is a release of gt
            # named by its own GT_VERSION constant). Left there, a gt module imported later resolves to
            # that release's copy -- in a source tree, an older one that lacks a newer setting.
            kept = list(sys.path)
            try:
                spec = importlib.util.spec_from_file_location("gt_savings_for_models", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:                            # noqa: BLE001
                continue
            finally:
                sys.path[:] = kept
    return None


def _parse_ts(text):
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def usage_by_agent(days=DAYS, now=None):
    """-> {agent type: {runs, tokens, cost_usd, state}} for the last `days` days, or None when the
    reader is unavailable. Ledger events that name their agent count first; every other subagent
    transcript with a meta file counts once."""
    sv = _load_savings()
    if sv is None:
        return None
    now = now or time.time()
    since = now - days * 86400
    out = {}

    def add(agent, usage, model, runs=1, tokens=None):
        e = out.setdefault(agent, {"runs": 0, "tokens": 0, "cost_usd": 0.0, "state": "estimated",
                                   "unpriced_runs": 0, "models": {}})
        e["runs"] += runs
        if model:                                        # runs per model: the advisor's input
            e["models"][str(model)] = e["models"].get(str(model), 0) + runs
        e["tokens"] += tokens if tokens is not None else sv.raw_tokens(usage)
        price, known = sv.price_for(model)
        if known:
            e["cost_usd"] += sv.weighted(usage) * price / 1e6
        else:                                            # a model with no price row adds no dollars
            e["unpriced_runs"] += runs

    counted = set()
    try:
        ledger = sv.read_ledger(os.path.join(home(), ".claude", "golden-thread", "usage",
                                             "savings.jsonl"))
    except Exception:                                    # noqa: BLE001
        ledger = {}
    for ev in ledger.values():
        ts = _parse_ts(ev.get("ts"))
        if ev.get("mechanism") != "agent-models" or not ev.get("agent") or ts is None \
                or ts < since:
            continue
        add(ev["agent"], ev.get("usage") or {}, ev.get("model"), tokens=ev.get("actual_tokens"))
        counted.add(ev["key"].rsplit(":", 1)[-1])
    projects = os.path.join(home(), ".claude", "projects")
    seen = set()
    for path in sorted(glob.glob(os.path.join(projects, "**", "subagents", "agent-*.jsonl"),
                                 recursive=True)):
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
                agent = json.load(fh).get("agentType")
        except (OSError, ValueError, AttributeError):
            continue
        if not agent or sv.opaque(path[len(projects):].lstrip(os.sep)) in counted:
            continue
        rec = sv.read_file(path, seen, datetime.datetime.fromtimestamp(
            since, datetime.timezone.utc))
        if rec and rec["turns"]:
            add(agent, rec["usage"], sv.modal(rec["models"]))
    for e in out.values():
        e["cost_usd"] = round(e["cost_usd"], 4)
    return out


# -- the page ----------------------------------------------------------------------------------
def _calibration(stage, latest):
    st = (latest.get("stages") or {}).get(stage)
    if isinstance(st, dict) and st.get("verdict"):
        return {"verdict": st["verdict"], "date": st.get("date"), "agreement": st.get("agreement")}
    why = (latest.get("not_calibrated") or {}).get(stage) or ""
    return {"verdict": "not enough runs" if "needed" in why else "not calibrated",
            "date": None, "agreement": None}


def run_counts():
    """-> {skill: {recorded, completed}} from the skill run log (0.21.4); {} when it has none or
    gt_skill_runlog.py is not here."""
    try:
        import gt_skill_runlog as rl
        out = {}
        for r in rl.read_runs():
            e = out.setdefault(r.get("skill"), {"recorded": 0, "completed": 0})
            e["recorded"] += 1
            e["completed"] += r.get("outcome") == "completed"
        return out
    except Exception:                                    # noqa: BLE001 - the page never fails for it
        return {}


def _skill_min_runs():
    try:
        import gt_skill_runlog as rl
        return rl.min_runs()
    except Exception:                                    # noqa: BLE001
        return 5


def _skill_calibration(skill, latest, counts, need):
    """A skill's last measured verdict and its date, else "not calibrated" with how many of the
    runs a verdict needs the log holds. Never a verdict the pass did not measure."""
    st = (latest.get("skills") or {}).get(skill)
    if isinstance(st, dict) and st.get("verdict"):
        return {"verdict": st["verdict"], "date": st.get("date"), "agreement": st.get("agreement"),
                "measured": True}
    c = counts.get(skill) or {"completed": 0}
    return {"verdict": "not calibrated", "date": None, "agreement": None, "measured": False,
            "reason": "%d of %d runs" % (c["completed"], need)}


def session_model():
    try:
        with open(os.path.join(home(), ".claude", "settings.json"), encoding="utf-8") as fh:
            m = json.load(fh).get("model")
        if m:
            return str(m)
    except (OSError, ValueError, AttributeError):
        pass
    return os.environ.get("ANTHROPIC_MODEL") or "the Claude Code default"


def build(vault=None, days=DAYS):
    vault = vault or find_vault()
    import gt_agent_spec
    P = mp.paths(None)
    profile = mp.model_choices(P)["profile"] or "inherit"
    latest = load_latest()
    usage = usage_by_agent(days)
    rows = []

    def usage_of(agent):
        if usage is None:
            return None
        return usage.get(agent) or {"runs": 0, "tokens": 0, "cost_usd": 0.0, "state": "estimated",
                                    "unpriced_runs": 0}

    agent_rows = mp.agent_rows(None, vault)
    specs, _ = gt_agent_spec.load_specs(vault)
    for r in agent_rows:
        if ":" in r["stage"]:
            continue
        stage = r["stage"]
        rows.append({"type": stage, "group": G_STAGES, "job_type": False, "model": r["model"],
                     "effort": r["effort"], "why": r["source"], "tier": r["tier"],
                     "usage": usage_of("gt:" + stage), "calibration": _calibration(stage, latest)})
        for job in sorted(specs):
            if job in gt_agent_spec.AGENT_STAGES or job.partition("-")[0] != stage:
                continue
            tier = (specs[job].get("data") or {}).get("model_tier")
            if not tier:
                continue
            try:
                m, e, src = mp.agent_settings(job, stage, tier, None, vault)
            except ValueError as exc:
                m, e, src = None, None, "REFUSED %s" % exc
            rows.append({"type": job, "group": G_STAGES, "job_type": True, "model": m, "effort": e,
                         "why": src, "tier": tier, "usage": None, "calibration": None})
    for r in agent_rows:
        if ":" not in r["stage"]:
            continue
        rows.append({"type": r["stage"], "group": G_MODULE, "job_type": False, "model": r["model"],
                     "effort": r["effort"], "why": r["source"], "tier": r["tier"],
                     "usage": usage_of(r["stage"]), "calibration": None})
    _prof, srows, _refused = mp.plan(P, None, vault)
    by_intent = {}
    seen = set()
    for r in srows:
        if (r["plugin"], r["skill"]) in seen:
            continue
        seen.add((r["plugin"], r["skill"]))
        try:
            intent = gt_model.read_intent(r["path"])[0]
        except (OSError, UnicodeDecodeError):
            continue
        if intent in gt_model.INTENTS:
            by_intent.setdefault(intent, []).append(r)
    for intent in gt_model.INTENTS:
        group = by_intent.get(intent, [])
        if not group:
            continue
        combos = {}
        for r in group:
            combos[(r["model"], r["effort"], r["source"])] = \
                combos.get((r["model"], r["effort"], r["source"]), 0) + 1
        (m, e, src), _n = max(combos.items(), key=lambda kv: kv[1])
        over = sum(1 for r in group if "override" in r["source"])
        rows.append({"type": intent, "group": G_SKILLS, "job_type": False, "count": len(group),
                     "model": m, "effort": e,
                     "why": src + ("; %d overridden" % over if over else ""),
                     "overridden": over, "tier": intent, "usage": None, "calibration": None})
    counts, need = run_counts(), _skill_min_runs()
    seen = set()
    for r in sorted(srows, key=lambda x: x["skill"]):
        if r["skill"] in seen:
            continue
        seen.add(r["skill"])
        try:
            intent = gt_model.read_intent(r["path"])[0]
        except (OSError, UnicodeDecodeError):
            continue
        if intent not in gt_model.INTENTS:
            continue
        c = counts.get(r["skill"]) or {"recorded": 0, "completed": 0}
        rows.append({"type": r["skill"], "group": G_SKILL_ROWS, "job_type": False,
                     "plugin": r["plugin"], "model": r["model"], "effort": r["effort"],
                     "why": r["source"], "tier": intent, "usage": None, "runs": c,
                     "calibration": _skill_calibration(r["skill"], latest, counts, need)})
    last = latest.get("updated")
    header = {"profile": profile, "session_model": session_model(),
              "agent_models": setting("agent_models"),
              "calibration": {k[len("model_calibration"):].lstrip("_") or "mode": setting(k)
                              for k in CAL_SETTINGS},
              "last_pass": (last or "")[:10] or None, "days": days,
              "usage_available": usage is not None,
              "skill_runlog": {"mode": setting("skill_runlog") or "off", "min_runs": need,
                               "recorded": sum(c["recorded"] for c in counts.values())}}
    return {"header": header, "rows": rows, "farm": farm_data(vault)}


def farm_data(vault):
    """-> the local-engines view as data: {first, installed, engines, history, tasks, personas}. Never raises:
    the page is not a place to fail for an optional module."""
    out = {"first": setting("farm_first") or "suggest", "installed": False, "engines": [], "history": [],
           "tasks": [], "personas": []}
    if out["first"] == "off":
        return out
    try:
        import gt_farm_link
        lib = gt_farm_link.farm_module()
    except Exception:                                    # noqa: BLE001
        lib = None
    if lib is None:
        return out
    out["installed"] = True
    try:
        out["engines"] = lib.overview(vault, None)
        out["history"] = lib.history(vault)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        import gt_persona
        names = gt_persona.resolve(vault, None)
        out["personas"], out["tasks"] = names, gt_persona.farm_tasks(names)
    except Exception:                                    # noqa: BLE001
        pass
    return out


def farm_lines(f):
    """-> the local-engines view as lines (None-safe)."""
    if f["first"] == "off":
        return ["", "Local engines: farm_first is off, so gt neither offers nor uses them "
                    "(gt_settings.py set farm_first suggest)"]
    if not f["installed"]:
        return []
    if not f["engines"] and not f["history"]:
        return ["", "Local engines: none configured (farm_first: %s). To get one: python3 gt_farm_engine.py install "
                    "NAME --model FILE --port N (see FARM-LOCAL-ENGINES.md), add its row to your farm_services "
                    "table, then python3 gt_farm_calibrate.py NAME" % f["first"]]
    out = ["", "Local engines (gt-farm; farm_first: %s)" % f["first"]]
    try:
        import gt_farm_link
        lib = gt_farm_link.farm_module()
        out += lib.overview_lines(f["engines"]) if f["engines"] else ["  (none in the service table)"]
    except Exception:                                    # noqa: BLE001
        pass
    if f["history"]:
        out.append("Model history: %d calibrated (python3 gt_farm_calibrate.py --list)" % len(f["history"]))
    if f["tasks"]:
        try:
            import gt_persona
            lines = gt_persona.farm_tasks_lines(f["tasks"], f["engines"])
            lines[0] = "Work your local engines can take for you (%s):" % ", ".join(f["personas"])
            out += [""] + lines
        except Exception:                                # noqa: BLE001
            pass
    return out


def _usage_text(u):
    if u is None:
        return "-"
    if not u["runs"]:
        return "0 runs"
    t = u["tokens"]
    tok = "%.1fk" % (t / 1000) if t >= 1000 else str(t)
    return "%d run%s · %s tok · ≈$%.2f estimated%s" % (
        u["runs"], "" if u["runs"] == 1 else "s", tok, u["cost_usd"],
        " (%d unpriced)" % u["unpriced_runs"] if u.get("unpriced_runs") else "")


def _cal_text(c):
    if not c:
        return "-"
    if c["date"]:
        return "%s %s%s" % (c["verdict"], c["date"], " agreement %.2f" % c["agreement"]
                            if c.get("agreement") is not None else "")
    if c.get("reason"):
        return "%s (%s)" % (c["verdict"], c["reason"])
    return c["verdict"]


def _model_text(r):
    if not r["model"]:
        return "session model"
    return "%s · %s" % (r["model"], r["effort"]) if r["effort"] else r["model"]


def _next_calibration():
    """The schedule line (0.21.0, item 44): the setting, the last run and when the next is due."""
    try:
        import gt_calibration_schedule as cs
        return cs.describe(cs.schedule())
    except Exception as e:                               # noqa: BLE001
        return "could not be worked out (%s)" % type(e).__name__


def render(d):
    h = d["header"]
    cal = h["calibration"]
    out = ["Models and cost   (cost is an estimate: tokens at list price; last %d days)" % h["days"],
           "profile: %s · session model: %s (set with /model in Claude Code; gt never changes it) · "
           "agent_models: %s" % (h["profile"], h["session_model"], h["agent_models"]),
           "calibration: %s · sample %s · budget %s · runner %s · min runs %s · remind %s days · "
           "last pass %s" % (cal.get("mode"), cal.get("sample"), cal.get("budget"),
                             "set" if cal.get("runner") else "none (Agent tool)",
                             cal.get("min_runs"), cal.get("remind_days"),
                             h["last_pass"] or "never")]
    out.append("next calibration: %s" % _next_calibration())
    if not h["usage_available"]:
        out.append("usage: the gt-usage module is not installed, so runs and cost are not shown")
    for group in (G_STAGES, G_MODULE, G_SKILLS, G_SKILL_ROWS):
        rows = [r for r in d["rows"] if r["group"] == group]
        if not rows:
            continue
        if group == G_SKILL_ROWS:
            sk = h["skill_runlog"]
            measured = [r for r in rows if r["calibration"]["measured"]]
            if sk["mode"] != "on" and not sk["recorded"] and not measured:
                out += ["", "%s   skill_runlog is %s, so no skill run is recorded and no skill is "
                        "calibrated (gt_settings.py set skill_runlog on)" % (group, sk["mode"])]
                continue
            out += ["", "%s   skill_runlog %s; a verdict needs %d completed runs; %d run(s) recorded"
                    % (group, sk["mode"], sk["min_runs"], sk["recorded"])]
        else:
            out += ["", group]
        for r in rows:
            name = ("  " + r["type"]) if r["job_type"] else r["type"]
            if group == G_SKILLS:
                name = "%s (%d skill%s)" % (r["type"], r["count"], "" if r["count"] == 1 else "s")
            shown = _usage_text(r["usage"])
            if group == G_SKILL_ROWS:
                shown = "%d run%s recorded" % (r["runs"]["recorded"],
                                               "" if r["runs"]["recorded"] == 1 else "s")
            out.append("  %-26s %-16s %-34s %-40s %s" % (
                name, _model_text(r), r["why"][:34], shown, _cal_text(r["calibration"])))
    out += farm_lines(d["farm"]) if d.get("farm") else []
    out += ["", "Change a row:",
            "  models set <agent-type> <model> [<effort>]   e.g. models set verify sonnet high",
            "  models reset <agent-type>                     back to the profile or task tier",
            "  models profile average|very-high|inherit      the profile, applied at once",
            "  models apply-calibration [<agent-type>|all]   apply stored calibration verdicts",
            "  models advise [--agent T] [--days N] [--context N]   would Sonnet have sufficed? (suggest only)",
            "Re-check them: /gt:gt-calibrate  (runs the calibration pass now, whatever "
            "model_calibration says; budget and weekly-allowance guards still apply)",
            "Prefix with: python3 gt_settings.py  (or use /gt:gt-settings)"]
    return "\n".join(out)


# -- changing rows -----------------------------------------------------------------------------
def known_types(d):
    return [r["type"] for r in d["rows"] if r["group"] not in (G_SKILLS, G_SKILL_ROWS)]


def _policy(argv):
    v = find_vault()
    return mp.main(argv + (["--vault", v] if v else []))


def _scope_args(args):
    """-> (["--agent", T] | ["--skill", S] | ["--plugin", P], rest, label) from the argv."""
    for flag in ("--skill", "--plugin"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                return None, args, None
            return [flag, args[i + 1]], args[:i] + args[i + 2:], args[i + 1]
    if not args:
        return None, args, None
    return ["--agent", args[0]], args[1:], args[0]


def _row_text(t):
    r = next((x for x in build()["rows"] if x["type"] == t), None)
    return "%s   (%s)" % (_model_text(r), r["why"]) if r else ""


def cmd_set(args):
    scope, rest, label = _scope_args(args)
    if not scope or not rest:
        print("usage: models set <agent-type> <model> [<effort>]   (or --skill NAME / --plugin NAME)")
        return USAGE
    model = rest[0]
    effort = rest[1] if len(rest) > 1 else None
    if scope[0] == "--agent":
        types = known_types(build())
        if label not in types:
            print("refused: %r is not an agent type here. Known: %s" % (label, ", ".join(types)))
            return REFUSED
    bad = gt_model.effort_problem(model, effort)
    if bad:
        allowed = gt_model.allowed_efforts(model)
        print("refused: %s. %s accepts: %s. Nothing was changed." % (
            bad, model, ", ".join(allowed) if allowed else "no effort level"
            if allowed is not None else "(unknown model)"))
        return REFUSED
    argv = ["set"] + scope + ["--model", model] + (["--effort", effort] if effort else [])
    rc = _policy(argv)
    if rc == 0 and scope[0] == "--agent":
        print("set %s: %s" % (label, _row_text(label)))
    return rc


def cmd_reset(args):
    scope, rest, label = _scope_args(args)
    if not scope:
        print("usage: models reset <agent-type>   (or --skill NAME / --plugin NAME)")
        return USAGE
    rc = _policy(["clear"] + scope)
    if rc == 0 and scope[0] == "--agent":
        print("reset %s: %s" % (label, _row_text(label)))
    return rc


def cmd_profile(args):
    if len(args) != 1 or args[0] not in mp.PROFILES:
        print("usage: models profile %s" % "|".join(mp.PROFILES))
        return USAGE
    return set_profile(args[0])


def set_profile(value):
    """Record the profile and re-apply at once (also `gt_settings.py set model_profile`)."""
    rc = _policy(["choose", value])
    if rc:
        return rc
    rc = _policy(["apply"])
    print("model_profile: %s" % value)
    return rc


def cmd_apply_calibration(args):
    latest = load_latest()
    stages = latest.get("stages") if isinstance(latest.get("stages"), dict) else {}
    want = args[0] if args else "all"
    if want == "all":
        names = sorted(stages)
    elif want in stages:
        names = [want]
    else:
        print("refused: no stored calibration verdict for %r (run /gt:gt-calibrate first). "
              "Stored: %s" % (want, ", ".join(sorted(stages)) or "none"))
        return REFUSED
    todo, rc = [], OK
    for n in names:
        v = stages[n]
        line = "  %-10s %s %s%s" % (n, v.get("verdict"), v.get("date") or "",
                                    "  agreement %.2f" % v["agreement"]
                                    if v.get("agreement") is not None else "")
        if v.get("verdict") in ("move-down", "move-up", "raise-effort", "lower-effort") and v.get("model"):
            print("%s  ->  %s" % (line, "%s · %s" % (v["model"], v["effort"]) if v.get("effort")
                                  else v["model"]))
            todo.append((n, v))
        elif want != "all":
            print("%s\nnothing to apply: a %s verdict recommends no change" % (line, v.get("verdict")))
            rc = REFUSED
    if not todo:
        if want == "all":
            print("nothing to apply: no stored verdict recommends a change")
        return rc
    for n, v in todo:
        argv = ["set", "--agent", n, "--model", v["model"]] + (
            ["--effort", v["effort"]] if v.get("effort") else [])
        r = _policy(argv)
        print("applied %s: %s" % (n, "ok" if r == 0 else "FAILED (exit %d)" % r))
        rc = rc or r
    return rc


def cli(args):
    if not args or args[0] == "--json":
        d = build()
        print(json.dumps(d, indent=1) if args else render(d))
        return OK
    verb, rest = args[0], args[1:]
    if verb == "farm":
        f = farm_data(find_vault())
        if "--json" in rest:
            print(json.dumps(f, indent=1, default=str))
        else:
            print("\n".join(farm_lines(f)).lstrip("\n") or "gt-farm is not installed")
        return OK
    if verb == "advise":                                  # Opus-vs-Sonnet advisor (0.22.0, phase 104)
        import gt_model_advise
        return gt_model_advise.main(rest)
    fn = {"set": cmd_set, "reset": cmd_reset, "profile": cmd_profile,
          "apply-calibration": cmd_apply_calibration}.get(verb)
    if fn is None:
        print("usage: models [--json] | set <type> <model> [<effort>] | reset <type> | "
              "profile <average|very-high|inherit> | apply-calibration [<type>|all] | advise | farm")
        return USAGE
    return fn(rest)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
