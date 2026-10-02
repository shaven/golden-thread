#!/usr/bin/env python3
"""gt_agent_spec.py -- stage x kind specs for specialist agents, and the prompt each one gets.

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
with that prompt, then writing the record (gt_ingest_pipeline.py `packet` writes a pipeline
stage's record; `spool-path` names where a one-off record goes).

STAGE x KIND (0.18.0). Ingest and promote are pipelines of small, stateless stages (owner,
2026-09-30), so a job type is a STAGE, and for an ingest stage a KIND parameter:

    ingest   intake-scan* -> survey* -> extract -> classify -> reconcile -> draft
    promote  scan* -> verify -> generalize -> place -> owner approves*
             (* deterministic or human: gt_intake_scan.py / gt_ingest_pipeline.py, no agent)
    kinds    code | docs | tool | session (gt-work) | wiki

One spec per stage in `templates/agent-specs/stages/<stage>.json` (the full spec format), plus
one small per-kind delta in `kinds/<kind>.json` that may ADD prompt lines, inputs and output
fields and replace the summary, trigger skill, tier, summary fields or required settings. A
kind delta can never change the executor, the intake-scan requirement or the context strategy:
the safety properties belong to the stage. The job type of a composition is `<stage>-<kind>`
(extract-code, reconcile-session); a promote stage has no kind (verify, generalize, place).

THE 0.17.10 NAMES. `aliases.json` keeps ingest-code, ingest-docs, ingest-tool (extract-<kind>),
validate (verify) and skeptic (reconcile-session; its `additions` input is `findings` now)
working for one release: they list, render and check the composition, a record may carry either
name, and `render` notes the new name on stderr. A release file still named after an alias is
ignored (the composition replaces it); a VAULT override under an old name still wins.

THE SETTINGS. `agent_specialization` (default off) gates all of it. With it off, `resolve`
answers `action: inline` for every skill and the skills run exactly as they did before this
existed. `skeptic_pass` (default off) additionally gates reconcile-session, gt-work's skeptic.

WHERE SPECS LIVE. The release ships them in `templates/agent-specs/`. NOT in `packs/`: that
directory is the input to gt_registry, it is hashed into MANIFEST.json as an input to security
controls, and the release gate re-validates everything there as a definition pack -- a spec is
none of those things. A vault may override a stage or a kind (or add a whole flat spec) in
`<vault>/Projects/golden-thread/packs/agent-specs/{stages,kinds,}/`; the vault copy wins when it
is valid, and an invalid one is reported and ignored rather than silently replacing the shipped
spec. That directory sits under the vault's protected packs/ tree, so writing there asks first.

ZERO PRIOR CONTEXT. The verify and reconcile stages (and the old validate/skeptic names) must
declare the context strategy `none`; `validate` refuses a spec that does not. A validator that
has read the vault's research.md has read the conclusion it is meant to check. reconcile is
given the related vault facts as an INPUT, selected by gt_ingest_pipeline.py, not by browsing.

CLAUDE ONLY (owner, 2026-09-30). No ingest or promote stage may run through gt-farm or any
non-Claude service: letting a stage run externally is a security issue not yet mitigated.
`validate` refuses a spec whose `executor` is anything but `claude`, and a spec that names
gt-farm anywhere. A gt-ingest spec, and every extract stage, must declare `executor: claude`.

THE INTAKE SCAN. Only the extract stage reads material, so every extract composition (and any
flat gt-ingest spec) must set `requires_intake_scan: true` and take a `path`; `render` then runs
gt_intake_scan.py over that path (just the `unit`, when one is given) and REFUSES, exit 1,
unless the scan is clean. The later stages read packets, never material, and may not take a
`path` input at all. `--template` renders no real input and scans nothing.

`resolve` NEVER FAILS A SKILL. A missing spec, an invalid spec, or a path no heuristic matches
all answer `action: inline` with a `notice:` line for the session to show. Exit 2 is kept for
usage errors (an unknown subcommand, `--skill gt-ingest` with no `--path`).
"""
import argparse
import copy
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

