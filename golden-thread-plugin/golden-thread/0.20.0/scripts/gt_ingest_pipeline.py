#!/usr/bin/env python3
"""gt_ingest_pipeline.py -- the deterministic stages of the ingest and promote pipelines, and the
spool packets that hand work from one stage to the next.

    gt_ingest_pipeline.py stages [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py survey <path> --kind K [--project SLUG] [--run R] [--unit-depth N] [--deeper DIR] [--record] [--why TEXT] [--repo-key KEY] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py packet <run> --stage S --unit U --result-file F [--session ID] [--replace] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py fan-in <run> --stage S [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py reconcile <run> [--facts-out F] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py draft <run> [--owner-continue] [--session ID] [--no-drain] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py promote-scan --candidates-file F [--run R] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py promote-plan <run> [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py status <run> [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py workflow-args <run> --stage S [--prompt UNIT=FILE ...] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py packets <run> --stage S --results-file F [--session ID] [--replace] [--json] [--vault V] [--dry-run]
    gt_ingest_pipeline.py cleanup <run> [--json] [--vault V] [--dry-run]

THE SHAPE (owner, 2026-09-30). Ingest and promote are pipelines of small STATELESS stages, so
work can be handed off and run in parallel instead of one agent doing everything:

    ingest   intake-scan -> survey -> extract (per unit, parallel) -> classify (per batch)
             -> reconcile -> draft (per target file)            kind: code|docs|tool|session|wiki
    promote  scan -> verify (zero-context, per candidate) -> generalize -> place -> OWNER APPROVES

The agent stages (extract, classify, reconcile, draft, verify, generalize, place) are specs in
gt_agent_spec.py; the skill spawns them. THIS tool is everything that is not an agent: the unit
split, the intake scan per unit, the packets, fan-in, the contradiction check, the queued writes.

THE PACKET. Each stage's input is the previous stage's packet in the spool, never a session's
memory: `<vault>/Projects/golden-thread/spool/pipeline/<run>/`. An agent packet is
`<stage>/<unit>.json` -- {run_id, pipeline, kind, stage, job_type, unit, refs, input,
output_schema, verification, session_id, created, result} -- created exclusively (one packet per
unit; parallel extract jobs never collide). The tool's own stage outputs sit beside them:
run.json, survey.json, fanin-<stage>.json, reconciled.json, drafted.json, plan.json.

THE THREE STOPS, and nothing else stops an ingest (owner): a direct CONTRADICTION with a fact
already in gt (reconcile, exit 1); a SECURITY issue (a credential or prompt injection found by the
intake scan, an unscannable unit, or an extract agent reporting text that tried to instruct it);
UNSAFE SOURCE CODE (the intake scan). A stop names the KIND and the location, never the content.
`draft` refuses while a stop stands unless the owner said to continue with the rest
(--owner-continue), and a contradicted or stopped unit's findings are never written by it.

UNIT SPLIT. A unit is one top-level folder of the source tree; root files are the unit `.`. It is
tunable per repo: `--unit-depth 2` splits every folder one level deeper, `--deeper packages`
splits only that folder (a monorepo). `--record` saves the split for the repo (key: its folder
name, or --repo-key) in `<vault>/Projects/golden-thread/ingest-units.json`, so the next survey of
that repo starts from it. For the session kind the units are the `## ` segments of the session
notes file, each written to `<notes>.segments/seg-NN.md` beside it (never into the vault).

THE WORKFLOW ROUTE (0.20.0). On a Claude Code with workflows, a stage's units can run as the
plugin's `gt:pipeline-stage` workflow instead of one Agent call each. `workflow-args` is the
deterministic half: it writes each unit's prompt to `<run>/prompts/<stage>/<unit>.md` and prints
the workflow's args (the stage's JSON Schema; per unit the prompt file, its sha256, and the agent
type -- or model and effort -- from gt_agent_spec.agent_route). Extract renders its own prompts,
each through the intake scan, so that route cannot skip it; the other stages take prompts the
skill rendered (`--prompt UNIT=FILE`, checked to be a `render` output for that job). `packets`
records the workflow's result: each unit checked as `packet` checks it. Units still without a
packet are what the next `workflow-args` hands out, which is the resume.

SCRATCH FOLDERS (0.20.0). Every agent unit gets a private scratch folder OUTSIDE the vault,
`~/.gt-scratch/<run>/<stage>-<unit>/` (0700; on Windows under %LOCALAPPDATA% with an owner-only
ACL; gt_scratch.py), named in its prompt as the only place for intermediate files -- the vault
spool holds gt's packets and stage files and nothing an agent made. `workflow-args` creates each
unit's folder and names it in the prompt (`scratch_dir` in the item); `gt_agent_spec.py render
--scratch-run R --scratch-unit U` does the same for an Agent-tool spawn. A run's scratch is removed
when it finishes (`draft` wrote its queue, `promote-plan` reached the owner), and by `cleanup
<run>` when it is abandoned; `status` shows it, and `gt_scratch.py check` reports a finished or
vanished run's scratch as a leak.

PROMOTIONS STAY HUMAN-APPROVED. promote-plan ends at `awaiting-owner` and this tool has no command
that applies a promotion: after the owner's yes, the skill queues the writes itself.

CLAUDE ONLY. Nothing here calls a model or any external service; the agent stages are Claude
subagents the skill spawns (gt_agent_spec.py refuses a spec naming gt-farm).

Vault Markdown is written only through gt_write_queue.py + gt_broker.py (`draft`). The JSON
packets and the split record are gt's own spool/config files. --dry-run writes nothing.

Exit: 0 ok | 1 STOP for the owner (or refused) | 2 usage | 3 incomplete -- not a pass.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_agent_spec as specs                                    # noqa: E402
import gt_scratch                                                # noqa: E402

OK, STOP, USAGE, INCOMPLETE = 0, 1, 2, 3
SPOOL = ("Projects", "golden-thread", "spool", "pipeline")
UNITS_FILE = ("Projects", "golden-thread", "ingest-units.json")
INTAKE = os.path.join(HERE, "gt_intake_scan.py")
KINDS = ("code", "docs", "tool", "session", "wiki")
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
PACKET_VERSION = 1
MAX_FACT_FILE = 2 * 1024 * 1024
MAX_FACT_FILES = 5000
ROUTED = {"research": "Projects/{p}/research.md", "design": "Projects/{p}/design.md",
          "inbox": "INBOX.md"}
SESSION_STEP = {"decisions": "an ADR: allocate it with gt_adr.py (decisions.md is generated)",
                "memory": "a memory note: the skill writes it as its own file",
                "knowledge": "a Knowledge page: capture its source in Sources/ first",
                "global_memory": "global memory: the owner reviews every write there"}
STAGE_INPUT = {"extract": "survey.json", "classify": "fanin-extract.json",
               "reconcile": "fanin-classify.json|fanin-extract.json",
               "draft": "reconciled.json", "verify": "scan.json",
               "generalize": "verify/<unit>.json", "place": "generalize/<unit>.json"}


# -- small helpers --------------------------------------------------------------------------------
def clean(value, limit=160):
    s = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C")
    return s if len(s) <= limit else s[:limit - 3] + "..."


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def find_vault(explicit):
    return specs.find_vault(explicit)


def spool_dir(vault, run):
    return os.path.join(vault, *SPOOL, run)


def unit_slug(unit):
    if unit in (".", ""):
        return "_root"
    s = re.sub(r"[^A-Za-z0-9._-]+", "__", unit.strip("/"))
    return s[:120] or "_unit"


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, data, exclusive=False):
    """Atomic. exclusive: refuse (FileExistsError) when the file is already there."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        if exclusive:
            os.link(tmp, path)          # atomic, and fails if the name exists
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def emit(a, data, lines):
    if getattr(a, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


def fail(msg, code=USAGE):
    print("gt_ingest_pipeline: %s" % msg, file=sys.stderr)
    return code


def need_vault(a):
    v = find_vault(a.vault)
    if not v or not os.path.isdir(v):
        return None
    return v


def load_run(vault, run):
    if not RUN_RE.match(run or "") or ".." in run:
        return None, "%r is not a run id" % clean(run, 60)
    data = read_json(os.path.join(spool_dir(vault, run), "run.json"))
    if not isinstance(data, dict):
        return None, "no run '%s' in %s (start one with survey or promote-scan)" % (
            clean(run, 60), os.path.join(vault, *SPOOL))
    return data, None


def job_for(run, stage):
    if run["pipeline"] == "ingest" and stage in specs.KIND_STAGES:
        return "%s-%s" % (stage, run["kind"])
    return stage


# -- the recorded unit split --------------------------------------------------------------------
def read_units_config(vault):
    data = read_json(os.path.join(vault, *UNITS_FILE)) if vault else None
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"version": 1, "repos": {}}
    return data


