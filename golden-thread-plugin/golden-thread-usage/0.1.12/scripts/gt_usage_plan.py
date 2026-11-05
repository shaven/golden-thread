#!/usr/bin/env python3
"""gt_usage_plan.py -- the /gt-usage:usage-plan command (gt-usage 0.1.12, gt 0.22.1).

    gt_usage_plan.py [show]                 the plan in effect and what it set
    gt_usage_plan.py changes                what the plan changed, each setting with its value before
    gt_usage_plan.py off | min | max        turn the plan off (the values from before come back) or on
    gt_usage_plan.py hold [on|off|status] [--session ID]
                                            hold every automatic cut for THIS session (a bare `hold` is `hold on`)
    gt_usage_plan.py farm --base-url URL [--model NAME] [--name NAME] [--runner CMD] [--dry-run]
                                            calibrate the engine at URL; only when it passes, make the safe
                                            farm fixes and turn farm_share on

The plan itself lives in gt core: show/changes/off/min/max run `gt_settings.py usage-plan <verb>` and print its
output unchanged, so this command and `/gt:gt-settings` can never disagree. `hold` runs `gt_auto_clear.py hold`
with this session's id (--session, else $CLAUDE_CODE_SESSION_ID, $CLAUDE_SESSION_ID or $GT_SESSION_ID, as
gt_compact.py learns it); a hold stops all four automatic cuts (auto_clear, auto_compact, the expiry guard's idle
cut and the open-session path) for that session only. A bare `hold` is `hold on`.

`farm` is glue over gt-farm, never a new routing path:
  1. gt_farm_calibrate.py --base-url URL [--name] [--model] --json   (the engine's onboarding calibration)
  2. only when that passes (exit 0 and at least one task class `can-take`):
     gt_farm_setup.py --apply   (farm_ladder, pipeline_engine on, the ladder table copied; never the service table)
     gt_settings.py set farm_share on
A refusal (a budget guard, no runner, no engine answering) or a calibration with no `can-take` class changes
nothing: farm_share stays as it was and the reason is printed. Finding an engine by itself is not done here.

Standard library only; Python 3.8 safe.
Exit: 0 done | 1 refused or failed (the reason is printed) | 2 usage, or a part of gt is not installed.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "hooks")
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID")
PLAN_VERBS = ("show", "changes", "off", "min", "max")
USAGE = ("usage: gt_usage_plan.py [show|changes|off|min|max]\n"
         "       gt_usage_plan.py hold [on|off|status] [--session ID]\n"
         "       gt_usage_plan.py farm --base-url URL [--model NAME] [--name NAME] [--runner CMD] [--dry-run]")


def _versions(d):
    try:
        names = [v for v in os.listdir(d) if re.match(r"^\d+\.\d+\.\d+$", v)]
    except OSError:
        return []
    return sorted(names, key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)


def core_dirs():
    """Where gt core's scripts may be, in order: $GT_CORE_SCRIPTS; the stable hooks dir and this folder (where the
    installer puts this script beside gt_settings.py, as the sibling gt-usage scripts expect); a source tree
    (golden-thread/<newest>/scripts beside this module)."""
    out = [os.environ.get("GT_CORE_SCRIPTS") or "", HOOKS, HERE]
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    for name in ("golden-thread", "gt"):
        out += [os.path.join(root, name, v, "scripts") for v in _versions(os.path.join(root, name))]
    # the installed plugin tree, where gt_settings.py has every module it imports beside it (the hooks dir does not
    # carry gt_models_page.py, which `set token_profile` and `set model_profile` need)
    try:
        sys.path.insert(0, HOOKS)
        import gt_components
        root = gt_components.plugin_root_from_settings()
        for name in ("golden-thread", "gt"):
            out += [os.path.join(root, name, v, "scripts") for v in _versions(os.path.join(root, name))] if root else []
    except Exception:                                             # noqa: BLE001 - no installed plugin is a state
        pass
    return [d for d in out if d]


def core_script(name, needs=()):
    """The first core folder that has `name` and every file in `needs` beside it."""
    return next((os.path.join(d, name) for d in core_dirs() if os.path.isfile(os.path.join(d, name))
                 and all(os.path.isfile(os.path.join(d, n)) for n in needs)), None)


SETTINGS_NEEDS = ("gt_models_page.py",)

def farm_script(name):
    """A gt-farm script, found the way gt core finds it (gt_farm_link.py), else None."""
    link = core_script("gt_farm_link.py")
    if link:
        d = os.path.dirname(link)
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import gt_farm_link                                   # noqa: E402
            found = gt_farm_link.farm_script(name)
            if found:
                return found
        except Exception:                                         # noqa: BLE001 - a missing module is a state
            pass
    cands = [os.environ.get("GT_FARM_SCRIPTS") or ""]
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    cands += [os.path.join(root, "golden-thread-farm", v, "scripts")
              for v in _versions(os.path.join(root, "golden-thread-farm"))]
    cands.append(HOOKS)
    return next((os.path.join(c, name) for c in cands if c and os.path.isfile(os.path.join(c, name))), None)


def run(args, timeout=None):
    """-> (exit code, stdout, stderr) of `python3 args...`."""
    try:
        p = subprocess.run([sys.executable] + list(args), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout, p.stderr


def passthrough(args, timeout=120):
    rc, out, err = run(args, timeout)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def session_id(given=""):
    if given:
        return given
    for var in SESSION_ENV:
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return ""


# ---- show / changes / off / min / max ------------------------------------------------------------------

def cmd_plan(verb):
    tool = core_script("gt_settings.py", SETTINGS_NEEDS)
    if not tool:
        print("gt_settings.py was not found (gt core is not installed here), so nothing was changed.")
        return 2
    return passthrough([tool, "usage-plan", verb])


# ---- hold ----------------------------------------------------------------------------------------------

def cmd_hold(argv):
    verb, sid, rest = "on", "", list(argv)         # a bare `hold` holds; `hold status` reports
    if rest and rest[0] in ("on", "off", "status"):
        verb = rest.pop(0)
    while rest:
        a = rest.pop(0)
        if a == "--session" and rest:
            sid = rest.pop(0)
        elif a.startswith("--session="):
            sid = a.split("=", 1)[1]
        else:
            print(USAGE)
            return 2
    tool = core_script("gt_auto_clear.py")
    if not tool:
        print("gt_auto_clear.py was not found (gt core is not installed here), so no hold was changed.")
        return 2
    sid = session_id(sid)
    args = [tool, "hold", verb] + (["--session", sid] if sid else [])
    rc, out, err = run(args, 60)
    sys.stdout.write(out)
    sys.stderr.write(err)
    if rc == 2 and not out.strip() and "hold" in err and "invalid choice" in err:
        print("this gt core has no session hold (gt_auto_clear.py hold needs gt 0.22.1 or newer); nothing was held.")
    return rc


# ---- farm ----------------------------------------------------------------------------------------------

def parse_farm(argv):
    opts = {"base_url": "", "model": "", "name": "", "runner": "", "dry_run": False}
    rest = list(argv)
    flags = {"--base-url": "base_url", "--model": "model", "--name": "name", "--runner": "runner"}
    while rest:
        a = rest.pop(0)
        if a == "--dry-run":
            opts["dry_run"] = True
            continue
        key, val = a, None
        if "=" in a and a.startswith("--"):
            key, val = a.split("=", 1)
        if key not in flags:
            return None
        if val is None:
            if not rest:
                return None
            val = rest.pop(0)
        opts[flags[key]] = val
    return opts if opts["base_url"] else None


def _json_tail(text):
    """The last line of `text` that parses as a JSON object, or None."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _engine_name(opts):
    if opts["name"]:
        return opts["name"]
    return re.sub(r"^https?://", "", opts["base_url"].rstrip("/")).split("/")[0].split(":")[0]


