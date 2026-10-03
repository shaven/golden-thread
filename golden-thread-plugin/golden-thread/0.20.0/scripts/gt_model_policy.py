#!/usr/bin/env python3
"""gt_model_policy.py -- the model and effort each INSTALLED gt skill runs at (0.19.1).

    gt_model_policy.py table   [--profile P] [--vault V]       # what a profile means, printed
    gt_model_policy.py choose  P [--home H]                    # record the profile
    gt_model_policy.py current [--home H]                      # the recorded profile, or nothing
    gt_model_policy.py apply   [--profile P] [--home H] [--vault V]
    gt_model_policy.py verify  [--home H]                      # exit 1 on a hand edit
    gt_model_policy.py set     (--skill S | --plugin P) --model M [--effort E] [--home H]
    gt_model_policy.py set     --agent A --model M [--home H]      # A: a stage or a job type
    gt_model_policy.py clear   (--skill S | --plugin P | --agent A) [--home H]
    gt_model_policy.py show    [--home H] [--vault V] [--json]

Claude Code honours `model:` and `effort:` in a skill's frontmatter. gt writes them into the
INSTALLED copies only -- the plugin cache and the marketplace directory -- never the release
source, and records exactly what it wrote in ~/.claude/golden-thread/model-policy-applied.json,
so a later edit to the same field is told apart from gt's own work (`verify`).

Profiles (owner, 2026-10-02):
  average    the intent pack (gt_model): fast haiku with no effort, balanced sonnet medium,
             deep opus high. The shipped default for a new install.
  very-high  opus xhigh for every skill.
  inherit    nothing written: every skill runs on the session's model and effort, as before.

Resolution for one skill, highest first: the user's per-skill override, the per-plugin override,
the skill's model_intent through the profile, else inherit. An effort the model does not accept
is refused, never written (gt_model.effort_problem) -- Claude Code would lower it silently.

AGENTS ARE SET BY THE TASK, NOT THE PROFILE (owner, 2026-10-02: "doc reading doesn't need
opus"). A specialist agent (gt_agent_spec.py) runs at its spec's model_tier through the intent
pack -- fast haiku, standard sonnet, careful opus -- whichever profile the skills use, so a
very-high machine still reads documents on sonnet. The `agent_models` setting (task, the
default, or session) turns it off: session passes no model at all. Highest first: a per-agent override for the job type
(extract-docs), then for its stage (extract), then the tier. The model is passed as the Agent
tool's `model`, which takes an alias only and has no effort parameter, so an agent override is
a model alias and nothing else.
Exit: 0 ok, 1 a problem (refused combination, verify drift), 2 usage.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import gt_model  # noqa: E402

OK, PROBLEM, USAGE = 0, 1, 2
MARKET = "golden-thread-plugin"
PROFILES = ("average", "very-high", "inherit")
VERY_HIGH = ("opus", "xhigh")
AGENT_MODELS = ("haiku", "sonnet", "opus", "fable")   # what the Agent tool's `model` accepts
FIELDS = ("model", "effort")


def paths(home):
    home = Path(home or Path.home()).expanduser()
    return {"home": home,
            "choices": home / ".claude" / "golden-thread" / "install-choices.json",
            "record": home / ".claude" / "golden-thread" / "model-policy-applied.json",
            "installed": home / ".claude" / "plugins" / "installed_plugins.json",
            "market": home / ".claude" / "plugins" / "marketplaces" / MARKET / "plugins"}


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes((json.dumps(data, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, p)


def model_choices(P):
    d = _load(P["choices"], {})
    m = d.get("model") if isinstance(d, dict) else None
    m = m if isinstance(m, dict) else {}
    return {"profile": m.get("profile"), "plugins": m.get("plugins") or {},
            "skills": m.get("skills") or {}, "agents": m.get("agents") or {}}


def installed_skills(P):
    """-> [(plugin, skill name, path)] for every installed copy of a gt-marketplace skill."""
    out = []
    data = _load(P["installed"], {})
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    for key, entries in (plugins.items() if isinstance(plugins, dict) else []):
        name, _, market = key.partition("@")
        if market != MARKET:
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            root = Path(str((e or {}).get("installPath") or ""))
            for f in sorted(root.glob("skills/*/SKILL.md")):
                out.append((name, f.parent.name, f))
        for f in sorted((P["market"] / name).glob("skills/*/SKILL.md")):
            out.append((name, f.parent.name, f))
    return out


def resolve_one(plugin, skill, path, profile, choices, vault):
    """-> (model or None, effort or None, source). Raises ValueError on a refused effort."""
    for scope, key in (("skill", skill), ("plugin", plugin)):
        o = choices[scope + "s"].get(key)
        if isinstance(o, dict) and o.get("model"):
            bad = gt_model.effort_problem(o["model"], o.get("effort"))
            if bad:
                raise ValueError("%s override for %s: %s" % (scope, key, bad))
            return o["model"], o.get("effort"), "%s override" % scope
    if profile == "inherit":
        return None, None, "inherit"
    intent = gt_model.read_intent(path)[0]
    if intent not in gt_model.INTENTS:
        return None, None, "inherit (no model_intent)"
    if profile == "very-high":
        return VERY_HIGH[0], VERY_HIGH[1], "very-high profile"
    res = gt_model.resolve(intent, vault)            # raises ValueError on a refused effort
    if not res.get("model"):
        return None, None, "inherit (intent unmapped)"
    return res["model"], res.get("effort"), "intent %s (average profile)" % intent


def agent_model(job, stage, tier, home=None, vault=None):
    """-> (model alias or None, source) for a specialist agent. None means the session's model.
    Raises ValueError for a tier that is not one."""
    if _agent_setting() == "session":
        return None, "agent_models is session"
    choices = model_choices(paths(home))
    for key in (job, stage):
        o = choices["agents"].get(key) if key else None
        if isinstance(o, dict) and o.get("model") in AGENT_MODELS:
            return o["model"], "agent override (%s)" % key
    intent = gt_model.intent_for_tier(tier)
    res = gt_model.resolve(intent, vault)
    model = (res.get("model") or "").lower()
    alias = next((a for a in AGENT_MODELS if a in model), None)
    if not alias:
        return None, "task tier %s -> %s, unmapped" % (tier, intent)
    return alias, "task tier %s -> %s" % (tier, intent)


def _agent_setting():
    try:
        import gt_settings                                # noqa: E402
        return gt_settings.get("agent_models") or "task"
    except Exception:                                     # noqa: BLE001 - the default, never fail
        return "task"


def agent_rows(home=None, vault=None):
    """-> [{stage, tier, model, source}] for every shipped agent stage."""
    import gt_agent_spec                                  # noqa: E402 -- only `show` needs it
    specs, _ = gt_agent_spec.load_specs(vault)
    rows = []
    for stage in gt_agent_spec.AGENT_STAGES:
        e = specs.get(stage)
        if not e:
            continue
        tier = e["data"]["model_tier"]
        model, source = agent_model(stage, stage, tier, home, vault)
        rows.append({"stage": stage, "tier": tier, "model": model, "source": source})
    return rows


def rewrite(path, model, effort):
    """Set (or remove) model:/effort: in a SKILL.md's frontmatter. -> True when it changed."""
    # Bytes, not read_text (0.20.0): text mode turns CRLF into "\n" and the rewrite then
    # wrote the file back LF, so on Windows (a CRLF checkout) "inherit" did not restore the
    # bytes the install laid down. The file keeps the line ending it came with.
    text = Path(path).read_bytes().decode("utf-8")
    crlf = "\r\n" in text
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return False
    head = [l for l in lines[1:end] if l.split(":", 1)[0].strip() not in FIELDS]
    if model:
        head.append("model: %s" % model)
        if effort:
            head.append("effort: %s" % effort)
    new = "\n".join([lines[0]] + head + lines[end:])
    if crlf:
        new = new.replace("\n", "\r\n")
    if new == text:
        return False
    Path(path).write_bytes(new.encode("utf-8"))
    return True