def split_for(a, vault, key):
    """-> (depth, deeper, where it came from)."""
    rec = read_units_config(vault)["repos"].get(key) if vault else None
    depth, deeper, src = 1, [], "default (one unit per top-level folder)"
    if isinstance(rec, dict):
        depth = rec.get("unit_depth", 1) if isinstance(rec.get("unit_depth"), int) else 1
        deeper = [d for d in rec.get("deeper", []) if isinstance(d, str)]
        src = "recorded %s%s" % (clean(rec.get("recorded", "?"), 20),
                                 (": " + clean(rec["why"], 120)) if rec.get("why") else "")
    if a.unit_depth is not None or a.deeper:      # flags replace the recorded split whole
        depth = a.unit_depth if a.unit_depth is not None else 1
        deeper = [d.strip("/") for d in (a.deeper or []) if d.strip("/")]
        src = "given on the command line"
    return max(1, depth), deeper, src


def local_depth(top, depth, deeper):
    return max(depth, 2) if top in deeper else depth


def my_unit(scan_unit, depth, deeper):
    if scan_unit == ".":
        return "."
    parts = scan_unit.split("/")
    return "/".join(parts[:local_depth(parts[0], depth, deeper)])


# -- survey -------------------------------------------------------------------------------------
def run_intake(path, depth, vault):
    cmd = [sys.executable, INTAKE, path, "--json", "--unit-depth", str(depth)]
    if vault:
        cmd += ["--vault", vault]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=specs.INTAKE_TIMEOUT)
        rep = json.loads(p.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, "the intake scan could not run (%s); a scan that cannot run is not a " \
                     "pass" % exc.__class__.__name__
    rep["exit"] = p.returncode
    return rep, None


def merge_units(report, depth, deeper):
    """The scan's units folded into this run's units: stop > incomplete > clean."""
    rank = {"clean": 0, "incomplete": 1, "stop": 2}
    out = {}
    for su in report.get("units", []):
        name = my_unit(su["unit"], depth, deeper)
        u = out.setdefault(name, {"unit": name, "status": "clean", "files": 0, "findings": {},
                                  "locations": [], "incomplete": 0})
        u["files"] += su.get("files", 0)
        if rank[su["status"]] > rank[u["status"]]:
            u["status"] = su["status"]
        for f in su.get("findings", []):
            u["findings"][f["kind"]] = u["findings"].get(f["kind"], 0) + 1
            if len(u["locations"]) < 50:
                u["locations"].append({"kind": f["kind"], "file": clean(f["file"]),
                                       "line": f["line"], "rule": clean(f["rule"], 60)})
        u["incomplete"] += len(su.get("incomplete", []))
    return sorted(out.values(), key=lambda u: (u["unit"] != ".", u["unit"]))


def split_session(path):
    """-> [(name, text)] -- one segment per `## ` heading of the session notes."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    segs, cur = [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("## ") and cur:
            segs.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        segs.append("".join(cur))
    segs = [s for s in segs if s.strip()]
    return [("seg-%02d" % (i + 1), s) for i, s in enumerate(segs)]


def cmd_survey(a):
    vault = need_vault(a)
    path = os.path.abspath(a.path)
    if not os.path.exists(path):
        return fail("%s does not exist" % clean(path, 300))
    if a.kind == "session" and not os.path.isfile(path):
        return fail("the session kind takes the session notes FILE (one `## ` per segment)")
    if not vault and not a.dry_run:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    key = a.repo_key or os.path.basename(path.rstrip(os.sep)) or "root"
    depth, deeper, split_src = split_for(a, vault, key)
    if a.kind == "session":
        depth, deeper, split_src = 1, [], "session notes: one unit per `## ` segment"
    scan_depth = max(depth, 2 if deeper else 1)
    report, err = run_intake(path, scan_depth, vault)
    if report is None:
        return fail(err, INCOMPLETE)
    units = merge_units(report, depth, deeper)
    seg_dir = None
    if a.kind == "session":
        whole = units[0] if units else {"status": "incomplete", "files": 0, "findings": {},
                                        "locations": [], "incomplete": 1}
        units = []
        if whole["status"] == "clean":
            seg_dir = os.path.join(os.path.dirname(path),
                                   os.path.splitext(os.path.basename(path))[0] + ".segments")
            for name, text in split_session(path):
                seg = os.path.join(seg_dir, name + ".md")
                units.append({"unit": name, "status": "clean", "files": 1, "findings": {},
                              "locations": [], "incomplete": 0, "path": seg})
                if not a.dry_run:
                    os.makedirs(seg_dir, exist_ok=True)
                    with open(seg, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(text)
        else:
            whole = dict(whole, unit=".")
            units = [whole]
    run = a.run or "%s-%s-%s" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), a.kind,
                                 re.sub(r"[^A-Za-z0-9_-]+", "-", key)[:40].strip("-") or "x")
    if not RUN_RE.match(run) or ".." in run:
        return fail("%r is not a usable run id" % clean(run, 60))
    stops = [u for u in units if u["status"] == "stop"]
    incomplete = [u for u in units if u["status"] == "incomplete"]
    for u in units:
        if u["status"] != "clean":
            continue
        top = u["unit"].split("/")[0]
        ld = local_depth(top, depth, deeper)
        u["render"] = ["render", "extract-%s" % a.kind, "--input",
                       "path=%s" % (u.get("path") or path)]
        if a.kind != "session" and os.path.isdir(path):
            u["render"] += ["--input", "unit=%s" % u["unit"]]
            if ld > 1:
                u["render"] += ["--input", "unit_depth=%d" % ld]
        if a.project:
            u["render"] += ["--input", "project=%s" % a.project]
    survey = {"packet_version": PACKET_VERSION, "run_id": run, "pipeline": "ingest",
              "stage": "survey", "kind": a.kind, "path": path, "project": a.project,
              "repo_key": key, "unit_depth": depth, "deeper": deeper, "split": split_src,
              "segments_dir": seg_dir, "units": units, "intake": {
                  "exit": report.get("exit"), "verdict": clean(report.get("verdict", ""), 400),
                  "skipped_dirs": report.get("skipped_dirs", [])},
              "stopped": [u["unit"] for u in stops + incomplete], "created": now_iso()}
    manifest = {"run_id": run, "pipeline": "ingest", "kind": a.kind, "path": path,
                "project": a.project, "created": survey["created"]}
    recorded = None
    if a.record and a.kind != "session":
        recorded = {"unit_depth": depth, "deeper": deeper, "why": a.why or "",
                    "recorded": datetime.date.today().isoformat()}
    if not a.dry_run:
        d = spool_dir(vault, run)
        if os.path.exists(os.path.join(d, "run.json")):
            return fail("run '%s' already exists; pass a new --run" % run)
        write_json(os.path.join(d, "run.json"), manifest)
        write_json(os.path.join(d, "survey.json"), survey)
        if recorded is not None:
            cfg = read_units_config(vault)
            cfg["repos"][key] = recorded
            write_json(os.path.join(vault, *UNITS_FILE), cfg)
    code = STOP if stops else (INCOMPLETE if incomplete else OK)
    lines = ["survey %s: %s, kind %s -- %d unit(s); split: depth %d%s (%s)" % (
        run, clean(path, 200), a.kind, len(units), depth,
        (", deeper: " + ", ".join(deeper)) if deeper else "", split_src)]
    for u in units:
        detail = ", ".join("%s %d" % (k, n) for k, n in sorted(u["findings"].items()))
        lines.append("UNIT %-24s %-10s %d file(s)%s" % (clean(u["unit"], 60), u["status"].upper(),
                                                         u["files"],
                                                         ("  " + detail) if detail else ""))
        for loc in u["locations"][:20]:
            lines.append("  %-16s %s:%s  %s" % (loc["kind"], loc["file"], loc["line"],
                                                loc["rule"]))
    lines.append(survey["intake"]["verdict"])
    if code == STOP:
        lines.append("STOP -- intake-scan: %d unit(s) have a security or unsafe-code finding "
                     "(kinds above; the content is never shown). Nothing reads them. Ask the "
                     "owner whether to continue with the clean units." % len(stops))
    elif code == INCOMPLETE:
        lines.append("INCOMPLETE -- %d unit(s) could not be fully scanned; not a pass. Ask the "
                     "owner." % len(incomplete))
    else:
        lines.append("clean -- extract each unit in parallel: gt_agent_spec.py <render args "
                     "above in --json>, then `packet %s --stage extract --unit <unit> "
                     "--result-file <agent JSON>`" % run)
    if recorded is not None:
        lines.append("recorded the split for '%s' in %s%s" % (
            key, os.path.join(*UNITS_FILE), " (dry run: not written)" if a.dry_run else ""))
    if a.dry_run:
        lines.append("dry run: nothing written")
    emit(a, dict(survey, exit=code, dry_run=a.dry_run), lines)
    return code


# -- packets ------------------------------------------------------------------------------------
def verification_of(stage, result):
    if stage == "verify":
        return result.get("verdict", "unknown")
    if stage == "extract":
        counts = {}
        for f in result.get("findings", []) if isinstance(result.get("findings"), list) else []:
            v = f.get("verification", "unstated") if isinstance(f, dict) else "unstated"
            v = v if v in ("verified", "inferred", "assumed") else "unstated"
            counts[v] = counts.get(v, 0) + 1
        return counts
    return "unverified"


def _stage_allowed(run):
    if run["pipeline"] == "ingest" and run.get("kind") == "wiki":
        return specs.AGENT_STAGES
    return tuple(s for s in specs.PIPELINES[run["pipeline"]] if s in specs.AGENT_STAGES)


def _unit_problem(vault, run_id, run, stage, unit):
    """-> (message, exit code) when `unit` is not one this stage of this run may take."""
    d = spool_dir(vault, run_id)
    if stage == "extract":
        survey = read_json(os.path.join(d, "survey.json")) or {}
        u = next((x for x in survey.get("units", []) if x["unit"] == unit), None)
        if u is None:
            return "unit '%s' is not in this run's survey" % clean(unit, 80), USAGE
        if u["status"] != "clean":
            return ("unit '%s' stopped at intake-scan (%s); no extract packet is taken for "
                    "it" % (clean(unit, 80), u["status"]), STOP)
    if run["pipeline"] == "promote":
        scan = read_json(os.path.join(d, "scan.json")) or {}
        if unit not in [c["id"] for c in scan.get("candidates", [])]:
            return "'%s' is not a candidate id of this promote run" % clean(unit, 40), USAGE
    return None, OK


def take_packet(vault, run_id, run, stage, unit, raw, session, replace, dry_run, label=None):
    """Check one agent result against its spec and write it as this stage's packet for `unit`.
    -> (exit code, lines, packet or None). Nothing is written on a problem or under dry_run."""
    unit = unit.strip("/") if unit not in (".",) else "."
    msg, code = _unit_problem(vault, run_id, run, stage, unit)
    if msg:
        return code, ["gt_ingest_pipeline: %s" % msg], None
    job = job_for(run, stage)
    try:
        entry = specs._spec_or_exit(job, vault, None)
    except SystemExit:
        return STOP, ["gt_ingest_pipeline: no valid spec for '%s'" % job], None
    if not isinstance(raw, dict):
        return USAGE, ["gt_ingest_pipeline: the result for unit '%s' is not a JSON object"
                       % clean(unit, 80)], None
    result = raw["result"] if isinstance(raw.get("result"), dict) and "job_type" in raw else raw
    created = now_iso()
    record = {"job_type": job, "session_id": session, "created": created, "result": result}
    problems = specs.check_record(entry["data"], record, entry.get("names", ()))
    if problems:
        where = label or "%s/%s" % (stage, clean(unit, 80))
        return STOP, ["INVALID %s: %s" % (where, e) for e in problems], None
    d = spool_dir(vault, run_id)
    packet = {"packet_version": PACKET_VERSION, "run_id": run_id, "pipeline": run["pipeline"],
              "kind": run.get("kind"), "stage": stage, "job_type": job, "unit": unit,
              "refs": [run.get("path")] + ([unit] if stage == "extract" else []),
              "input": STAGE_INPUT.get(stage), "output_schema": entry["data"]["output_schema"],
              "verification": verification_of(stage, result), "session_id": session,
              "created": created, "result": result}
    seen = result.get("instructions_seen") if stage == "extract" else None
    if seen:
        packet["stop"] = "security"
    target = os.path.join(d, stage, unit_slug(unit) + ".json")
    if not dry_run:
        try:
            if replace:
                write_json(target, packet)
            else:
                write_json(target, packet, exclusive=True)
        except FileExistsError:
            return STOP, ["gt_ingest_pipeline: a %s packet for unit '%s' already exists (one "
                          "packet per unit; --replace to supersede it)"
                          % (stage, clean(unit, 80))], None
    code = STOP if seen else OK
    lines = ["%s packet %s/%s.json -- %s" % ("would write" if dry_run else "wrote",
                                            stage, unit_slug(unit), job)]
    lines += ["  " + x for x in specs.summarise(entry["data"], result)][:12]
    if seen:
        lines.append("STOP -- security: the extract agent reported %d place(s) where the "
                     "material tried to instruct it (unit %s). Nothing from this unit is "
                     "written until the owner decides; the text is not shown." % (
                         len(seen) if isinstance(seen, list) else 1, clean(unit, 80)))
    return code, lines, dict(packet, path=target)


def _run_and_stage(a, vault):
    run, err = load_run(vault, a.run)
    if err:
        return None, fail(err)
    allowed = _stage_allowed(run)
    if a.stage not in allowed:
        return None, fail("stage '%s' is not an agent stage of this %s run (%s)" % (
            clean(a.stage, 40), run["pipeline"], ", ".join(allowed)))
    return run, None


def cmd_packet(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, code = _run_and_stage(a, vault)
    if run is None:
        return code
    raw = read_json(a.result_file)
    if not isinstance(raw, dict):
        return fail("%s is not a JSON object" % clean(a.result_file, 200))
    session = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
    code, lines, packet = take_packet(vault, a.run, run, a.stage, a.unit, raw, session,
                                      a.replace, a.dry_run, label=clean(a.result_file, 200))
    if packet is None:
        for line in lines:
            print(line, file=sys.stderr) if line.startswith("gt_ingest_pipeline:") \
                else print(line)
        return code
    emit(a, dict(packet, exit=code, dry_run=a.dry_run), lines)
    return code


def cmd_packets(a):
    """Every result a pipeline-stage workflow returned, each checked and written as its unit's
    packet exactly as `packet` would. A unit whose agent returned nothing is reported, not
    guessed: re-run workflow-args, which lists only the units still without a packet."""
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, code = _run_and_stage(a, vault)
    if run is None:
        return code
    raw = read_json(a.results_file)
    items = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return fail("%s holds no results list (the JSON the pipeline-stage workflow returned)"
                    % clean(a.results_file, 200))
    if isinstance(raw, dict):
        for k, want in (("run", a.run), ("stage", a.stage)):
            if raw.get(k) not in (None, want):
                return fail("the results are for %s '%s', not '%s'" % (
                    k, clean(raw.get(k), 60), clean(want, 60)))
    session = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
    worst, out, lines, missing = OK, [], [], []
    for it in items:
        unit = it.get("unit") if isinstance(it, dict) else None
        if not isinstance(unit, str) or not unit:
            worst = max(worst, USAGE)
            lines.append("gt_ingest_pipeline: a result without a unit name; skipped")
            continue
        if it.get("result") is None:
            missing.append(unit)
            continue
        code, ls, packet = take_packet(vault, a.run, run, a.stage, unit, it["result"], session,
                                       a.replace, a.dry_run)
        worst = STOP if STOP in (worst, code) else max(worst, code)
        lines += ls
        out.append({"unit": unit, "exit": code, "path": packet and packet["path"]})
    if missing:
        lines.append("INCOMPLETE -- %d unit(s) came back with no result (stopped or failed "
                     "agents): %s. Re-run workflow-args to hand them out again."
                     % (len(missing), ", ".join(clean(u, 60) for u in missing[:20])))
        if worst == OK:
            worst = INCOMPLETE
    if a.dry_run:
        lines.append("dry run: nothing written")
    emit(a, {"run": a.run, "stage": a.stage, "packets": out, "missing": missing,
             "exit": worst, "dry_run": a.dry_run}, lines)
    return worst


# -- the workflow route (0.20.0) ------------------------------------------------------------------
WORKFLOW = "gt:pipeline-stage"
MAX_PROMPT_BYTES = 512 * 1024


def _rendered_by_gt(text, job):
    """A prompt file must be what gt_agent_spec.py render printed for this job: it starts with
    the base prompt and names the job. A cheap check, not a signature; it keeps a mistyped or
    hand-written file from going to an agent as if gt had rendered it."""
    return text.startswith(specs.BASE_PROMPT) and ("## Your job: %s\n" % job) in text


def cmd_workflow_args(a):
    try:
        return _workflow_args(a)
    except gt_scratch.ScratchError as exc:
        return fail("no private scratch folder for the agents: %s" % clean(exc, 300), STOP)


def _workflow_args(a):
    """Everything a pipeline-stage workflow needs for one stage, as the JSON to pass it as
    `args`: the stage's JSON Schema (each agent's output is validated against it as it
    returns), and per unit still without a packet: the prompt file, its sha256, and the agent
    type, model and effort to run it at. Extract renders its own prompts -- each through
    gt_agent_spec.render, which runs the intake scan on the unit and refuses unless it is clean
    -- so the scan cannot be skipped by the workflow route. Every other stage takes the prompts
    the skill rendered (--prompt UNIT=FILE)."""
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, code = _run_and_stage(a, vault)
    if run is None:
        return code
    d = spool_dir(vault, a.run)
    job = job_for(run, a.stage)
    try:
        entry = specs._spec_or_exit(job, vault, None)
    except SystemExit:
        return fail("no valid spec for '%s'" % job, STOP)
    have = {p["unit"] for p in packets(d, a.stage)}
    todo, refused = [], []
    if a.stage == "extract":
        if a.prompt:
            return fail("extract renders its own prompts, so every unit passes the intake scan; "
                        "--prompt is for the other stages")
        survey = read_json(os.path.join(d, "survey.json")) or {}
        for u in survey.get("units", []):
            if u.get("status") != "clean" or u["unit"] in have:
                continue
            args = u.get("render") or []
            inputs = {}
            for i, tok in enumerate(args):
                if tok == "--input" and i + 1 < len(args):
                    k, _, v = args[i + 1].partition("=")
                    inputs[k] = v
            try:
                text, _info = specs.render(entry["data"], inputs, vault,
                                           scratch=_scratch_for(a, a.stage, u["unit"]))
            except specs.IntakeRefused as exc:
                refused.append({"unit": u["unit"], "why": clean(exc, 300)})
                continue
            except ValueError as exc:
                refused.append({"unit": u["unit"], "why": clean(exc, 300)})
                continue
            todo.append((u["unit"], text))
    else:
        for raw in a.prompt or []:
            unit, sep, f = raw.partition("=")
            if not sep or not unit or not f:
                return fail("--prompt takes UNIT=FILE, got %r" % clean(raw, 80))
            msg, c = _unit_problem(vault, a.run, run, a.stage, unit)
            if msg:
                return fail(msg, c)
            if unit in have:
                continue
            try:
                if os.path.getsize(f) > MAX_PROMPT_BYTES:
                    return fail("%s is larger than %d bytes" % (clean(f, 200), MAX_PROMPT_BYTES))
                with open(f, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                return fail("--prompt %s: %s" % (clean(unit, 60), clean(exc, 200)))
            if not _rendered_by_gt(text, job):
                return fail("%s is not a prompt gt_agent_spec.py rendered for %s (render it "
                            "with `gt_agent_spec.py render %s ... > FILE`)"
                            % (clean(f, 200), job, job))
            todo.append((unit, text))
        if not a.prompt:
            return fail("the %s stage takes its prompts from the skill: --prompt UNIT=FILE per "
                        "unit, each the output of gt_agent_spec.py render %s" % (a.stage, job))
    atype, model, effort, why = specs.agent_route(entry, job, vault)
    if atype:
        model = effort = None            # the definition carries them; passing them is noise
    items = []
    for unit, text in todo:
        sd = _scratch_for(a, a.stage, unit)
        text = specs.add_scratch(text, sd)            # a skill-rendered prompt may lack it
        pf = os.path.join(d, "prompts", a.stage, unit_slug(unit) + ".md")
        if not a.dry_run:
            os.makedirs(os.path.dirname(pf), exist_ok=True)
            with open(pf, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        items.append({"unit": unit, "label": "%s %s" % (job, unit_slug(unit)),
                      "prompt_file": pf.replace(os.sep, "/"),
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "scratch_dir": sd.replace(os.sep, "/"),
                      "agent_type": atype, "model": model, "effort": effort})
    out = {"workflow": WORKFLOW, "run": a.run, "run_dir": d.replace(os.sep, "/"),
           "stage": a.stage, "job_type": job,
           "schema": specs.json_schema(entry["data"]), "items": items, "refused": refused,
           "done": sorted(have), "agent_why": why}
    if not a.dry_run:
        write_json(os.path.join(d, "workflow-%s.json" % a.stage), out)
    code = STOP if refused else OK
    lines = ["workflow-args %s %s: %d unit(s) to run, %d already have a packet, %d refused"
             % (a.run, a.stage, len(items), len(have), len(refused))]
    lines.append("  agent: %s" % (atype or "the workflow's own agent at model %s, effort %s (%s)"
                                  % (model or "session", effort or "session", why)))
    for r in refused:
        lines.append("  REFUSED %s: %s" % (clean(r["unit"], 60), r["why"]))
    lines.append("run the %s workflow with args = the JSON this prints with --json "
                 "(also saved as workflow-%s.json in the run), then `packets %s --stage %s "
                 "--results-file <its result>`" % (WORKFLOW, a.stage, a.run, a.stage))
    if a.dry_run:
        lines.append("dry run: nothing written")
    emit(a, dict(out, exit=code, dry_run=a.dry_run), lines)
    return code


def _scratch_for(a, stage, unit):
    """The unit's private scratch folder: created (0700, outside the vault) unless a dry run."""
    return gt_scratch.unit_dir(a.run, stage, unit, create=not a.dry_run)