# The pipelines (0.18.0). Agent stages have a spec; the others are deterministic or human.
PIPELINES = {
    "ingest": ("intake-scan", "survey", "extract", "classify", "reconcile", "draft"),
    "promote": ("scan", "verify", "generalize", "place", "approve"),
}
AGENT_STAGES = ("extract", "classify", "reconcile", "draft", "verify", "generalize", "place")
KIND_STAGES = ("extract", "classify", "reconcile", "draft")     # take a kind delta
PACKET_STAGES = tuple(s for s in AGENT_STAGES if s != "extract")  # read packets, not material
ZERO_CONTEXT_STAGES = ("verify", "reconcile")
KIND_FIELDS = ("kind", "spec_version", "summary", "trigger_skill", "prompt_delta", "stages")
DELTA_FIELDS = ("prompt_delta", "inputs", "output_schema", "summary_fields", "summary",
                "trigger_skill", "requires_settings", "model_tier")

REQUIRED = ("job_type", "spec_version", "summary", "trigger_skill", "model_tier",
            "prompt_delta", "context_loading", "inputs", "output_schema")
OPTIONAL = ("requires_settings", "summary_fields", "executor", "requires_intake_scan",
            "pipeline", "stage", "kind")
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
SKILL_JOBS = {"gt-validate": "verify", "gt-work": "reconcile-session", "gt-promote": "verify"}
SKILL_PIPELINE = {"gt-ingest": "ingest", "gt-work": "ingest", "gt-promote": "promote"}

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
            if (jt in ZERO_CONTEXT_JOBS or data.get("stage") in ZERO_CONTEXT_STAGES) \
                    and strat != "none":
                p.append("context_loading.strategy: the '%s' job must load no prior context "
                         "(strategy 'none'), got %r" % (_clean(jt, 40), _clean(strat, 40)))

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
    stage = data.get("stage")
    if "stage" in data and stage not in AGENT_STAGES:
        p.append("stage: must be one of %s, got %r" % (", ".join(AGENT_STAGES),
                                                         _clean(stage, 40)))
    elif stage and isinstance(jt, str) and not (jt == stage or jt.startswith(stage + "-")):
        p.append("job_type: '%s' must be the stage name '%s' or '%s-<kind>'"
                 % (_clean(jt, 40), stage, stage))
    if "pipeline" in data and data["pipeline"] not in PIPELINES:
        p.append("pipeline: must be one of %s" % ", ".join(sorted(PIPELINES)))
    if "kind" in data and not (isinstance(data["kind"], str) and NAME_RE.match(data["kind"])):
        p.append("kind: must be lower-case letters, digits and dashes")
    reads_material = stage == "extract" or (stage is None and
                                            data.get("trigger_skill") in INTAKE_SKILLS)
    who = ("an extract stage spec" if stage == "extract" else
           "a %s spec" % _clean(data.get("trigger_skill"), 40))
    if reads_material:
        if "executor" not in data:
            p.append("executor: %s must declare executor 'claude' (Claude only)" % who)
        if ris is not True:
            p.append("requires_intake_scan: %s must be true -- material is scanned "
                     "before any agent reads it" % who)
        if isinstance(inputs, dict) and "path" not in inputs:
            p.append("inputs: %s needs a 'path' input for the intake scan to cover" % who)
    elif stage in PACKET_STAGES and isinstance(inputs, dict) and "path" in inputs:
        p.append("inputs: the %s stage reads packets, not material, so it may not take a "
                 "'path' input -- only extract reads material, after the intake scan" % stage)

    rs = data.get("requires_settings", ["agent_specialization"])
    if not (isinstance(rs, list) and all(isinstance(x, str) and FIELD_RE.match(x) for x in rs)):
        p.append("requires_settings: must be a list of setting names")
    elif "agent_specialization" not in rs:
        p.append("requires_settings: must include 'agent_specialization', the master switch")

    sf = data.get("summary_fields")
    if sf is not None:
        if not (isinstance(sf, list) and all(isinstance(x, str) for x in sf)):
            p.append("summary_fields: must be a list of output_schema field names")
        elif isinstance(schema, dict):
            for x in sf:
                if x not in schema:
                    p.append("summary_fields: '%s' is not in output_schema" % _clean(x, 40))
    return p