def current_fields(path):
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
    got = {}
    for l in lines[1:end]:
        k, _, v = l.partition(":")
        if k.strip() in FIELDS:
            got[k.strip()] = v.strip()
    return got.get("model"), got.get("effort")


def plan(P, profile, vault):
    choices = model_choices(P)
    profile = profile or choices["profile"] or "inherit"
    rows, refused = [], []
    for plugin, skill, path in installed_skills(P):
        try:
            model, effort, source = resolve_one(plugin, skill, path, profile, choices, vault)
        except ValueError as exc:
            refused.append("%s/%s: %s" % (plugin, skill, exc))
            continue
        rows.append({"plugin": plugin, "skill": skill, "path": str(path), "model": model,
                     "effort": effort, "source": source})
    return profile, rows, refused


def cmd_apply(a):
    P = paths(a.home)
    if a.profile and a.profile not in PROFILES:
        return _bad_profile(a.profile)
    profile, rows, refused = plan(P, a.profile, a.vault)
    changed = sum(rewrite(r["path"], r["model"], r["effort"]) for r in rows)
    _atomic(P["record"], {"version": 1, "profile": profile,
                          "applied": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "files": {r["path"]: {"model": r["model"], "effort": r["effort"],
                                                "source": r["source"]} for r in rows}})
    print("model policy: profile %s -- %d installed skill file(s), %d changed"
          % (profile, len(rows), changed))
    for r in refused:
        print("  REFUSED %s" % r, file=sys.stderr)
    return PROBLEM if refused else OK