def run_finished(vault, run_id):
    """True when a run is over -- or gone -- so its scratch is a leak if still there."""
    return gt_scratch.run_finished(vault, run_id)


def _drop_scratch(a, lines):
    """Remove a finished run's scratch; one line says so. Never fails the stage."""
    if a.dry_run:
        return
    try:
        if gt_scratch.cleanup(a.run):
            lines.append("scratch removed: %s" % gt_scratch.run_dir(a.run))
    except (OSError, gt_scratch.ScratchError) as exc:
        lines.append("scratch NOT removed (%s): run `cleanup %s`" % (clean(exc, 160), a.run))


def cmd_cleanup(a):
    """Remove an abandoned (or finished) run's scratch folders. The spool is left alone."""
    if not RUN_RE.match(a.run or "") or ".." in a.run:
        return fail("%r is not a run id" % clean(a.run, 60))
    path = gt_scratch.run_dir(a.run)
    present = os.path.isdir(path)
    removed = False
    if present and not a.dry_run:
        try:
            removed = gt_scratch.cleanup(a.run)
        except (OSError, gt_scratch.ScratchError) as exc:
            return fail("could not remove %s: %s" % (path, clean(exc, 200)), STOP)
    lines = [("removed the scratch of %s: %s" % (a.run, path)) if removed else
             ("would remove %s" % path if present else "no scratch for %s" % a.run)]
    emit(a, {"run": a.run, "path": path.replace(os.sep, "/"), "present": present,
             "removed": removed, "dry_run": a.dry_run}, lines)
    return OK