def _read_json(path):
    """-> (data or None, problems)."""
    try:
        size = os.path.getsize(path)
        if size > MAX_SPEC_BYTES:
            return None, ["%d bytes exceeds the %d-byte cap" % (size, MAX_SPEC_BYTES)]
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), []
    except RecursionError:
        return None, ["JSON nested too deeply"]
    except (OSError, ValueError) as exc:
        return None, ["cannot read: %s" % _clean(exc, 200)]


def read_spec(path):
    """-> (data or None, problems)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    data, problems = _read_json(path)
    if data is None:
        return None, problems
    problems = validate_spec(data, stem)
    return (None if problems else data), problems


# -- stage x kind -------------------------------------------------------------------------------
def validate_kind(data, stem=None):
    """-> problems with a per-kind delta file. Composition is checked separately."""
    p = []
    if not isinstance(data, dict):
        return ["the kind delta is not a JSON object"]
    if _depth(data) > MAX_DEPTH:
        return ["the kind delta is nested more than %d levels deep" % MAX_DEPTH]
    for k in ("kind", "spec_version", "summary", "stages"):
        if k not in data:
            p.append("missing required field '%s'" % k)
    for k in sorted(set(data) - set(KIND_FIELDS)):
        p.append("unknown field '%s' (allowed: %s) -- a kind may not change the executor, "
                 "the intake scan or the context strategy" % (_clean(k, 40),
                                                               ", ".join(KIND_FIELDS)))
    _strings_ok(data, "kind", p)
    _farm_refs(data, "kind", p)
    kind = data.get("kind")
    if "kind" in data:
        if not (isinstance(kind, str) and NAME_RE.match(kind)):
            p.append("kind: must be lower-case letters, digits and dashes")
        elif stem is not None and kind != stem:
            p.append("kind: '%s' does not match the file name '%s.json'" % (kind, _clean(stem)))
    if "spec_version" in data and data["spec_version"] != SPEC_VERSION:
        p.append("spec_version: must be %d" % SPEC_VERSION)
    if "summary" in data and not (isinstance(data["summary"], str) and data["summary"].strip()):
        p.append("summary: must be a non-empty string")
    pd = data.get("prompt_delta", [])
    if not (isinstance(pd, list) and all(isinstance(x, str) and x.strip() for x in pd)):
        p.append("prompt_delta: must be a list of non-empty strings")
    stages = data.get("stages")
    if "stages" in data:
        if not isinstance(stages, dict):
            p.append("stages: must be an object {stage: delta}")
            stages = {}
        for st, d in stages.items():
            where = "stages.%s" % _clean(st, 40)
            if st not in KIND_STAGES:
                p.append("%s: a kind applies only to the stages %s" % (where,
                                                                        ", ".join(KIND_STAGES)))
            if not isinstance(d, dict):
                p.append("%s: must be an object" % where)
                continue
            for k in sorted(set(d) - set(DELTA_FIELDS)):
                p.append("%s: unknown key '%s' (allowed: %s) -- a kind may not change the "
                         "executor, the intake scan or the context strategy"
                         % (where, _clean(k, 40), ", ".join(DELTA_FIELDS)))
    return p


def compose(stage_spec, kind_delta):
    """The stage spec with the kind's delta applied -> a full spec named <stage>-<kind>."""
    st = stage_spec["stage"]
    kind = kind_delta["kind"]
    d = (kind_delta.get("stages") or {}).get(st, {})
    out = copy.deepcopy(stage_spec)
    out["job_type"] = "%s-%s" % (st, kind)
    out["kind"] = kind
    if kind_delta.get("trigger_skill"):
        out["trigger_skill"] = kind_delta["trigger_skill"]
    base = out["prompt_delta"]
    base = [base] if isinstance(base, str) else list(base)
    extra = d.get("prompt_delta", [])
    extra = [extra] if isinstance(extra, str) else list(extra)
    out["prompt_delta"] = base + list(kind_delta.get("prompt_delta", [])) + extra
    for k in ("inputs", "output_schema"):
        if isinstance(d.get(k), dict):
            merged = dict(out[k])
            merged.update(d[k])
            out[k] = merged
    for k in ("summary_fields", "trigger_skill", "requires_settings", "model_tier"):
        if k in d:
            out[k] = copy.deepcopy(d[k])
    out["summary"] = d.get("summary") or "%s Kind: %s -- %s" % (
        stage_spec["summary"].rstrip(), kind, kind_delta["summary"].rstrip())
    return out


