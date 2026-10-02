#!/usr/bin/env python3
"""gt_agent_spec.py -- job-type specs for specialist agents, and the prompt each one gets.

    gt_agent_spec.py list [--json] [--vault V] [--specs-dir D]
    gt_agent_spec.py validate <spec-file>
    gt_agent_spec.py resolve --skill S [--path P] [--json] [--vault V] [--specs-dir D]
    gt_agent_spec.py render <job-type> [--input K=V] [--input-file K=F] [--template] [--json]
    gt_agent_spec.py check-output <job-type> <record-file> [--vault V] [--specs-dir D]
    gt_agent_spec.py spool-path <job-type> [--session ID] [--vault V]

Every subcommand READS. Nothing here writes a file, spawns an agent or changes a setting.

WHY A SCRIPT CANNOT DO THE SPAWNING. A specialist agent is started by Claude, with the Agent
tool, because a skill told it to. So the work splits in two: this script owns everything that
is data and can be checked -- which job type applies, whether its spec is well-formed, the exact
prompt the agent receives, and whether the record the agent's result was saved in has the
fields the spec promised. The skill owns the one step only Claude can take: spawning the agent
with that prompt, then writing the record to the path `spool-path` names.

THE SETTINGS. Two independent switches (0.18.0; before that the skeptic needed both).
`agent_specialization` (default off) gates the ingest and validation hand-off: with it off,
`resolve` answers `action: inline` for gt-ingest and gt-validate and they run exactly as they
did before this existed. `skeptic_pass` (default off) alone gates the `skeptic` job for
gt-work: with it on, gt-work spawns the skeptic whatever `agent_specialization` says, and with
it off there is no skeptic whatever `agent_specialization` says.

WHERE SPECS LIVE. The release ships them in `templates/agent-specs/<job-type>.json`. NOT in
`packs/`: that directory is the input to gt_registry, it is hashed into MANIFEST.json as an
input to security controls, and the release gate re-validates everything there as a definition
pack -- a spec is none of those things. A vault may override or add a spec in
`<vault>/Projects/golden-thread/packs/agent-specs/`; the vault copy wins when it is valid, and an
invalid one is reported and ignored rather than silently replacing the shipped spec. That
directory sits under the vault's protected packs/ tree, so writing there asks first.

ZERO PRIOR CONTEXT. The `validate` and `skeptic` specs must declare the context strategy `none`;
`validate` refuses a spec that does not. A validator that has read the vault's research.md has
read the conclusion it is meant to check.

CLAUDE ONLY (owner, 2026-09-30). No ingest or promote stage may run through gt-farm or any
non-Claude service: letting a stage run externally is a security issue not yet mitigated.
`validate` refuses a spec whose `executor` is anything but `claude`, and a spec that names
gt-farm anywhere. A gt-ingest spec must declare `executor: claude` outright.

THE INTAKE SCAN. A gt-ingest spec must set `requires_intake_scan: true`, and `render` then runs
gt_intake_scan.py over the `path` input (just the `unit`, when one is given) and REFUSES, exit 1,
unless that scan is clean. No prompt is produced for material that has not passed the scan, so
an agent cannot be pointed at it by accident. `--template` renders no real input and scans
nothing.

`resolve` NEVER FAILS A SKILL. A missing spec, an invalid spec, or a path no heuristic matches
all answer `action: inline` with a `notice:` line for the session to show. Exit 2 is kept for
usage errors (an unknown subcommand, `--skill gt-ingest` with no `--path`).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
RELEASE = os.path.dirname(HERE)
RELEASE_SPECS = os.path.join(RELEASE, "templates", "agent-specs")
VAULT_SPECS = ("Projects", "golden-thread", "packs", "agent-specs")
SPOOL = ("Projects", "golden-thread", "spool", "agents")

MAX_SPEC_BYTES = 256 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 8
SPEC_VERSION = 1

TIERS = ("fast", "standard", "careful")
STRATEGIES = ("none", "target-only", "vault")
FIELD_TYPES = ("string", "list", "object", "enum", "number", "boolean")
ZERO_CONTEXT_JOBS = ("validate", "skeptic")

REQUIRED = ("job_type", "spec_version", "summary", "trigger_skill", "model_tier",
            "prompt_delta", "context_loading", "inputs", "output_schema")
OPTIONAL = ("requires_settings", "summary_fields", "executor", "requires_intake_scan")
EXECUTORS = ("claude",)
INTAKE_SKILLS = ("gt-ingest",)
FARM_RE = re.compile(r"\bgt[-_ ]?farm\b|\bfarm[-_]packet\b", re.I)
INTAKE_SCAN = os.path.join(HERE, "gt_intake_scan.py")
INTAKE_TIMEOUT = 900

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SKILL_RE = re.compile(r"^gt-[a-z0-9-]{1,40}$")
SESSION_RE = re.compile(r"[^A-Za-z0-9_-]")

# The skills that know a job type without looking at a path. gt-ingest is decided by the path.
SKILL_JOBS = {"gt-validate": "validate", "gt-work": "skeptic"}
# The setting that alone switches a job type on. Every job answers to agent_specialization
# except the skeptic, which answers to skeptic_pass only (0.18.0): turning the skeptic on must
# not also hand ingest and validation to specialists, which is a separate decision and cost.
MASTER_SWITCH = {"skeptic": "skeptic_pass"}

# -- ingest heuristics: file extensions and a few marker file names, nothing deeper -------------
CODE_EXT = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".rs", ".java", ".kt", ".kts",
            ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
            ".sh", ".bash", ".zsh", ".ps1", ".lua", ".pl", ".r", ".m", ".dart", ".ex", ".exs",
            ".clj", ".hs", ".sql", ".vue", ".svelte"}
DOC_EXT = {".md", ".markdown", ".txt", ".pdf", ".rst", ".adoc", ".org", ".docx", ".doc",
           ".rtf", ".odt", ".html", ".htm"}
TOOL_NAMES = {"plugin.json", "skill.md", "openapi.json", "openapi.yaml", "openapi.yml",
              "swagger.json", "swagger.yaml", "swagger.yml", ".mcp.json", "mcp.json"}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
             "dist", "build", "target", "vendor", ".tox", ".mypy_cache", ".pytest_cache",
             ".idea", ".vscode", ".obsidian"}
MAX_WALK = 20000

BASE_PROMPT = (
    "You are a Golden Thread specialist agent. A Golden Thread session spawned you for one job "
    "and will record your result in its vault; you have not seen that session's conversation.\n"
    "- Do not write, move or delete any file. Return your result; the session records it.\n"
    "- Every statement you make carries its evidence: a file path, a section, a command and "
    "what it printed.\n"
    "- Separate what you read from what you inferred. An inference is labelled as one.\n"
    "- When you could not determine something, say so in the output. Silence reads as a pass.")


# -- small helpers --------------------------------------------------------------------------------
def _clean(value, limit=120):
    """Strip control characters before interpolating untrusted text into a terminal line."""
    s = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C")
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _has_control(s):
    return any(unicodedata.category(ch)[0] == "C" and ch not in "\n\t" for ch in s)


def _depth(obj, level=0):
    if level > MAX_DEPTH:
        return level
    if isinstance(obj, dict):
        return max([_depth(v, level + 1) for v in obj.values()] or [level])
    if isinstance(obj, list):
        return max([_depth(v, level + 1) for v in obj] or [level])
    return level


def find_vault(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("GT_VAULT")
    if env:
        return env
    try:
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("vault_path")
        return v if isinstance(v, str) and v else None
    except (OSError, ValueError, AttributeError):
        return None


def setting(name):
    """The effective value of a gt setting, default applied. Falls back to reading the config
    directly when gt_settings cannot be imported (a copy of this script on its own)."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_settings                               # noqa: E402
        v = gt_settings.get(name)
        if v is not None:
            return v
    except Exception:                                    # noqa: BLE001 - fall back, never fail
        pass
    try:
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get(name)
        return v.strip().lower() if isinstance(v, str) and v.strip().lower() in ("on", "off") \
            else "off"
    except (OSError, ValueError, AttributeError):
        return "off"