def cmd_verify(a):
    P = paths(a.home)
    rec = _load(P["record"], None)
    if not isinstance(rec, dict):
        print("model policy: never applied on this machine -- nothing to verify")
        return OK
    drift = []
    recorded = rec.get("files") or {}
    for _plugin, skill, path in installed_skills(P):
        want = recorded.get(str(path))
        have = current_fields(path)
        if want is None:
            if any(have):
                drift.append("%s: model/effort %s present but not written by the policy"
                             % (path, have))
            continue
        for k, h in zip(FIELDS, have):
            if (want.get(k) or None) != (h or None):
                drift.append("%s (%s): %s is %r, the policy wrote %r"
                             % (path, skill, k, h, want.get(k)))
    for d in drift:
        print("  DRIFT " + d)
    print("model policy: %s" % ("%d file(s) edited since the policy wrote them" % len(drift)
                                if drift else "every installed skill is as the policy wrote it"))
    return PROBLEM if drift else OK


def profile_table(profile, vault):
    if profile == "inherit":
        return [(i, None, None) for i in gt_model.INTENTS]
    if profile == "very-high":
        return [(i,) + VERY_HIGH for i in gt_model.INTENTS]
    out = []
    for i in gt_model.INTENTS:
        r = gt_model.resolve(i, vault)
        out.append((i, r.get("model"), r.get("effort")))
    return out


def cmd_table(a):
    profile = a.profile or model_choices(paths(a.home))["profile"] or "average"
    if profile not in PROFILES:
        return _bad_profile(profile)
    rows = profile_table(profile, a.vault)
    print("Model profile: %s" % profile)
    for intent, model, effort in rows:
        print("  %-9s %s" % (intent, "session model and effort" if not model
                             else "%s · %s" % (model, effort or "no effort setting")))
    if any(e in ("high", "xhigh", "max") for _i, _m, e in rows):
        print("  Note: efforts above medium use more of your Claude allowance.")
    return OK