def read_aliases(path):
    """-> ({alias: {to, inputs}}, problems)."""
    if not os.path.isfile(path):
        return {}, []
    data, problems = _read_json(path)
    if data is None:
        return {}, problems
    out, p = {}, []
    al = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(al, dict):
        return {}, ["aliases: must be an object {old name: {to, inputs}}"]
    _farm_refs(data, "aliases", p)
    for name, a in al.items():
        if not (NAME_RE.match(name) and isinstance(a, dict) and isinstance(a.get("to"), str)
                and NAME_RE.match(a["to"]) and isinstance(a.get("inputs", {}), dict)
                and not set(a) - {"to", "inputs"}):
            p.append("aliases.%s: must be {to: <job type>, inputs: {old: new}}" % _clean(name))
            continue
        out[name] = {"to": a["to"], "inputs": dict(a.get("inputs", {}))}
    return (out if not p else {}), p


def spec_dirs(vault=None, specs_dir=None):
    """-> [(source, directory)] lowest precedence first."""
    out = [("release", specs_dir or RELEASE_SPECS)]
    v = find_vault(vault)
    if v:
        out.append(("vault", os.path.join(v, *VAULT_SPECS)))
    return out


def _json_files(d, problems):
    if not os.path.isdir(d):
        return []
    try:
        names = sorted(os.listdir(d))
    except OSError as exc:
        problems.append((d, "cannot list: %s" % _clean(exc)))
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        if _has_control(name):
            problems.append((os.path.join(d, _clean(name)), "control characters in the file "
                             "name; refusing to load"))
            continue
        out.append(os.path.join(d, name))
    return out


def load_specs(vault=None, specs_dir=None):
    """-> ({job_type: {path, source, data[, alias_of, inputs_map]}}, [(path, problem)]).

    Stages and kinds load per file, release first; a valid vault file replaces the release one
    and an invalid one is a problem that replaces nothing. Then every ingest stage composes
    with every kind. Flat full specs (a vault's own job types) come next, and the 0.17.10
    aliases last, for names nothing else defines."""
    specs, problems = {}, []
    stages, kinds, flat, aliases = {}, {}, {}, {}
    dirs = spec_dirs(vault, specs_dir)
    for source, d in dirs:
        if not os.path.isdir(d):
            if source == "release":
                problems.append((d, "the shipped agent-specs directory is missing"))
            continue
        for path in _json_files(os.path.join(d, "stages"), problems):
            data, errs = read_spec(path)
            if data is not None and data.get("stage") != data["job_type"]:
                data, errs = None, ["stage: a stage file must declare stage '%s'"
                                    % data["job_type"]]
            if data is None:
                problems.extend((path, e) for e in errs)
            else:
                stages[data["job_type"]] = {"path": path, "source": source, "data": data}
        for path in _json_files(os.path.join(d, "kinds"), problems):
            stem = os.path.splitext(os.path.basename(path))[0]
            data, errs = _read_json(path)
            errs = errs or validate_kind(data, stem)
            if errs:
                problems.extend((path, e) for e in errs)
            else:
                kinds[data["kind"]] = {"path": path, "source": source, "data": data}
        if source == "release":
            al, errs = read_aliases(os.path.join(d, "aliases.json"))
            problems.extend((os.path.join(d, "aliases.json"), e) for e in errs)
            aliases.update(al)
        for path in _json_files(d, problems):
            if os.path.basename(path) == "aliases.json":
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            if source == "release" and stem in aliases:
                continue        # a 0.17.10 file the composition replaces; the alias serves it
            data, errs = read_spec(path)
            if data is None:
                problems.extend((path, e) for e in errs)
            else:
                flat[data["job_type"]] = {"path": path, "source": source, "data": data}
    for name, st in stages.items():
        specs[name] = dict(st, stage=name, kind=None)
    for name, st in sorted(stages.items()):
        if name not in KIND_STAGES:
            continue
        for kname, k in sorted(kinds.items()):
            data = compose(st["data"], k["data"])
            errs = validate_spec(data)
            if errs:
                problems.extend((k["path"], "%s: %s" % (data["job_type"], e)) for e in errs)
                continue
            src = "vault" if "vault" in (st["source"], k["source"]) else "release"
            specs[data["job_type"]] = {"path": "%s + %s" % (st["path"], k["path"]),
                                       "source": src, "data": data, "stage": name,
                                       "kind": kname}
    for name, f in flat.items():
        specs[name] = dict(f, stage=f["data"].get("stage"), kind=f["data"].get("kind"))
    for name, a in aliases.items():
        if name in specs:
            continue
        target = specs.get(a["to"])
        if target is None:
            problems.append(("aliases.json", "alias '%s' points to '%s', which is not a valid "
                             "job type" % (name, a["to"])))
            continue
        specs[name] = dict(target, alias_of=a["to"], inputs_map=a["inputs"], source="alias")
    return specs, problems