def packets(d, stage):
    sd = os.path.join(d, stage)
    out = []
    try:
        names = sorted(n for n in os.listdir(sd) if n.endswith(".json") and
                       not n.startswith("."))
    except OSError:
        return out
    for n in names:
        p = read_json(os.path.join(sd, n))
        if isinstance(p, dict) and p.get("stage") == stage:
            out.append(p)
    return out


# -- fan-in -------------------------------------------------------------------------------------
def target_for(dest, project):
    if dest in ROUTED and (project or dest == "inbox"):
        return ROUTED[dest].format(p=project)
    if dest in SESSION_STEP:
        return "session-step:%s" % dest
    return None


def cmd_fan_in(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, err = load_run(vault, a.run)
    if err:
        return fail(err)
    d = spool_dir(vault, a.run)
    if a.stage == "extract":
        survey = read_json(os.path.join(d, "survey.json")) or {}
        expected = [u["unit"] for u in survey.get("units", []) if u["status"] == "clean"]
        got = {p["unit"]: p for p in packets(d, "extract")}
        missing = [u for u in expected if u not in got]
        stopped = sorted(u for u, p in got.items() if p.get("stop"))
        findings = []
        for u in expected:
            p = got.get(u)
            if not p or p.get("stop"):
                continue
            for i, f in enumerate(p["result"].get("findings", []), 1):
                if not isinstance(f, dict) or not isinstance(f.get("claim"), str):
                    continue
                findings.append({"id": "%s#%d" % (u, i), "unit": u, "claim": f["claim"],
                                 "citation": f.get("citation", ""),
                                 "verification": f.get("verification", "unstated")})
        out = {"stage": "fanin-extract", "run_id": a.run, "units_done": sorted(got),
               "missing": missing, "stopped": stopped, "findings": findings,
               "created": now_iso()}
    elif a.stage == "classify":
        base = read_json(os.path.join(d, "fanin-extract.json"))
        if not isinstance(base, dict):
            return fail("run `fan-in %s --stage extract` first" % a.run)
        cls = {}
        for p in packets(d, "classify"):
            for c in p["result"].get("classifications", []):
                if isinstance(c, dict) and isinstance(c.get("id"), str):
                    cls[c["id"]] = c
        findings, missing = [], []
        for f in base["findings"]:
            c = cls.get(f["id"])
            if c is None:
                missing.append(f["id"])
                c = {}
            proj = c.get("project") or run.get("project")
            dest = c.get("dest") or "research"
            findings.append(dict(f, project=proj, dest=dest, level=c.get("level", 3),
                                 target=target_for(dest, proj)))
        out = {"stage": "fanin-classify", "run_id": a.run, "missing": missing,
               "stopped": base.get("stopped", []), "findings": findings,
               "defaulted": len(missing), "created": now_iso()}
        missing = []          # an unclassified finding defaults to research; not a stop
    else:
        return fail("fan-in takes --stage extract or classify (promote: use promote-plan)")
    if not a.dry_run:
        write_json(os.path.join(d, "fanin-%s.json" % a.stage), out)
    code = STOP if out["stopped"] else (INCOMPLETE if missing else OK)
    lines = ["fan-in %s: %d finding(s)" % (a.stage, len(out["findings"]))]
    if a.stage == "extract":
        lines[0] += " from %d of %d unit(s)" % (len(out["units_done"]),
                                                 len(out["units_done"]) + len(missing))
    if missing:
        lines.append("INCOMPLETE -- no packet yet for: %s" % ", ".join(map(clean, missing)))
    if out["stopped"]:
        lines.append("STOP -- security: unit(s) %s reported text that tried to instruct the "
                     "agent; their findings are held out. Ask the owner."
                     % ", ".join(map(clean, out["stopped"])))
    emit(a, dict(out, exit=code, dry_run=a.dry_run), lines)
    return code


# -- reconcile: dedupe, and compare with what gt already records ----------------------------------
WORD = re.compile(r"[a-z]+|\d+(?:[.,]\d+)?%?")
NEG = {"not", "no", "never", "cannot", "neither", "nor", "without", "none"}
NT = re.compile(r"\b(\w+)n['’]t\b", re.I)
STOP_WORDS = {"the", "a", "an", "is", "are", "was", "were", "be", "of", "to", "in", "on", "for",
              "and", "or", "it", "its", "this", "that", "with", "as", "at", "by", "from"}


def analyse(sentence):
    """-> (shape, values, negated, words). shape: the sentence with figures as # and negation
    removed; two sentences with one shape and different values or polarity contradict."""
    s = NT.sub(lambda m: m.group(1) + " not", sentence.lower())
    toks = WORD.findall(s)
    neg = any(t in NEG for t in toks)
    shape, vals, words = [], [], 0
    for t in toks:
        if t in NEG:
            continue
        if t[0].isdigit():
            shape.append("#")
            vals.append(t.replace(",", ""))
        else:
            shape.append(t)
            if t not in STOP_WORDS:
                words += 1
    return " ".join(shape), tuple(vals), neg, words


def sentences(text):
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s+|[-*+>]\s+|\d+[.)]\s+|\[[ xX]\]\s+)+", "", line).strip()
        if not line:
            continue
        for s in re.split(r"(?<=[.!?;])\s+", line):
            s = s.strip().rstrip(".!?;")
            if len(s.split()) >= 3:
                out.append(s)
    return out