def calibration_verdict(cal_tool, opts, out):
    """-> (passed, why). Pass = at least one task class can-take, from this run's records or, when the run reused a
    stored calibration (it prints no record then), from the stored verdicts."""
    data = _json_tail(out) or {}
    recs = [e.get("record") or {} for e in data.get("engines") or []]
    if recs:
        take = sorted(c for r in recs for c, k in (r.get("kinds") or {}).items() if k.get("verdict") == "can-take")
        if take:
            return True, "can take: " + ", ".join(take)
        return False, "calibration found no task class this engine can take without a check"
    rc, eout, _err = run([cal_tool, "--export-json"], 120)
    try:
        rows = json.loads(eout) if rc == 0 else []
    except ValueError:
        rows = []
    name = _engine_name(opts)
    take = sorted(set(r.get("task") for r in rows if isinstance(r, dict) and r.get("verdict") == "can-take"
                      and (r.get("engine") == name or (opts["model"] and r.get("model") == opts["model"]))))
    if take:
        return True, "already calibrated; can take: " + ", ".join(take)
    return False, "no calibration on record for this engine can take any task class"


def cmd_farm(argv):
    opts = parse_farm(argv)
    if opts is None:
        print(USAGE)
        return 2
    cal_tool, setup_tool = farm_script("gt_farm_calibrate.py"), farm_script("gt_farm_setup.py")
    settings = core_script("gt_settings.py", SETTINGS_NEEDS)
    if not (cal_tool and setup_tool):
        print("gt-farm is not installed here (gt_farm_calibrate.py / gt_farm_setup.py not found); farm_share unchanged.")
        return 2
    if not settings:
        print("gt_settings.py was not found (gt core is not installed here); farm_share unchanged.")
        return 2
    args = [cal_tool, "--base-url", opts["base_url"]]
    for flag, key in (("--name", "name"), ("--model", "model"), ("--runner", "runner")):
        if opts[key]:
            args += [flag, opts[key]]
    if opts["dry_run"]:
        rc = passthrough(args + ["--dry-run"], 300)
        print("dry run: nothing was sent and farm_share is unchanged.")
        return rc
    rc, out, err = run(args + ["--json"], 3600)
    sys.stdout.write(err)                      # with --json, the calibration's report lines go to stderr
    if rc != 0:
        print("calibration did not run to a result (gt_farm_calibrate.py exit %d: %s); farm_share unchanged."
              % (rc, {1: "refused by a budget guard", 2: "usage, or no runner for Claude's reference answers",
                      3: "no engine answered at that URL"}.get(rc, "failed")))
        return 1
    passed, why = calibration_verdict(cal_tool, opts, out)
    if not passed:
        print("calibration did not pass (%s); farm_share unchanged." % why)
        return 1
    print("calibration passed (%s)." % why)
    rc = passthrough([setup_tool, "--apply"], 120)
    if rc != 0:
        print("the farm fixes (gt_farm_setup.py --apply) did not complete (exit %d); farm_share unchanged." % rc)
        return 1
    rc = passthrough([settings, "set", "farm_share", "on"], 60)
    if rc != 0:
        print("farm_share could not be turned on (gt_settings.py exit %d)." % rc)
        return 1
    print("farm_share is on: eligible tasks of the classes above may now go to %s." % _engine_name(opts))
    return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    verb = a[0] if a else "show"
    if verb in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if verb in PLAN_VERBS:
        if len(a) > 1:
            print(USAGE)
            return 2
        return cmd_plan(verb)
    if verb == "hold":
        return cmd_hold(a[1:])
    if verb == "farm":
        return cmd_farm(a[1:])
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