def _problems_for(job, problems):
    """The load problems that explain why `job` has no valid spec."""
    stage, _, kind = job.partition("-")
    out = []
    for pth, e in problems:
        stem = os.path.splitext(os.path.basename(pth))[0]
        if stem == job or e.startswith(job + ":") or (stem in (stage, kind) and kind) \
                or (stem == stage and not kind):
            out.append(e)
    return out


def canonical(entry, name):
    """The job type a spec entry really is (an alias's target, else its own name)."""
    return entry.get("alias_of") or name


# -- resolve ------------------------------------------------------------------------------------
def classify_path(path):
    """-> (kind or None, why): code | docs | tool. Extension heuristics only (deeper detection
    is out of scope). The job type is then extract-<kind>."""
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
        return "tool", "tool markers: %s" % ", ".join(_clean(t, 60) for t in tool[:4])
    if code:
        return "code", "%d source file(s): %s" % (sum(code.values()), counts(code))
    if docs:
        return "docs", "%d document(s), no source files: %s" % (sum(docs.values()),
                                                                     counts(docs))
    return None, "no source, document or tool files found under %s" % _clean(path, 200)


def resolve(skill, path=None, vault=None, specs_dir=None):
    """-> dict {skill, job, why, spec, source, tier, action, notice}. Never raises for a
    missing or bad spec: the answer is then `inline`, with a notice."""
    out = {"skill": skill, "job": None, "why": "", "spec": None, "source": None, "tier": None,
           "agent_specialization": setting("agent_specialization"), "action": "inline",
           "notice": None, "stage": None, "kind": None,
           "pipeline": list(PIPELINES[SKILL_PIPELINE[skill]]) if skill in SKILL_PIPELINE
           else None}
    if skill == "gt-ingest":
        kind, out["why"] = classify_path(path)
        if kind:
            out["job"], out["stage"], out["kind"] = "extract-%s" % kind, "extract", kind
    elif skill in SKILL_JOBS:
        out["job"], out["why"] = SKILL_JOBS[skill], "the job type for %s" % skill
        out["stage"], _, k = out["job"].partition("-")
        out["kind"] = k or None
    else:
        specs, _ = load_specs(vault, specs_dir)
        hits = sorted(j for j, s in specs.items()
                      if s["data"]["trigger_skill"] == skill and not s.get("alias_of"))
        if len(hits) == 1:
            out["job"], out["why"] = hits[0], "the only spec whose trigger_skill is %s" % skill
        else:
            out["why"] = ("no job type is defined for %s" % skill if not hits else
                          "several specs name %s (%s); pass the job type to render"
                          % (skill, ", ".join(hits)))

    skeptic_on = setting("skeptic_pass") == "on"
    if out["agent_specialization"] != "on":
        out["why"] = out["why"] or "agent_specialization is off"
        if skill == "gt-work" and skeptic_on:
            out["notice"] = ("skeptic_pass is on but agent_specialization is off, so no skeptic "
                             "pass runs; turn both on to get one")
        return out
    if out["job"] is None:
        out["notice"] = "no specialist job type for this %s run (%s); running inline" % (
            skill, out["why"])
        return out
    if out["job"] == "reconcile-session" and not skeptic_on:
        out["why"] = "skeptic_pass is off, so gt-work writes back with no skeptic pass"
        return out

    specs, problems = load_specs(vault, specs_dir)
    spec = specs.get(out["job"])
    if spec is None:
        bad = _problems_for(out["job"], problems)
        out["notice"] = ("no valid spec for job type '%s'%s; running inline"
                         % (out["job"], (" (%s)" % bad[0]) if bad else
                            " (looked in %s)" % " and ".join(d for _, d in
                                                             spec_dirs(vault, specs_dir))))
        return out
    for s in spec["data"].get("requires_settings", ["agent_specialization"]):
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


