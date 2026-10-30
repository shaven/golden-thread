#!/usr/bin/env python3
"""gt_farm_setup.py -- what keeps a calibrated local engine from ever being used? (gt-farm, gt 0.22.0)

    gt_farm_send.py setup [--check] [--json] [--skip-setup] [--services FILE]
    gt_farm_send.py setup --apply            make the safe fixes (below)
    gt_farm_send.py setup --apply-template   only copy the ladder table (install.sh uses this)

Five independent gaps, found in practice on 2026-10-21 (calibration passed, and every packet still went to
Claude, silently): (1) no service table with a `local: yes` chat-completions row; (2) `farm_ladder` off;
(3) `pipeline_engine` off; (4) no `<vault>/farm-ladder.tsv` (without it the built-in table starts most task
classes above the local engines); (5) no calibration record (an uncalibrated engine gets no automatic work).
Each is reported alone with its fix. A machine with none of them sees nothing.

--apply sets the two settings (through gt_settings.py) and copies templates/farm-ladder.tsv to the vault,
never overwriting a file that exists (a direct, exclusive create: the write queue takes Markdown only). It never calibrates and never writes a
service table: those need you (the calibration command is named). --skip-setup prints nothing, so a person
who knows what they are doing is never nagged.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_farm_calibration as cal  # noqa: E402
import gt_farm_send as fs  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(HERE), "templates", "farm-ladder.tsv")
SETTINGS_FIX = "python3 ~/.claude/golden-thread/hooks/gt_settings.py set %s on"


def _gap(gap, what, fix):
    return {"gap": gap, "what": what, "fix": fix}


def gaps(vault=None, services=None):
    vault = vault or fs.vault_path()
    out = []
    path = fs.services_path(services, vault)
    local = cal.table_engines(services, vault)
    if not path or not os.path.isfile(path):
        out.append(_gap("services", "farm_services is not set, or the service table it names is missing",
                        "write a service table (FARM-LOCAL-ENGINES.md section 6) and set farm_services to it"))
    elif not local:
        out.append(_gap("services", "the service table has no `local: yes` chat-completions row",
                        "add a row for your engine (python3 gt_farm_engine.py install NAME --model FILE --port N prints it)"))
    if not fs.ladder_on():
        out.append(_gap("farm_ladder", "farm_ladder is off, so packets are not tried on local engines first",
                        SETTINGS_FIX % "farm_ladder"))
    if not fs.pipeline_on():
        out.append(_gap("pipeline_engine", "pipeline_engine is off, so ingest stages do not use a local engine",
                        SETTINGS_FIX % "pipeline_engine"))
    if vault and not os.path.isfile(os.path.join(vault, "farm-ladder.tsv")):
        out.append(_gap("ladder_table", "<vault>/farm-ladder.tsv is missing, so the built-in table starts most task "
                        "classes above your local engines",
                        "python3 gt_farm_send.py setup --apply-template   (lower the rung starts after calibration)"))
    if not (cal.vault_records(vault) or cal.cache_load().get("models")):
        out.append(_gap("calibration", "no engine has a calibration record, and an uncalibrated engine gets no automatic work",
                        "python3 gt_farm_calibrate.py NAME --dry-run   (then without --dry-run)"))
    return out


def copy_template(vault):
    """-> (done, message). Creates <vault>/farm-ladder.tsv from the template; an existing file is never touched
    (exclusive create). This is a direct write on purpose: the write queue takes Markdown only, and this is a
    plain config file the installer places, like the tools it puts in the vault."""
    if not vault or not os.path.isdir(vault):
        return False, "no vault to copy the ladder table into"
    dest = os.path.join(vault, "farm-ladder.tsv")
    try:
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        with open(dest, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except FileExistsError:
        return False, "farm-ladder.tsv already exists; left alone"
    except OSError as exc:
        return False, "could not copy the ladder table (%s); copy templates/farm-ladder.tsv by hand" % type(exc).__name__
    return True, "copied templates/farm-ladder.tsv to the vault"


def set_on(name):
    core = cal.core_scripts()
    tool = os.path.join(core, "gt_settings.py") if core else None
    if not tool or not os.path.isfile(tool):
        return False, "gt_settings.py was not found"
    p = subprocess.run([sys.executable, tool, "set", name, "on"], capture_output=True, text=True, timeout=60)
    if p.returncode == 0:
        return True, "%s on" % name
    last = (p.stdout + p.stderr).strip().splitlines()
    return False, last[-1] if last else "gt_settings.py failed"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_farm_send.py setup", description=__doc__.split("\n")[1])
    ap.add_argument("--check", action="store_true", help="report the gaps (the default)")
    ap.add_argument("--apply", action="store_true", help="set farm_ladder and pipeline_engine on and copy the ladder table")
    ap.add_argument("--apply-template", action="store_true", help="only copy the ladder table (install.sh)")
    ap.add_argument("--skip-setup", action="store_true", help="print nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--services")
    a = ap.parse_args(argv)
    vault = fs.vault_path()
    if a.apply_template:
        ok, msg = copy_template(vault)
        print("farm setup: " + msg)
        return 0
    if a.skip_setup:
        return 0
    found = gaps(vault, a.services)
    if a.apply:
        done = []
        names = {g["gap"] for g in found}
        for name in ("farm_ladder", "pipeline_engine"):
            if name in names:
                ok, msg = set_on(name)
                done.append(("ok: " if ok else "could not: ") + str(msg))
        if "ladder_table" in names:
            ok, msg = copy_template(vault)
            done.append(("ok: " if ok else "could not: ") + msg)
        for d in done:
            print("farm setup: " + d)
        found = gaps(vault, a.services)
    if a.json:
        print(json.dumps({"gaps": found}))
        return 0
    if not found:
        print("farm routing is set up: a local engine, the ladder and pipeline on, a ladder table, a calibration.")
        return 0
    for g in found:
        print("gap: %s -- %s\n     fix: %s" % (g["gap"], g["what"], g["fix"]))
    if not a.apply:
        print("`gt_farm_send.py setup --apply` makes the safe fixes (settings and the ladder table); "
              "the service table and the calibration need you. `--skip-setup` hides this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