# -- validation ---------------------------------------------------------------------------------
def _strings_ok(obj, where, problems):
    if isinstance(obj, str):
        if _has_control(obj):
            problems.append("%s: contains control characters" % where)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _strings_ok(k, where, problems)
            _strings_ok(v, "%s.%s" % (where, _clean(k, 40)), problems)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _strings_ok(v, "%s[%d]" % (where, i), problems)


def _farm_refs(obj, where, problems):
    if isinstance(obj, str):
        if FARM_RE.search(obj):
            problems.append("%s: names gt-farm -- Claude only: no ingest or promote stage may "
                            "run through gt-farm or any non-Claude service" % where)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _farm_refs(k, where, problems)
            _farm_refs(v, "%s.%s" % (where, _clean(k, 40)), problems)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _farm_refs(v, "%s[%d]" % (where, i), problems)


def validate_spec(data, stem=None):
    """-> list of problems, empty when the spec is valid. Each names the field at fault."""
    p = []
    if not isinstance(data, dict):
        return ["the spec is not a JSON object"]
    if _depth(data) > MAX_DEPTH:
        return ["the spec is nested more than %d levels deep" % MAX_DEPTH]
    for k in REQUIRED:
        if k not in data:
            p.append("missing required field '%s'" % k)
    for k in sorted(set(data) - set(REQUIRED) - set(OPTIONAL)):
        p.append("unknown field '%s' (allowed: %s)" % (_clean(k, 40),
                                                        ", ".join(REQUIRED + OPTIONAL)))
    _strings_ok(data, "spec", p)

    jt = data.get("job_type")
    if "job_type" in data:
        if not (isinstance(jt, str) and NAME_RE.match(jt)):
            p.append("job_type: must be lower-case letters, digits and dashes, got %r"
                     % (_clean(jt, 40),))
        elif stem is not None and jt != stem:
            p.append("job_type: '%s' does not match the file name '%s.json'" % (jt, _clean(stem)))
    if "spec_version" in data and data["spec_version"] != SPEC_VERSION:
        p.append("spec_version: must be %d, got %r" % (SPEC_VERSION, data["spec_version"]))
    if "summary" in data and not (isinstance(data["summary"], str) and data["summary"].strip()):
        p.append("summary: must be a non-empty string")
    if "trigger_skill" in data and not (isinstance(data["trigger_skill"], str)
                                        and SKILL_RE.match(data["trigger_skill"])):
        p.append("trigger_skill: must name a gt skill such as gt-ingest")
    if "model_tier" in data and data["model_tier"] not in TIERS:
        p.append("model_tier: must be one of %s, got %r"
                 % (", ".join(TIERS), _clean(data["model_tier"], 40)))

    pd = data.get("prompt_delta")
    if "prompt_delta" in data:
        if isinstance(pd, str):
            pd = [pd]
        if not (isinstance(pd, list) and pd
                and all(isinstance(x, str) and x.strip() for x in pd)):
            p.append("prompt_delta: must be a non-empty string or a list of non-empty strings")

    cl = data.get("context_loading")
    if "context_loading" in data:
        if not isinstance(cl, dict):
            p.append("context_loading: must be an object {strategy, load, note}")
        else:
            for k in sorted(set(cl) - {"strategy", "load", "note"}):
                p.append("context_loading: unknown field '%s'" % _clean(k, 40))
            strat = cl.get("strategy")
            load = cl.get("load", [])
            if strat not in STRATEGIES:
                p.append("context_loading.strategy: must be one of %s, got %r"
                         % (", ".join(STRATEGIES), _clean(strat, 40)))
            if not (isinstance(load, list) and all(isinstance(x, str) and x for x in load)):
                p.append("context_loading.load: must be a list of vault-relative paths")
                load = []
            for x in load:
                parts = x.replace("\\", "/").split("/")
                if x.startswith(("/", "~")) or ".." in parts or re.match(r"^[A-Za-z]:", x):
                    p.append("context_loading.load: '%s' must be vault-relative, with no '..'"
                             % _clean(x))
            if strat in ("none", "target-only") and load:
                p.append("context_loading.load: must be empty for strategy '%s'" % strat)
            if strat == "vault" and not load:
                p.append("context_loading.load: strategy 'vault' needs at least one path")
            if "note" in cl and not isinstance(cl["note"], str):
                p.append("context_loading.note: must be a string")
            if jt in ZERO_CONTEXT_JOBS and strat != "none":
                p.append("context_loading.strategy: the '%s' job must load no prior context "
                         "(strategy 'none'), got %r" % (jt, _clean(strat, 40)))

    inputs = data.get("inputs")
    if "inputs" in data:
        if not isinstance(inputs, dict):
            p.append("inputs: must be an object {name: {required, description}}")
        else:
            for name, spec in inputs.items():
                if not FIELD_RE.match(name):
                    p.append("inputs: '%s' is not a valid input name" % _clean(name, 40))
                if not (isinstance(spec, dict) and isinstance(spec.get("required"), bool)
                        and isinstance(spec.get("description", ""), str)
                        and not set(spec) - {"required", "description"}):
                    p.append("inputs.%s: must be {required: true|false, description: text}"
                             % _clean(name, 40))

    schema = data.get("output_schema")
    if "output_schema" in data:
        if not (isinstance(schema, dict) and schema):
            p.append("output_schema: must be a non-empty object {field: {type, description}}")
            schema = {}
        for name, f in schema.items():
            where = "output_schema.%s" % _clean(name, 40)
            if not FIELD_RE.match(name):
                p.append("%s: not a valid field name" % where)
            if not isinstance(f, dict):
                p.append("%s: must be an object {type, description}" % where)
                continue
            for k in sorted(set(f) - {"type", "description", "required", "values"}):
                p.append("%s: unknown key '%s'" % (where, _clean(k, 40)))
            if f.get("type") not in FIELD_TYPES:
                p.append("%s.type: must be one of %s" % (where, ", ".join(FIELD_TYPES)))
            if not (isinstance(f.get("description"), str) and f["description"].strip()):
                p.append("%s.description: must be a non-empty string" % where)
            if "required" in f and not isinstance(f["required"], bool):
                p.append("%s.required: must be true or false" % where)
            if f.get("type") == "enum":
                vals = f.get("values")
                if not (isinstance(vals, list) and vals and all(isinstance(v, str) for v in vals)):
                    p.append("%s.values: an enum needs a non-empty list of strings" % where)
            elif "values" in f:
                p.append("%s.values: only an enum field takes values" % where)

    _farm_refs(data, "spec", p)
    ex = data.get("executor")
    if "executor" in data and ex not in EXECUTORS:
        p.append("executor: Claude only -- a stage may not run through gt-farm or any "
                 "non-Claude service; got %r" % (_clean(ex, 40),))
    ris = data.get("requires_intake_scan")
    if "requires_intake_scan" in data and not isinstance(ris, bool):
        p.append("requires_intake_scan: must be true or false")
    if data.get("trigger_skill") in INTAKE_SKILLS:
        if "executor" not in data:
            p.append("executor: a %s spec must declare executor 'claude' (Claude only)"
                     % data["trigger_skill"])
        if ris is not True:
            p.append("requires_intake_scan: a %s spec must be true -- material is scanned "
                     "before any agent reads it" % data["trigger_skill"])
        if isinstance(inputs, dict) and "path" not in inputs:
            p.append("inputs: a %s spec needs a 'path' input for the intake scan to cover"
                     % data["trigger_skill"])

    rs = data.get("requires_settings", ["agent_specialization"])
    master = MASTER_SWITCH.get(data.get("job_type"), "agent_specialization")
    if not (isinstance(rs, list) and all(isinstance(x, str) and FIELD_RE.match(x) for x in rs)):
        p.append("requires_settings: must be a list of setting names")
    elif master not in rs:
        p.append("requires_settings: must include '%s', the master switch" % master)

    sf = data.get("summary_fields")
    if sf is not None:
        if not (isinstance(sf, list) and all(isinstance(x, str) for x in sf)):
            p.append("summary_fields: must be a list of output_schema field names")
        elif isinstance(schema, dict):
            for x in sf:
                if x not in schema:
                    p.append("summary_fields: '%s' is not in output_schema" % _clean(x, 40))
    return p