def intake_check(path, unit=None, vault=None, unit_depth=None):
    """Run gt_intake_scan.py -> summary dict. Raises IntakeRefused unless the scan is clean.
    The scanner never prints matched text, so nothing hostile passes through here. With no
    `unit_depth`, a unit's depth is its own number of path segments."""
    cmd = [sys.executable, INTAKE_SCAN, path, "--json"]
    if unit:
        cmd += ["--unit", unit]
        depth = len(unit.strip("/").split("/"))
        if unit_depth and str(unit_depth).isdigit() and int(unit_depth) > depth:
            depth = int(unit_depth)
        if unit != ".":
            cmd += ["--unit-depth", str(depth)]
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
        scan = intake_check(inputs["path"], inputs.get("unit") or None, vault,
                            inputs.get("unit_depth"))
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


def check_record(spec, record, names=()):
    """-> problems with a spool record {job_type, session_id, created, result}. `names` are
    other names the record may carry for this job (a 0.17.10 alias)."""
    p = []
    if not isinstance(record, dict):
        return ["the record is not a JSON object"]
    if record.get("job_type") != spec["job_type"] and record.get("job_type") not in names:
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
        bad = _problems_for(job, problems)
        print("gt_agent_spec: no valid spec for job type '%s'%s"
              % (_clean(job, 40), (": " + bad[0]) if bad else ""), file=sys.stderr)
        print("  installed: %s" % (", ".join(sorted(specs)) or "none"), file=sys.stderr)
        sys.exit(1)
    entry = specs[job]
    entry["names"] = tuple(n for n, e in specs.items()
                           if e.get("alias_of") == canonical(entry, job) and n != job)
    if entry.get("alias_of"):
        entry["names"] += (job,)
    return entry


def _row(name, e):
    d = e["data"]
    return {"job_type": name, "trigger_skill": d["trigger_skill"], "model_tier": d["model_tier"],
            "context": d["context_loading"]["strategy"], "source": e["source"],
            "path": e["path"], "summary": d["summary"], "stage": e.get("stage"),
            "kind": e.get("kind"), "pipeline": d.get("pipeline"),
            "alias_of": e.get("alias_of")}


def cmd_list(a):
    specs, problems = load_specs(a.vault, a.specs_dir)
    state = {"agent_specialization": setting("agent_specialization"),
             "skeptic_pass": setting("skeptic_pass")}
    if a.json:
        print(json.dumps({"settings": state, "pipelines": PIPELINES,
                          "specs": [_row(j, s) for j, s in sorted(specs.items())],
                          "problems": [{"path": pth, "problem": e} for pth, e in problems]},
                         indent=2))
        return 1 if problems else 0
    print("%-18s %-12s %-9s %-12s %-8s %s" % ("JOB TYPE", "SKILL", "TIER", "CONTEXT",
                                              "SOURCE", "SUMMARY"))
    for j, s in sorted(specs.items(), key=lambda kv: (bool(kv[1].get("alias_of")), kv[0])):
        d = s["data"]
        summary = ("deprecated 0.17.10 name of %s; removed in 0.19.0" % s["alias_of"]
                   if s.get("alias_of") else d["summary"])
        print("%-18s %-12s %-9s %-12s %-8s %s" % (j, d["trigger_skill"], d["model_tier"],
                                                  d["context_loading"]["strategy"],
                                                  s["source"], _clean(summary, 90)))
    if not specs:
        print("(no specs installed)")
    for pth, e in problems:
        print("PROBLEM %s: %s" % (_clean(pth, 200), e))
    print("\npipelines: ingest = %s; promote = %s" % (" -> ".join(PIPELINES["ingest"]),
                                                    " -> ".join(PIPELINES["promote"])))
    print("agent_specialization: %s   skeptic_pass: %s" % (state["agent_specialization"],
                                                         state["skeptic_pass"]))
    if state["agent_specialization"] != "on":
        print("Skills run inline. Turn specialists on with: "
              "gt_settings.py set agent_specialization on")
    return 1 if problems else 0