def fact_files(vault):
    roots = [os.path.join(vault, "Knowledge"), os.path.join(vault, "global-memory")]
    out = []
    proj = os.path.join(vault, "Projects")
    try:
        for slug in sorted(os.listdir(proj)):
            pd = os.path.join(proj, slug)
            for name in ("research.md", "decisions.md", "design.md", "source.md"):
                if os.path.isfile(os.path.join(pd, name)):
                    out.append(os.path.join(pd, name))
            roots.append(os.path.join(pd, "memory"))
    except OSError:
        pass
    for r in roots:
        for dp, dn, fn in os.walk(r):
            dn[:] = [x for x in sorted(dn) if not x.startswith(".")]
            out += [os.path.join(dp, f) for f in sorted(fn) if f.endswith(".md")]
    return out[:MAX_FACT_FILES]


def vault_facts(vault):
    """-> [{fact, ref, shape, values, neg, words}] from the vault's project files, Knowledge
    and global memory. ref = file > section."""
    facts = []
    for path in fact_files(vault):
        try:
            if os.path.getsize(path) > MAX_FACT_FILE:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, vault).replace(os.sep, "/")
        section = ""
        for line in text.splitlines():
            m = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
            if m:
                section = m.group(1)
                continue
            for s in sentences(line):
                shape, vals, neg, words = analyse(s)
                facts.append({"fact": s, "ref": "%s > %s" % (rel, section) if section else rel,
                              "shape": shape, "values": vals, "neg": neg, "words": words})
    return facts


def related(findings, facts, per=5):
    index = {}
    for i, f in enumerate(facts):
        for w in set(f["shape"].split()):
            if len(w) >= 4 and w not in STOP_WORDS:
                index.setdefault(w, []).append(i)
    out = {}
    for fd in findings:
        score = {}
        for w in set(analyse(fd["claim"])[0].split()):
            if len(w) >= 4 and w not in STOP_WORDS:
                for i in index.get(w, [])[:2000]:
                    score[i] = score.get(i, 0) + 1
        best = sorted((i for i, n in score.items() if n >= 3), key=lambda i: -score[i])[:per]
        for i in best:
            out[(facts[i]["fact"], facts[i]["ref"])] = True
    return [{"fact": f, "ref": r} for f, r in out]