def read_spec(path):
    """-> (data or None, problems)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        size = os.path.getsize(path)
        if size > MAX_SPEC_BYTES:
            return None, ["%d bytes exceeds the %d-byte cap" % (size, MAX_SPEC_BYTES)]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except RecursionError:
        return None, ["JSON nested too deeply"]
    except (OSError, ValueError) as exc:
        return None, ["cannot read: %s" % _clean(exc, 200)]
    problems = validate_spec(data, stem)
    return (None if problems else data), problems


def spec_dirs(vault=None, specs_dir=None):
    """-> [(source, directory)] lowest precedence first."""
    out = [("release", specs_dir or RELEASE_SPECS)]
    v = find_vault(vault)
    if v:
        out.append(("vault", os.path.join(v, *VAULT_SPECS)))
    return out


def load_specs(vault=None, specs_dir=None):
    """-> ({job_type: {path, source, data}}, [(path, problem)]). A valid vault spec replaces
    the release one; an invalid one is a problem and replaces nothing."""
    specs, problems = {}, []
    for source, d in spec_dirs(vault, specs_dir):
        if not os.path.isdir(d):
            if source == "release":
                problems.append((d, "the shipped agent-specs directory is missing"))
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError as exc:
            problems.append((d, "cannot list: %s" % _clean(exc)))
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            if _has_control(name):
                problems.append((os.path.join(d, _clean(name)), "control characters in the "
                                 "file name; refusing to load"))
                continue
            data, errs = read_spec(path)
            if data is None:
                for e in errs:
                    problems.append((path, e))
                continue
            specs[data["job_type"]] = {"path": path, "source": source, "data": data}
    return specs, problems


# -- resolve ------------------------------------------------------------------------------------
def classify_path(path):
    """-> (job type or None, why). Extension heuristics only (deeper detection is out of scope)."""
    if not os.path.exists(path):
        return None, "%s does not exist" % _clean(path, 200)
    files = []
    if os.path.isfile(path):
        files = [path]
    else:
        for root, dirs, names in os.walk(path):
            dirs[:] = sorted(d for d in dirs
                             if d not in SKIP_DIRS and (not d.startswith(".")
                                                        or d == ".claude-plugin"))
            for n in names:
                files.append(os.path.join(root, n))
                if len(files) >= MAX_WALK:
                    break
            if len(files) >= MAX_WALK:
                break
    code, docs, tool = {}, {}, []
    for f in files:
        base = os.path.basename(f).lower()
        ext = os.path.splitext(base)[1]
        if base in TOOL_NAMES:
            tool.append(os.path.relpath(f, path) if os.path.isdir(path) else base)
        if ext in CODE_EXT:
            code[ext] = code.get(ext, 0) + 1
        elif ext in DOC_EXT:
            docs[ext] = docs.get(ext, 0) + 1

    def counts(d):
        return ", ".join("%s %d" % (k, v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:6])

    if tool:
        return "ingest-tool", "tool markers: %s" % ", ".join(_clean(t, 60) for t in tool[:4])
    if code:
        return "ingest-code", "%d source file(s): %s" % (sum(code.values()), counts(code))
    if docs:
        return "ingest-docs", "%d document(s), no source files: %s" % (sum(docs.values()),
                                                                     counts(docs))
    return None, "no source, document or tool files found under %s" % _clean(path, 200)


def resolve(skill, path=None, vault=None, specs_dir=None):
    """-> dict {skill, job, why, spec, source, tier, action, notice}. Never raises for a
    missing or bad spec: the answer is then `inline`, with a notice."""
    out = {"skill": skill, "job": None, "why": "", "spec": None, "source": None, "tier": None,
           "agent_specialization": setting("agent_specialization"),
           "skeptic_pass": setting("skeptic_pass"), "action": "inline", "notice": None}
    if skill == "gt-ingest":
        out["job"], out["why"] = classify_path(path)
    elif skill in SKILL_JOBS:
        out["job"], out["why"] = SKILL_JOBS[skill], "the job type for %s" % skill
    else:
        specs, _ = load_specs(vault, specs_dir)
        hits = sorted(j for j, s in specs.items() if s["data"]["trigger_skill"] == skill)
        if len(hits) == 1:
            out["job"], out["why"] = hits[0], "the only spec whose trigger_skill is %s" % skill
        else:
            out["why"] = ("no job type is defined for %s" % skill if not hits else
                          "several specs name %s (%s); pass the job type to render"
                          % (skill, ", ".join(hits)))

    master = MASTER_SWITCH.get(out["job"], "agent_specialization")
    if setting(master) != "on":
        if master == "skeptic_pass":
            out["why"] = "skeptic_pass is off, so gt-work writes back with no skeptic pass"
        else:
            out["why"] = out["why"] or "agent_specialization is off"
        return out
    if out["job"] is None:
        out["notice"] = "no specialist job type for this %s run (%s); running inline" % (
            skill, out["why"])
        return out

    specs, problems = load_specs(vault, specs_dir)
    spec = specs.get(out["job"])
    if spec is None:
        bad = [e for pth, e in problems
               if os.path.splitext(os.path.basename(pth))[0] == out["job"]]
        out["notice"] = ("no valid spec for job type '%s'%s; running inline"
                         % (out["job"], (" (%s)" % bad[0]) if bad else
                            " (looked in %s)" % " and ".join(d for _, d in
                                                             spec_dirs(vault, specs_dir))))
        return out
    for s in spec["data"].get("requires_settings", ["agent_specialization"]):
        # A skeptic spec written before 0.18.0 (a vault override) still lists
        # agent_specialization; the skeptic no longer answers to it, so it is not consulted.
        if master == "skeptic_pass" and s == "agent_specialization":
            continue
        if setting(s) != "on":
            out["notice"] = "the %s spec needs the %s setting on; running inline" % (
                out["job"], s)
            return out
    out.update(spec=spec["path"], source=spec["source"], tier=spec["data"]["model_tier"],
               action="spawn")
    return out


# -- render -------------------------------------------------------------------------------------
def _field_line(name, f):
    kind = f["type"]
    if kind == "enum":
        kind = "one of: %s" % " | ".join(f["values"])
    opt = "" if f.get("required", True) else ", optional"
    return "- `%s` (%s%s): %s" % (name, kind, opt, f["description"])


def context_section(spec, vault, inputs):
    cl = spec["context_loading"]
    strat = cl["strategy"]
    note = cl.get("note")
    loads = []
    if strat == "none":
        lines = ["Load NOTHING from the knowledge vault or from any earlier session: no "
                 "research.md, decisions.md, design.md, memory files, handoffs, logs or "
                 "transcripts. The inputs below are everything you are given."]
    elif strat == "target-only":
        lines = ["Read only the target named in the inputs. Do not open the knowledge vault."]
    else:
        v = find_vault(vault) or "<vault>"
        for rel in cl["load"]:
            try:
                rel = rel.format(**inputs)
            except (KeyError, IndexError, ValueError):
                pass
            loads.append(os.path.join(v, rel))
        lines = ["Load these vault files before you start, and nothing else from the vault:"]
        lines += ["- %s" % x for x in loads]
    if note:
        lines.append(note)
    return lines, loads


class IntakeRefused(Exception):
    """The intake scan did not come back clean, so no prompt is rendered."""


def intake_check(path, unit=None, vault=None):
    """Run gt_intake_scan.py -> summary dict. Raises IntakeRefused unless the scan is clean.
    The scanner never prints matched text, so nothing hostile passes through here."""
    cmd = [sys.executable, INTAKE_SCAN, path, "--json"]
    if unit:
        cmd += ["--unit", unit]
        if unit != ".":
            cmd += ["--unit-depth", str(len(unit.strip("/").split("/")))]
    v = find_vault(vault)
    if v:
        cmd += ["--vault", v]
    where = _clean(path, 200) + ((" (unit %s)" % _clean(unit, 80)) if unit else "")
    if not os.path.isfile(INTAKE_SCAN):
        raise IntakeRefused("the intake scanner is missing (%s); a scan that cannot run is "
                            "not a pass" % _clean(INTAKE_SCAN, 200))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=INTAKE_TIMEOUT)
        report = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise IntakeRefused("the intake scan of %s could not run (%s)"
                            % (where, exc.__class__.__name__))
    if proc.returncode != 0:
        what = {1: "findings", 3: "incomplete -- part of it could not be scanned",
                2: "usage error"}.get(proc.returncode, "exit %d" % proc.returncode)
        raise IntakeRefused("the intake scan of %s is not clean (%s). Run `gt_intake_scan.py "
                            "%s` for the kinds and locations; no prompt is rendered for "
                            "material that has not passed the scan"
                            % (where, what, _clean(path, 200)))
    return {"exit": 0, "path": path, "unit": unit, "files": report.get("files"),
            "skipped_dirs": report.get("skipped_dirs", []),
            "verdict": _clean(report.get("verdict", ""), 400)}


def intake_section(scan, template=False):
    if template or scan is None:
        return ["gt_intake_scan.py scanned this material before you were spawned: <the "
                "scan's verdict line>."]
    lines = ["gt_intake_scan.py scanned this material before you were spawned and found no "
             "credential, unsafe code or prompt injection:", scan["verdict"]]
    if scan["skipped_dirs"]:
        lines.append("These directories were NOT scanned; do not open anything under them: "
                     + ", ".join(_clean(d, 60) for d in scan["skipped_dirs"]))
    lines.append("A clean scan lowers the risk; it does not make the content trustworthy.")
    return lines


def render(spec, inputs, vault=None, template=False):
    """-> (prompt text, dict). Raises ValueError on a missing or unknown input, and
    IntakeRefused when the spec requires the intake scan and the material did not pass it."""
    declared = spec["inputs"]
    unknown = sorted(set(inputs) - set(declared))
    if unknown:
        raise ValueError("unknown input(s) %s; the %s spec takes: %s"
                         % (", ".join(unknown), spec["job_type"],
                            ", ".join(sorted(declared)) or "nothing"))
    missing = [n for n, d in declared.items() if d["required"] and n not in inputs]
    if missing and not template:
        raise ValueError("missing required input(s): %s (pass --input NAME=VALUE, or "
                         "--template for placeholders)" % ", ".join(missing))
    scan = None
    if spec.get("requires_intake_scan") and not template:
        scan = intake_check(inputs["path"], inputs.get("unit") or None, vault)
    delta = spec["prompt_delta"]
    delta = [delta] if isinstance(delta, str) else delta
    ctx_lines, loads = context_section(spec, vault, inputs)
    out = [BASE_PROMPT, "", "## Your job: %s" % spec["job_type"], ""]
    out += delta
    out += ["", "## Context you may load", ""] + ctx_lines
    if spec.get("requires_intake_scan"):
        out += ["", "## Intake scan", ""] + intake_section(scan, template)
    out += ["", "## Inputs", ""]
    for name, d in declared.items():
        if name in inputs:
            out += ["### %s" % name, inputs[name].rstrip("\n"), ""]
        elif d["required"]:
            out += ["### %s" % name, "<%s: %s>" % (name, d.get("description", "")), ""]
    out += ["## Output", "",
            "Return ONE JSON object and nothing before or after it, with these fields:"]
    out += [_field_line(n, f) for n, f in spec["output_schema"].items()]
    text = "\n".join(out).rstrip() + "\n"
    info = {"job_type": spec["job_type"], "model_tier": spec["model_tier"],
            "context": {"strategy": spec["context_loading"]["strategy"], "load": loads},
            "inputs": sorted(inputs), "output_schema": spec["output_schema"],
            "summary_fields": spec.get("summary_fields", list(spec["output_schema"])[:3]),
            "executor": spec.get("executor", "claude"), "intake_scan": scan,
            "record_shape": {"job_type": spec["job_type"], "session_id": "<session id>",
                             "created": "<ISO-8601 time>", "result": "<the agent's JSON>"},
            "prompt": text}
    return text, info


# -- output records -----------------------------------------------------------------------------
def _type_ok(kind, value):
    return {"string": lambda v: isinstance(v, str),
            "list": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
            "enum": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool)}[kind](value)


def check_record(spec, record):
    """-> problems with a spool record {job_type, session_id, created, result}."""
    p = []
    if not isinstance(record, dict):
        return ["the record is not a JSON object"]
    if record.get("job_type") != spec["job_type"]:
        p.append("job_type: expected '%s', got %r" % (spec["job_type"],
                                                      _clean(record.get("job_type"), 40)))
    for k in ("session_id", "created"):
        if not (isinstance(record.get(k), str) and record[k].strip()):
            p.append("%s: missing or empty" % k)
    result = record.get("result")
    if not isinstance(result, dict):
        return p + ["result: missing, or not a JSON object"]
    for name, f in spec["output_schema"].items():
        if name not in result:
            if f.get("required", True):
                p.append("result.%s: missing required field" % name)
            continue
        if not _type_ok(f["type"], result[name]):
            p.append("result.%s: expected %s" % (name, f["type"]))
        elif f["type"] == "enum" and result[name] not in f["values"]:
            p.append("result.%s: %r is not one of %s" % (name, _clean(result[name], 40),
                                                         ", ".join(f["values"])))
    return p


def summarise(spec, result, width=100):
    lines = []
    for name in spec.get("summary_fields", list(spec["output_schema"])[:3]):
        if name not in result:
            continue
        v = result[name]
        if isinstance(v, list):
            lines.append("%s: %d item(s)" % (name, len(v)))
            for item in v[:5]:
                s = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                lines.append("  - %s" % _clean(s, width))
            if len(v) > 5:
                lines.append("  ... %d more in the record" % (len(v) - 5))
        else:
            lines.append("%s: %s" % (name, _clean(v if isinstance(v, str) else json.dumps(v),
                                                  width * 3)))
    return lines


def spool_path(vault, job, session, now=None):
    now = now or datetime.datetime.now()
    sid = SESSION_RE.sub("", session or "")[:64] or "session"
    return os.path.join(vault, *SPOOL, job, "%s-%s.json" % (now.strftime("%Y%m%dT%H%M%S"), sid))


# -- commands -----------------------------------------------------------------------------------
def _spec_or_exit(job, vault, specs_dir):
    specs, problems = load_specs(vault, specs_dir)
    if job not in specs:
        bad = [e for pth, e in problems if os.path.splitext(os.path.basename(pth))[0] == job]
        print("gt_agent_spec: no valid spec for job type '%s'%s"
              % (_clean(job, 40), (": " + bad[0]) if bad else ""), file=sys.stderr)
        print("  installed: %s" % (", ".join(sorted(specs)) or "none"), file=sys.stderr)
        sys.exit(1)
    return specs[job]


def cmd_list(a):
    specs, problems = load_specs(a.vault, a.specs_dir)
    state = {"agent_specialization": setting("agent_specialization"),
             "skeptic_pass": setting("skeptic_pass")}
    if a.json:
        print(json.dumps({"settings": state,
                          "specs": [{"job_type": j, "trigger_skill": s["data"]["trigger_skill"],
                                     "model_tier": s["data"]["model_tier"],
                                     "context": s["data"]["context_loading"]["strategy"],
                                     "source": s["source"], "path": s["path"],
                                     "summary": s["data"]["summary"]}
                                    for j, s in sorted(specs.items())],
                          "problems": [{"path": pth, "problem": e} for pth, e in problems]},
                         indent=2))
        return 1 if problems else 0
    print("%-13s %-12s %-9s %-12s %-8s %s" % ("JOB TYPE", "SKILL", "TIER", "CONTEXT",
                                              "SOURCE", "SUMMARY"))
    for j, s in sorted(specs.items()):
        d = s["data"]
        print("%-13s %-12s %-9s %-12s %-8s %s" % (j, d["trigger_skill"], d["model_tier"],
                                                  d["context_loading"]["strategy"],
                                                  s["source"], _clean(d["summary"], 90)))
    if not specs:
        print("(no specs installed)")
    for pth, e in problems:
        print("PROBLEM %s: %s" % (_clean(pth, 200), e))
    print("\nagent_specialization: %s   skeptic_pass: %s" % (state["agent_specialization"],
                                                           state["skeptic_pass"]))
    if state["agent_specialization"] != "on":
        print("Ingest and validation run inline. Turn specialists on with: "
              "gt_settings.py set agent_specialization on")
    if state["skeptic_pass"] != "on":
        print("No skeptic pass at gt-work. Turn it on (on its own) with: "
              "gt_settings.py set skeptic_pass on")
    return 1 if problems else 0


def cmd_validate(a):
    _, problems = read_spec(a.spec_file)
    if problems:
        for e in problems:
            print("INVALID %s: %s" % (_clean(a.spec_file, 200), e))
        return 1
    print("ok %s" % _clean(a.spec_file, 200))
    return 0


def cmd_resolve(a):
    if a.skill == "gt-ingest" and not a.path:
        print("gt_agent_spec: resolve --skill gt-ingest needs --path", file=sys.stderr)
        return 2
    r = resolve(a.skill, a.path, a.vault, a.specs_dir)
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print("skill:   %s" % _clean(r["skill"], 60))
    print("job:     %s" % (r["job"] or "none"))
    print("why:     %s" % _clean(r["why"], 300))
    print("setting: agent_specialization=%s skeptic_pass=%s" % (r["agent_specialization"],
                                                                r["skeptic_pass"]))
    if r["spec"]:
        print("spec:    %s (%s)" % (_clean(r["spec"], 300), r["source"]))
        print("tier:    %s (advisory: the session's model configuration decides)" % r["tier"])
    print("action:  %s" % r["action"])
    if r["notice"]:
        print("notice:  %s" % r["notice"])
    return 0


def _parse_inputs(a):
    inputs = {}
    for raw in a.input or []:
        k, sep, v = raw.partition("=")
        if not sep or not k:
            raise ValueError("--input takes NAME=VALUE, got %r" % _clean(raw, 60))
        inputs[k] = v
    for raw in a.input_file or []:
        k, sep, f = raw.partition("=")
        if not sep or not k:
            raise ValueError("--input-file takes NAME=PATH, got %r" % _clean(raw, 60))
        try:
            with open(f, encoding="utf-8") as fh:
                inputs[k] = fh.read()
        except OSError as exc:
            raise ValueError("--input-file %s: %s" % (_clean(k, 40), _clean(exc, 200)))
    return inputs


def cmd_render(a):
    spec = _spec_or_exit(a.job_type, a.vault, a.specs_dir)
    try:
        text, info = render(spec["data"], _parse_inputs(a), a.vault, a.template)
    except ValueError as exc:
        print("gt_agent_spec: %s" % exc, file=sys.stderr)
        return 2
    except IntakeRefused as exc:
        print("gt_agent_spec: render refused: %s" % exc, file=sys.stderr)
        return 1
    if a.json:
        info["spec"], info["source"] = spec["path"], spec["source"]
        print(json.dumps(info, indent=2))
    else:
        sys.stdout.write(text)
    return 0


def cmd_check_output(a):
    spec = _spec_or_exit(a.job_type, a.vault, a.specs_dir)["data"]
    try:
        if os.path.getsize(a.record_file) > MAX_RECORD_BYTES:
            print("INVALID %s: larger than %d bytes" % (_clean(a.record_file, 200),
                                                         MAX_RECORD_BYTES))
            return 1
        with open(a.record_file, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError, RecursionError) as exc:
        print("INVALID %s: cannot read: %s" % (_clean(a.record_file, 200), _clean(exc, 200)))
        return 1
    problems = check_record(spec, record)
    if problems:
        for e in problems:
            print("INVALID %s: %s" % (_clean(a.record_file, 200), e))
        return 1
    print("ok %s -- %s record for session %s" % (_clean(a.record_file, 200), spec["job_type"],
                                                 _clean(record["session_id"], 64)))
    for line in summarise(spec, record["result"]):
        print("  " + line)
    return 0


def cmd_spool_path(a):
    if not NAME_RE.match(a.job_type):
        print("gt_agent_spec: %r is not a job type name" % _clean(a.job_type, 40),
              file=sys.stderr)
        return 2
    v = find_vault(a.vault)
    if not v:
        print("gt_agent_spec: no vault (pass --vault, set GT_VAULT, or run /gt:gt-init)",
              file=sys.stderr)
        return 2
    print(spool_path(v, a.job_type, a.session))
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", help="the vault whose Projects/golden-thread/packs/"
                        "agent-specs/ may override the shipped specs (default: GT_VAULT, "
                        "then vault-config.json)")
    common.add_argument("--specs-dir", help="read the shipped specs from this directory "
                        "instead of the release's templates/agent-specs/")
    ap = argparse.ArgumentParser(
        prog="gt_agent_spec.py",
        description="Job-type specs for specialist agents. Every subcommand only reads.")
    sub = ap.add_subparsers(dest="cmd", metavar="{list,validate,resolve,render,check-output,"
                                               "spool-path}")
    sub.required = True

    p = sub.add_parser("list", parents=[common], help="the installed specs and their skills")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("validate", help="check one spec file; exit 1 with the reasons")
    p.add_argument("spec_file")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("resolve", parents=[common],
                       help="which job type a skill run gets, and spawn or inline")
    p.add_argument("--skill", required=True, help="gt-ingest, gt-validate or gt-work")
    p.add_argument("--path", help="the ingest target (gt-ingest)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("render", parents=[common], help="the full prompt for a job type")
    p.add_argument("job_type")
    p.add_argument("--input", action="append", metavar="NAME=VALUE",
                   help="an input the spec declares (repeatable)")
    p.add_argument("--input-file", action="append", metavar="NAME=PATH",
                   help="an input read from a file (repeatable)")
    p.add_argument("--template", action="store_true",
                   help="render placeholders for missing required inputs instead of failing")
    p.add_argument("--json", action="store_true",
                   help="the prompt plus tier, context list and output schema, as JSON")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("check-output", parents=[common],
                       help="check a spool record against the spec's output schema")
    p.add_argument("job_type")
    p.add_argument("record_file")
    p.set_defaults(fn=cmd_check_output)

    p = sub.add_parser("spool-path", help="where a specialist's record goes (creates nothing)")
    p.add_argument("job_type")
    p.add_argument("--session", help="the session id to name the record after")
    p.add_argument("--vault", help="the vault (default: GT_VAULT, then vault-config.json)")
    p.set_defaults(fn=cmd_spool_path)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