def cmd_validate(a):
    """A stage spec, a flat spec, a kind delta (checked by composing it with the shipped
    stages), or aliases.json -- told apart by where the file sits and what it holds."""
    path = a.spec_file
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if base == "aliases.json":
        _, problems = read_aliases(path)
    else:
        data, problems = _read_json(path)
        if data is not None:
            stem = os.path.splitext(base)[0]
            if parent == "kinds" or (isinstance(data, dict) and "kind" in data
                                     and "job_type" not in data):
                problems = validate_kind(data, stem)
                if not problems:
                    stage_dir = os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(path))), "stages")
                    if not os.path.isdir(stage_dir):
                        stage_dir = os.path.join(RELEASE_SPECS, "stages")
                    for sp in _json_files(stage_dir, []):
                        sd, errs = read_spec(sp)
                        if sd is None or sd.get("stage") not in KIND_STAGES:
                            continue
                        comp = compose(sd, data)
                        problems += ["%s: %s" % (comp["job_type"], e)
                                     for e in validate_spec(comp)]
            else:
                problems = validate_spec(data, stem)
                if not problems and parent == "stages" and data.get("stage") != stem:
                    problems = ["stage: a stage file must declare stage '%s'" % stem]
    if problems:
        for e in problems:
            print("INVALID %s: %s" % (_clean(path, 200), e))
        return 1
    print("ok %s" % _clean(path, 200))
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
    if r["stage"]:
        print("stage:   %s%s" % (r["stage"], (" (kind %s)" % r["kind"]) if r["kind"] else ""))
    if r["pipeline"]:
        print("pipeline: %s" % " -> ".join(r["pipeline"]))
    print("why:     %s" % _clean(r["why"], 300))
    print("setting: agent_specialization=%s" % r["agent_specialization"])
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


def map_alias_inputs(entry, inputs):
    """An alias's old input names -> the composition's names (skeptic's additions -> findings)."""
    m = entry.get("inputs_map") or {}
    return {m.get(k, k): v for k, v in inputs.items()}


def cmd_render(a):
    spec = _spec_or_exit(a.job_type, a.vault, a.specs_dir)
    if spec.get("alias_of"):
        print("gt_agent_spec: '%s' is the 0.17.10 name of '%s' (kept for one release; use the "
              "new name)" % (a.job_type, spec["alias_of"]), file=sys.stderr)
    try:
        text, info = render(spec["data"], map_alias_inputs(spec, _parse_inputs(a)), a.vault,
                            a.template)
    except ValueError as exc:
        print("gt_agent_spec: %s" % exc, file=sys.stderr)
        return 2
    except IntakeRefused as exc:
        print("gt_agent_spec: render refused: %s" % exc, file=sys.stderr)
        return 1
    if a.json:
        info["spec"], info["source"] = spec["path"], spec["source"]
        info["stage"], info["kind"] = spec.get("stage"), spec.get("kind")
        info["alias_of"] = spec.get("alias_of")
        print(json.dumps(info, indent=2))
    else:
        sys.stdout.write(text)
    return 0


def cmd_check_output(a):
    entry = _spec_or_exit(a.job_type, a.vault, a.specs_dir)
    spec = entry["data"]
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
    problems = check_record(spec, record, entry.get("names", ()))
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
        description="Stage x kind specs for specialist agents. Every subcommand only reads.")
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
    p.add_argument("--skill", required=True,
                   help="gt-ingest, gt-validate, gt-work or gt-promote")
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