def cmd_choose(a):
    if a.profile not in PROFILES:
        return _bad_profile(a.profile)
    P = paths(a.home)
    d = _load(P["choices"], {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("version", 1)
    d.setdefault("choices", {})      # the module-choice shape every other reader expects
    d.setdefault("model", {})["profile"] = a.profile
    _atomic(P["choices"], d)
    print("model policy: profile %s recorded" % a.profile)
    return OK


def cmd_current(a):
    p = model_choices(paths(a.home))["profile"]
    if p:
        print(p)
    return OK


def _edit_override(a, value):
    P = paths(a.home)
    d = _load(P["choices"], {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("version", 1)
    d.setdefault("choices", {})
    scope, key = (("skills", a.skill) if a.skill else ("agents", a.agent)
                  if getattr(a, "agent", None) else ("plugins", a.plugin))
    bucket = d.setdefault("model", {}).setdefault(scope, {})
    if value is None:
        bucket.pop(key, None)
    else:
        bucket[key] = value
    _atomic(P["choices"], d)
    # Re-applied at once: a choice that waits for the next install is a choice nobody sees.
    return cmd_apply(argparse.Namespace(home=a.home, vault=a.vault, profile=None))


def cmd_set(a):
    if a.agent:
        if a.model not in AGENT_MODELS:
            print("gt_model_policy: refused -- an agent's model is passed to the Agent tool, "
                  "which takes one of %s, got %r" % (", ".join(AGENT_MODELS), a.model),
                  file=sys.stderr)
            return USAGE
        if a.effort:
            print("gt_model_policy: refused -- the Agent tool has no effort parameter, so an "
                  "agent runs at the session's effort; set --model only", file=sys.stderr)
            return USAGE
        return _edit_override(a, {"model": a.model})
    bad = gt_model.effort_problem(a.model, a.effort)
    if bad:
        print("gt_model_policy: refused -- %s" % bad, file=sys.stderr)
        return USAGE
    v = {"model": a.model}
    if a.effort:
        v["effort"] = a.effort
    return _edit_override(a, v)


def cmd_clear(a):
    return _edit_override(a, None)


def cmd_show(a):
    profile, rows, refused = plan(paths(a.home), None, a.vault)
    agents = agent_rows(a.home, a.vault)
    if a.json:
        print(json.dumps({"profile": profile, "skills": rows, "agents": agents,
                          "refused": refused}, indent=2))
        return PROBLEM if refused else OK
    print("Model profile: %s (skills)" % profile)
    for r in rows:
        print("  %-14s %-22s %-8s %-8s %s" % (r["plugin"], r["skill"], r["model"] or "session",
                                             r["effort"] or "-", r["source"]))
    print("Agents (by task; effort is the session's):")
    for r in agents:
        print("  %-14s %-22s %-8s %-8s %s" % ("agent", r["stage"], r["model"] or "session", "-",
                                             r["source"]))
    for r in refused:
        print("  REFUSED %s" % r)
    return PROBLEM if refused else OK


def _bad_profile(p):
    print("gt_model_policy: profile %r is not one of %s" % (p, ", ".join(PROFILES)),
          file=sys.stderr)
    return USAGE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_model_policy.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="the HOME whose install is changed (default: yours)")
    common.add_argument("--vault", help="vault whose local packs apply (Core rule 2: named)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("apply", cmd_apply), ("table", cmd_table)):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("--profile")
        p.set_defaults(fn=fn)
    p = sub.add_parser("choose", parents=[common])
    p.add_argument("profile")
    p.set_defaults(fn=cmd_choose)
    for name, fn in (("current", cmd_current), ("verify", cmd_verify)):
        sub.add_parser(name, parents=[common]).set_defaults(fn=fn)
    for name, fn in (("set", cmd_set), ("clear", cmd_clear)):
        p = sub.add_parser(name, parents=[common])
        who = p.add_mutually_exclusive_group(required=True)
        who.add_argument("--skill")
        who.add_argument("--plugin")
        who.add_argument("--agent", help="a specialist agent: a stage (extract) or a job type "
                         "(extract-docs)")
        if name == "set":
            p.add_argument("--model", required=True)
            p.add_argument("--effort")
        p.set_defaults(fn=fn)
    p = sub.add_parser("show", parents=[common])
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