def cmd_reconcile(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, err = load_run(vault, a.run)
    if err:
        return fail(err)
    d = spool_dir(vault, a.run)
    src = "fanin-classify.json" if os.path.isfile(os.path.join(d, "fanin-classify.json")) \
        else "fanin-extract.json"
    base = read_json(os.path.join(d, src))
    if not isinstance(base, dict):
        return fail("run `fan-in %s --stage extract` first" % a.run)
    findings = base["findings"]
    facts = vault_facts(vault)
    by_shape = {}
    for f in facts:
        if f["words"] >= 3:
            by_shape.setdefault(f["shape"], []).append(f)
    seen, duplicates, known, contradictions, keep = {}, [], [], [], []
    for fd in findings:
        verdict = None
        for s in sentences(fd["claim"]) or [fd["claim"]]:
            shape, vals, neg, words = analyse(s)
            for f in by_shape.get(shape, []) if words >= 3 else []:
                if f["values"] == vals and f["neg"] == neg:
                    verdict = verdict or ("known", f)
                else:
                    how = "a different figure" if f["values"] != vals else "the opposite claim"
                    verdict = ("contradiction", f, how)
                    break
            if verdict and verdict[0] == "contradiction":
                break
        key = analyse(fd["claim"])
        if verdict and verdict[0] == "contradiction":
            contradictions.append({"finding": fd["id"], "claim": fd["claim"],
                                   "citation": fd.get("citation", ""), "fact": verdict[1]["fact"],
                                   "fact_ref": verdict[1]["ref"], "how": verdict[2],
                                   "by": "script"})
        elif verdict:
            known.append({"finding": fd["id"], "ref": verdict[1]["ref"]})
        elif key in seen:
            duplicates.append({"finding": fd["id"], "of": seen[key], "by": "script"})
        else:
            seen[key] = fd["id"]
            keep.append(fd["id"])
    flags = []
    ids = {fd["id"]: fd for fd in findings}
    for p in packets(d, "reconcile"):
        r = p["result"]
        for c in r.get("contradictions", []):
            fid = c.get("finding") if isinstance(c, dict) else None
            if fid in ids and fid not in [x["finding"] for x in contradictions]:
                contradictions.append({"finding": fid, "claim": ids[fid]["claim"],
                                       "citation": ids[fid].get("citation", ""),
                                       "fact": clean(c.get("fact", ""), 400),
                                       "fact_ref": clean(c.get("fact_ref", ""), 200),
                                       "how": clean(c.get("detail", ""), 300), "by": "agent"})
        for dup in r.get("duplicates", []) or []:
            fid = dup.get("finding") if isinstance(dup, dict) else None
            if fid in keep:
                duplicates.append({"finding": fid, "of": clean(dup.get("of", ""), 120),
                                   "by": "agent"})
        flags += [x for x in r.get("flags", []) or [] if isinstance(x, dict)]
    withheld = {c["finding"] for c in contradictions} | {x["finding"] for x in duplicates}
    keep = [i for i in keep if i not in withheld]
    out = {"stage": "reconciled", "run_id": a.run, "input": src, "facts_checked": len(facts),
           "keep": keep, "duplicates": duplicates, "known": known,
           "contradictions": contradictions, "flags": flags,
           "agent_packets": len(packets(d, "reconcile")), "created": now_iso()}
    if a.facts_out:
        rel = related([ids[i] for i in ids], facts)
        if not a.dry_run:
            with open(a.facts_out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(rel, fh, indent=2, ensure_ascii=False)
        out["facts_out"] = {"path": a.facts_out, "facts": len(rel)}
    if not a.dry_run:
        write_json(os.path.join(d, "reconciled.json"), out)
    code = STOP if contradictions else OK
    lines = ["reconcile %s: %d finding(s) checked against %d fact(s) in gt -- keep %d, "
             "duplicate %d, already recorded %d, contradiction %d" % (
                 a.run, len(findings), len(facts), len(keep), len(duplicates), len(known),
                 len(contradictions))]
    if flags:
        lines.append("skeptic flags: %d (advice for the session, never a gate)" % len(flags))
    if contradictions:
        lines.append("STOP -- contradiction: these findings directly contradict facts already "
                     "in gt. They are NOT written; the owner decides each one.")
        for c in contradictions:
            lines.append("  %s  %s  [%s]" % (clean(c["finding"], 60), clean(c["claim"], 200),
                                             clean(c["citation"], 120)))
            lines.append("    vs  %s  [%s]  (%s)" % (clean(c["fact"], 200),
                                                    clean(c["fact_ref"], 160), clean(c["how"])))
    emit(a, dict(out, exit=code, dry_run=a.dry_run), lines)
    return code


# -- draft: the kept findings, through the write broker -------------------------------------------
def default_entry(target, fds, run, date):
    src = os.path.basename(str(run.get("path") or "")) or run["run_id"]
    if target == "INBOX.md":
        return "".join("- [ ] %s (from ingest of %s)\n" % (f["claim"], src) for f in fds)
    lines = ["## %s: ingest of %s (%s)" % (date, src, run.get("kind")), ""]
    for f in fds:
        v = f.get("verification", "unstated")
        todo = "" if v == "verified" else " TODO - verify"
        lines.append("- %s -- %s (%s)%s" % (f["claim"], f.get("citation") or "no citation", v,
                                             todo))
    return "\n".join(lines) + "\n"


def cmd_draft(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, err = load_run(vault, a.run)
    if err:
        return fail(err)
    d = spool_dir(vault, a.run)
    rec = read_json(os.path.join(d, "reconciled.json"))
    if not isinstance(rec, dict):
        return fail("run `reconcile %s` first: nothing is drafted before reconcile" % a.run)
    survey = read_json(os.path.join(d, "survey.json")) or {}
    stops = []
    if survey.get("stopped"):
        stops.append("intake-scan stopped unit(s): %s" % ", ".join(survey["stopped"]))
    fan = read_json(os.path.join(d, rec.get("input", "fanin-extract.json"))) or {}
    if fan.get("stopped"):
        stops.append("extract agent(s) reported instruction text in unit(s): %s"
                     % ", ".join(fan["stopped"]))
    if rec["contradictions"]:
        stops.append("%d contradiction(s) at reconcile" % len(rec["contradictions"]))
    if stops and not a.owner_continue:
        for s in stops:
            print("STOP -- %s" % clean(s, 300))
        print("draft refused: the ingest is stopped for the owner. If they say to continue "
              "with the rest, re-run with --owner-continue; the stopped findings are still "
              "never written.")
        return STOP
    keep = set(rec["keep"])
    by_target, steps, skipped = {}, [], 0
    for f in fan.get("findings", []):
        if f["id"] not in keep:
            continue
        dest = f.get("dest", "research")
        tgt = f.get("target") or target_for(dest, f.get("project") or run.get("project"))
        if dest == "skip":
            skipped += 1
        elif tgt is None:
            steps.append({"finding": f["id"], "why": "no project: pass --project to survey"})
        elif tgt.startswith("session-step:"):
            steps.append({"finding": f["id"], "dest": dest, "why": SESSION_STEP[dest]})
        else:
            by_target.setdefault(tgt, []).append(f)
    drafts = {p["unit"]: p for p in packets(d, "draft")}
    date = datetime.date.today().isoformat()
    writes = []
    for tgt, fds in sorted(by_target.items()):
        p = drafts.get(tgt)
        if p and p["result"].get("entries"):
            content = "\n".join("%s\n\n%s\n" % (e.get("heading", "").strip(),
                                                e.get("body", "").strip())
                                for e in p["result"]["entries"] if isinstance(e, dict))
            if content and not content.lstrip().startswith("#") and tgt != "INBOX.md":
                content = "## %s: ingest\n\n%s" % (date, content)
        else:
            content = default_entry(tgt, fds, run, date)
        writes.append({"path": tgt, "op": "append", "content": content,
                       "hint": "ingest run %s (%s)" % (a.run, run.get("kind"))})
    import gt_write_queue as wq                                  # noqa: E402
    from pathlib import Path
    vp = Path(vault)
    session = a.session or wq.session_id(None)
    if a.dry_run:
        results = []
        for w in writes:
            req = wq.build(vp, w["path"], w["op"], w["content"], None, session, "session",
                           w["hint"])
            why = wq.validate(req, vp)
            results.append({"path": w["path"], "op": w["op"],
                            "decision": "refused" if why else "would queue",
                            "reason": why or ""})
        note = "dry run"
    else:
        results, note = wq.submit(vp, writes, session=session, drain=not a.no_drain) \
            if writes else ([], None)
    refused = [r for r in results if r.get("decision") in ("refused", "reject")]
    out = {"stage": "drafted", "run_id": a.run, "writes": results, "note": note,
           "session_steps": steps, "skipped": skipped, "owner_continue": a.owner_continue,
           "stops": stops, "created": now_iso()}
    if not a.dry_run:
        write_json(os.path.join(d, "drafted.json"), out)
    code = STOP if refused else OK
    lines = ["draft %s: %d target file(s), %d finding(s) kept" % (a.run, len(writes),
                                                                  len(keep))]
    for r in results:
        lines.append("  %-12s %s%s" % (r.get("decision"), clean(r.get("path"), 160),
                                       ("  (%s)" % clean(r.get("reason"), 160))
                                       if r.get("reason") and r.get("decision") != "apply"
                                       else ""))
    for s in steps:
        lines.append("  session step  %s: %s" % (clean(s["finding"], 60), s["why"]))
    if note:
        lines.append("note: %s" % clean(note, 200))
    if not stops and not refused:
        lines.append("complete -- no owner prompt was needed (no contradiction, security issue "
                     "or unsafe code)")
    _drop_scratch(a, lines)                       # the run is over: its agents are done
    emit(a, dict(out, exit=code, dry_run=a.dry_run), lines)
    return code


# -- promote ------------------------------------------------------------------------------------
def cmd_promote_scan(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    raw = read_json(a.candidates_file)
    if not isinstance(raw, list) or not raw:
        return fail("--candidates-file must hold a non-empty JSON list of {claim, origin}")
    cands = []
    for i, c in enumerate(raw, 1):
        if not (isinstance(c, dict) and isinstance(c.get("claim"), str) and c["claim"].strip()):
            return fail("candidate %d has no claim" % i)
        cands.append({"id": "c%02d" % i, "claim": c["claim"],
                      "origin": clean(c.get("origin", ""), 200),
                      "evidence": c.get("evidence", ""), "level": c.get("level", "knowledge")})
    run = a.run or "%s-promote" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    if not RUN_RE.match(run) or ".." in run:
        return fail("%r is not a usable run id" % clean(run, 60))
    scan = {"packet_version": PACKET_VERSION, "run_id": run, "pipeline": "promote",
            "stage": "scan", "candidates": cands, "created": now_iso()}
    if not a.dry_run:
        dd = spool_dir(vault, run)
        if os.path.exists(os.path.join(dd, "run.json")):
            return fail("run '%s' already exists" % run)
        write_json(os.path.join(dd, "run.json"), {"run_id": run, "pipeline": "promote",
                                                  "kind": None, "path": None,
                                                  "created": scan["created"]})
        write_json(os.path.join(dd, "scan.json"), scan)
    lines = ["promote-scan %s: %d candidate(s)" % (run, len(cands))]
    lines += ["  %s  %s" % (c["id"], clean(c["claim"], 120)) for c in cands]
    lines.append("next: one zero-context verify agent per candidate (gt_agent_spec.py render "
                 "verify), then `packet %s --stage verify --unit <id> --result-file <JSON>`"
                 % run)
    emit(a, dict(scan, dry_run=a.dry_run), lines)
    return OK


def cmd_promote_plan(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    run, err = load_run(vault, a.run)
    if err:
        return fail(err)
    if run["pipeline"] != "promote":
        return fail("'%s' is not a promote run" % a.run)
    d = spool_dir(vault, a.run)
    scan = read_json(os.path.join(d, "scan.json")) or {"candidates": []}
    got = {st: {p["unit"]: p["result"] for p in packets(d, st)}
           for st in ("verify", "generalize", "place")}
    proposals, dropped, waiting = [], [], []
    for c in scan["candidates"]:
        cid = c["id"]
        v = got["verify"].get(cid)
        if v is None:
            waiting.append({"id": cid, "stage": "verify"})
            continue
        if v.get("verdict") != "confirmed":
            dropped.append({"id": cid, "why": "verify: %s" % v.get("verdict")})
            continue
        g = got["generalize"].get(cid)
        if g is None:
            waiting.append({"id": cid, "stage": "generalize"})
            continue
        if not g.get("generalizes"):
            dropped.append({"id": cid, "why": "does not generalize beyond its project"})
            continue
        p = got["place"].get(cid)
        if p is None:
            waiting.append({"id": cid, "stage": "place"})
            continue
        proposals.append({"id": cid, "from": c["origin"], "claim": c["claim"],
                          "statement": g.get("statement"), "scope": g.get("scope"),
                          "target": p.get("target"), "action": p.get("action"),
                          "overlaps": p.get("overlaps", []), "links": p.get("links", []),
                          "index_entry": p.get("index_entry")})
    out = {"stage": "plan", "run_id": a.run, "status": "awaiting-owner",
           "proposals": proposals, "dropped": dropped, "waiting": waiting,
           "created": now_iso()}
    if not a.dry_run:
        write_json(os.path.join(d, "plan.json"), out)
    lines = ["promote-plan %s: %d proposal(s), %d dropped, %d waiting" % (
        a.run, len(proposals), len(dropped), len(waiting))]
    for p in proposals:
        lines.append("  %s  %s -> %s (%s)" % (p["id"], clean(p["from"], 80),
                                             clean(p["target"], 80), p["action"]))
        lines.append("      %s" % clean(p["statement"], 200))
    for x in dropped:
        lines.append("  %s  dropped: %s" % (x["id"], x["why"]))
    for x in waiting:
        lines.append("  %s  waiting on %s" % (x["id"], x["stage"]))
    lines.append("OWNER APPROVAL REQUIRED -- nothing has been written. Promotions stay "
                 "human-approved: show each proposal, and queue the writes only for the ones "
                 "the owner says yes to.")
    if not waiting:
        _drop_scratch(a, lines)                   # every agent stage is done
    emit(a, dict(out, dry_run=a.dry_run), lines)
    return INCOMPLETE if waiting else OK


# -- status / stages ----------------------------------------------------------------------------
def run_status(vault, run_id):
    run, err = load_run(vault, run_id)
    if err:
        return None, err
    d = spool_dir(vault, run_id)
    st = {"run_id": run_id, "pipeline": run["pipeline"], "kind": run.get("kind"), "stages": []}

    def add(name, state, detail=""):
        st["stages"].append({"stage": name, "state": state, "detail": detail})

    if run["pipeline"] == "ingest":
        sv = read_json(os.path.join(d, "survey.json")) or {}
        units = sv.get("units", [])
        add("intake-scan", "stop" if sv.get("stopped") else "done",
            "%d unit(s), %d stopped" % (len(units), len(sv.get("stopped", []))))
        add("survey", "done", "split depth %s" % sv.get("unit_depth"))
        clean_units = [u["unit"] for u in units if u["status"] == "clean"]
        ex = {p["unit"] for p in packets(d, "extract")}
        add("extract", "done" if clean_units and set(clean_units) <= ex else
            ("waiting" if clean_units else "skipped"),
            "%d of %d packet(s)" % (len(ex & set(clean_units)), len(clean_units)))
        add("classify", "done" if os.path.isfile(os.path.join(d, "fanin-classify.json"))
            else "optional", "%d packet(s)" % len(packets(d, "classify")))
        rec = read_json(os.path.join(d, "reconciled.json"))
        add("reconcile", "waiting" if rec is None else
            ("stop" if rec["contradictions"] else "done"),
            "" if rec is None else "%d contradiction(s)" % len(rec["contradictions"]))
        dr = read_json(os.path.join(d, "drafted.json"))
        add("draft", "waiting" if dr is None else "done",
            "" if dr is None else "%d write(s)" % len(dr["writes"]))
        stops = [s["stage"] for s in st["stages"] if s["state"] == "stop"]
        if stops and dr is not None and dr.get("owner_continue"):
            st["verdict"] = ("complete for the rest, on the owner's word; held at %s: never "
                             "written" % ", ".join(stops))
        elif stops:
            st["verdict"] = "stopped at %s for the owner" % ", ".join(stops)
        elif dr is not None:
            st["verdict"] = "complete -- no owner prompt was needed"
        else:
            st["verdict"] = "in progress"
    else:
        sc = read_json(os.path.join(d, "scan.json")) or {"candidates": []}
        add("scan", "done", "%d candidate(s)" % len(sc["candidates"]))
        for name in ("verify", "generalize", "place"):
            add(name, "packets", "%d packet(s)" % len(packets(d, name)))
        plan = read_json(os.path.join(d, "plan.json"))
        add("approve", "awaiting-owner" if plan else "waiting",
            "" if plan is None else "%d proposal(s)" % len(plan["proposals"]))
        st["verdict"] = "awaiting the owner's approval" if plan else "in progress"
    sp = gt_scratch.run_dir(run_id)
    st["scratch"] = {"path": sp.replace(os.sep, "/"), "present": os.path.isdir(sp)}
    return st, None


def cmd_status(a):
    vault = need_vault(a)
    if not vault:
        return fail("no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)")
    st, err = run_status(vault, a.run)
    if err:
        return fail(err)
    lines = ["run %s (%s%s)" % (a.run, st["pipeline"],
                                (", kind %s" % st["kind"]) if st["kind"] else "")]
    lines += ["  %-12s %-15s %s" % (s["stage"], s["state"], s["detail"]) for s in st["stages"]]
    if st["scratch"]["present"]:
        lines.append("  scratch      %s%s" % (st["scratch"]["path"],
                                              "  (the run is finished: `cleanup %s`)" % a.run
                                              if run_finished(vault, a.run) else ""))
    lines.append(st["verdict"])
    emit(a, st, lines)
    return STOP if st["verdict"].startswith("stopped") else OK


def cmd_stages(a):
    rows = []
    for pipe, stages in specs.PIPELINES.items():
        for s in stages:
            who = ("agent (Claude subagent)" if s in specs.AGENT_STAGES else
                   "owner" if s == "approve" else "tool")
            par = {"intake-scan": "per unit", "extract": "per unit", "classify": "per batch",
                   "draft": "per target file", "verify": "per candidate",
                   "generalize": "per candidate", "place": "per candidate"}.get(s, "one")
            job = ("%s-<kind>" % s) if pipe == "ingest" and s in specs.KIND_STAGES else \
                (s if s in specs.AGENT_STAGES else "-")
            rows.append({"pipeline": pipe, "stage": s, "runs": who, "parallel": par,
                         "job_type": job})
    lines = ["%-8s %-12s %-24s %-16s %s" % ("PIPELINE", "STAGE", "RUNS AS", "PARALLEL",
                                             "JOB TYPE")]
    lines += ["%-8s %-12s %-24s %-16s %s" % (r["pipeline"], r["stage"], r["runs"],
                                             r["parallel"], r["job_type"]) for r in rows]
    lines.append("kinds: %s. No stage runs through gt-farm or any non-Claude service."
                 % ", ".join(KINDS))
    emit(a, {"stages": rows, "kinds": list(KINDS)}, lines)
    return OK


# -- CLI ----------------------------------------------------------------------------------------
def _common(defaults):
    """The flags every command takes. The subcommands' copy has NO defaults (SUPPRESS): with
    one, argparse let the subcommand's `--dry-run` default (False) overwrite a `--dry-run`
    given BEFORE the subcommand, so `--dry-run draft` WROTE (2026-10-03, research.md: a 55-line
    entry queued and drained by a "preview"). Now the flag counts wherever it is placed."""
    kw = {} if defaults else {"default": argparse.SUPPRESS}
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--vault", help="the vault (default: GT_VAULT, then vault-config.json)", **kw)
    c.add_argument("--dry-run", action="store_true", help="show what would happen; "
                   "write nothing", **kw)
    c.add_argument("--json", action="store_true", **kw)
    return c


def build_parser():
    common = _common(defaults=False)
    ap = argparse.ArgumentParser(prog="gt_ingest_pipeline.py", parents=[_common(defaults=True)],
                                 description="The deterministic stages of the ingest and "
                                             "promote pipelines, and their spool packets.")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("stages", parents=[common], help="the pipelines, stage by stage")
    p.set_defaults(fn=cmd_stages)

    p = sub.add_parser("survey", parents=[common],
                       help="intake-scan + split a source tree (or session notes) into units")
    p.add_argument("path")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--project", help="the vault project slug the material goes to")
    p.add_argument("--run", help="the run id (default: <time>-<kind>-<repo>)")
    p.add_argument("--unit-depth", type=int, help="folder levels per unit (default 1, or the "
                   "repo's recorded split)")
    p.add_argument("--deeper", action="append", metavar="DIR",
                   help="split this top-level folder one level deeper (repeatable)")
    p.add_argument("--record", action="store_true",
                   help="save this split for the repo in ingest-units.json")
    p.add_argument("--why", help="one line on why the split was tuned (with --record)")
    p.add_argument("--repo-key", help="the name the split is recorded under (default: the "
                   "folder name)")
    p.set_defaults(fn=cmd_survey)

    p = sub.add_parser("packet", parents=[common],
                       help="wrap one agent's result as this stage's packet for one unit")
    p.add_argument("run")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit", required=True, help="the unit, batch, target file or candidate id")
    p.add_argument("--result-file", required=True, help="the agent's JSON (or a record)")
    p.add_argument("--session", help="the session id")
    p.add_argument("--replace", action="store_true", help="supersede an existing packet")
    p.set_defaults(fn=cmd_packet)

    p = sub.add_parser("fan-in", parents=[common], help="collect a stage's packets")
    p.add_argument("run")
    p.add_argument("--stage", required=True, choices=("extract", "classify"))
    p.set_defaults(fn=cmd_fan_in)

    p = sub.add_parser("reconcile", parents=[common],
                       help="dedupe, and stop on a contradiction with a fact in gt")
    p.add_argument("run")
    p.add_argument("--facts-out", help="write the related vault facts here, as the reconcile "
                   "agent's `facts` input")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("draft", parents=[common],
                       help="queue the kept findings through the write broker")
    p.add_argument("run")
    p.add_argument("--owner-continue", action="store_true",
                   help="the owner said to continue with the rest despite a stop")
    p.add_argument("--session", help="the session id the writes are queued under")
    p.add_argument("--no-drain", action="store_true", help="queue only; do not drain")
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser("promote-scan", parents=[common], help="start a promote run")
    p.add_argument("--candidates-file", required=True)
    p.add_argument("--run")
    p.set_defaults(fn=cmd_promote_scan)

    p = sub.add_parser("promote-plan", parents=[common],
                       help="fold verify/generalize/place into proposals for the owner")
    p.add_argument("run")
    p.set_defaults(fn=cmd_promote_plan)

    p = sub.add_parser("status", parents=[common], help="where a run stands")
    p.add_argument("run")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("workflow-args", parents=[common],
                       help="the args for the pipeline-stage workflow: one stage's schema and "
                            "the units still without a packet")
    p.add_argument("run")
    p.add_argument("--stage", required=True)
    p.add_argument("--prompt", action="append", metavar="UNIT=FILE",
                   help="a prompt gt_agent_spec.py rendered for UNIT (every stage but extract, "
                        "which renders its own after the intake scan)")
    p.set_defaults(fn=cmd_workflow_args)

    p = sub.add_parser("packets", parents=[common],
                       help="write every result a pipeline-stage workflow returned as its "
                            "unit's packet")
    p.add_argument("run")
    p.add_argument("--stage", required=True)
    p.add_argument("--results-file", required=True, help="the JSON the workflow returned")
    p.add_argument("--session", help="the session id")
    p.add_argument("--replace", action="store_true", help="supersede existing packets")
    p.set_defaults(fn=cmd_packets)

    p = sub.add_parser("cleanup", parents=[common],
                       help="remove a run's private scratch folders (an abandoned run's; a "
                            "finished run's go by themselves)")
    p.add_argument("run")
    p.set_defaults(fn=cmd_cleanup)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
