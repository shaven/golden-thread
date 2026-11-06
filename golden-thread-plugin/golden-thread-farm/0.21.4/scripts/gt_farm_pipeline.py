#!/usr/bin/env python3
"""gt_farm_pipeline.py -- the "v4" packaging for running one gt pipeline stage (extract, classify,
draft, generalize) on a big local engine (ladder rung 2). No network of its own: the caller hands
in a `chat(messages, schema)` function, and `gt_farm_send.py pipeline` wires that to a
chat-completions engine from the service table.

Why a packaging and not just a prompt. Measured on the owner's fleet (gt 0.21.4 intake): the same
stage prompt, sent bare, came back acceptable about half the time from a large local model;
packaged like this it came back acceptable about nine times in ten. What the packaging does:

  1. MATERIAL INLINE. The files of the unit are read here, numbered, and placed in the prompt, so
     the engine cites `file:line` from text it can see. The material is never split: a unit that
     does not fit the budget is REFUSED (the stage then runs in Claude) rather than truncated,
     because a silently shortened extract loses coverage without saying so.
  2. ONE EXEMPLAR. A finished answer for a different unit of the same stage, shown as a shape and a
     standard, never as content to copy.
  3. THE OUTPUT FIELDS SPELLED OUT, and the schema bound with `response_format`.
  4. A STAGE CHECKLIST and ONE SELF-AUDIT: the engine re-reads its own answer against the
     checklist and returns a corrected object.
  5. VALIDATE AND RETRY ONCE. The answer is checked locally (required fields, empty sub-fields,
     enum values, citations that resolve to a real file and line); if anything is wrong the engine
     is told exactly what and gets one more try. Whatever is still wrong afterwards is reported in
     `_runner.problems_final`, and the caller escalates.

The packet is a JSON file: {"stage": "extract-...", "output_schema": {field: {"type": "string|list|
boolean|number|object", "description": "...", "required": true}}}. For a list field whose
description says `Objects {a, b, c}` the item keys are required. A description that says
`severity is high|medium|low` makes that an enum the validator enforces. The prompt is the stage's
rendered instructions; an extract prompt names the unit with `### path` and `### unit` headings.
"""
import json
import os
import re
import time

MATERIAL_BUDGET = 140000      # characters of unit material; over this the stage is refused
EXEMPLAR_BUDGET = 9000
MAX_PROBLEMS = 25

SYSTEM = ("You are a careful specialist agent. Rules: (1) Base every statement on the MATERIAL or "
          "INPUTS given; never invent files, paths, names or facts. (2) Every finding or entry "
          "carries a citation in the form file:line (or document and section) that exists in the "
          "material, and a verification state (verified when you read it stated; inferred when you "
          "derived it; assumed otherwise). (3) Follow the job's Output section exactly: the "
          "required fields, nothing else, as ONE JSON object and no prose. (4) If material is "
          "attached below, it IS the unit: read all of it before answering. (5) When classifying "
          "or placing, keep to the levels and destinations the job defines and justify each choice "
          "in one specific sentence, not boilerplate.")

CHECKLISTS = {
    "generalize": [
        "Write out every condition the claim depends on (library or tool and its version, platform, mode, setting, timing). Keep each one in the statement unless the evidence shows it is not needed.",
        "The thing the fact is ABOUT (a named library, tool, protocol or API) is not project-specific: keep its name. Remove only this project's own names, hosts, paths and one-off figures.",
        "A condition or figure the verifier said it could NOT establish is not part of the verified claim: leave it out of the statement and list it in removed.",
        "Ask: is this a property of the library/tool/platform (can generalize), or a design choice of this one codebase (does not generalize)? A single codebase's layout, naming or workflow is generalizes=false, with the reason in statement.",
        "When generalizes is false, the statement still says WHY in one specific sentence. Never leave it empty or vague.",
        "Never broaden: the general statement must not claim more than the evidence shows, and must not become a tautology."],
    "extract": [
        "Cover every file in the material, not only the largest: each module's purpose, entry points, and how they connect.",
        "For code, record the safeguards: validation gates, guards against invalid or future data, error handling, retry or fill rules, configuration and data-format assumptions, each with file:line.",
        "Record contradictions and inconsistencies between files (an import that does not exist, a parameter named two ways, an example that disagrees with the code).",
        "Every finding has a citation that exists in the material and an honest verification state."],
    "classify": [
        "Every id in the batch gets exactly one classification, or goes under unplaced with its reason. Never drop or invent an id.",
        "Use only the projects, levels and destinations the job defines. Never invent a project or a destination.",
        "Each why names the specific content of THIS finding that decides its destination, in one sentence. No sentence repeated across items.",
        "A finding that only applies to one project stays in that project; a finding that helps outside it may go wider only if the job's rules allow it."],
    "draft": [
        "Every finding id you were given is covered by an entry, and each entry lists the ids it covers. Never add a fact that is not in the findings.",
        "Keep each finding's citation and verification state in the entry text.",
        "Follow the target file's conventions the job states (heading style, dates, append-only wording).",
        "Merge findings that say the same thing into one entry instead of repeating them."],
}

_OBJECTS = re.compile(r"Objects \{([^}]*)\}")
_ENUM = re.compile(r"(\w+) is ([a-z_]+(?:\|[a-z_]+)+)")


def stage_of(packet):
    return str(packet.get("stage", "")).split("-")[0]


def json_schema(desc):
    """The packet's output_schema -> a JSON schema for `response_format`."""
    props, req = {}, []
    for name, spec in desc.items():
        t = (spec.get("type") or "string").lower()
        if t == "list":
            m = _OBJECTS.search(spec.get("description", ""))
            keys = [k.strip() for k in m.group(1).split(",") if k.strip()] if m else []
            props[name] = {"type": "array",
                           "items": {"type": "object", "properties": {k: {} for k in keys},
                                     "required": keys} if keys else {}}
        elif t == "boolean":
            props[name] = {"type": "boolean"}
        elif t in ("number", "integer"):
            props[name] = {"type": "number"}
        elif t in ("object", "dict"):
            props[name] = {"type": "object"}
        else:
            props[name] = {"type": "string"}
        if spec.get("required", True):
            req.append(name)
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


def fields_text(desc):
    return "\n".join("- `%s` (%s%s): %s" % (k, v.get("type", "string"),
                                            "" if v.get("required", True) else ", optional",
                                            v.get("description", "")) for k, v in desc.items())


def read_material(prompt, budget=MATERIAL_BUDGET):
    """-> (files, chars, over_budget). `files` is [(path, numbered text, line count)], read from the
    `### path` / `### unit` headings of an extract prompt; [] for any other stage."""
    mp = re.search(r"### path\n(.+?)\n", prompt)
    if not mp:
        return [], 0, False
    mu = re.search(r"### unit\n(.+?)\n", prompt)
    base = mp.group(1).strip()
    unit = mu.group(1).strip() if mu else "."
    target = base if unit in (".", "") else os.path.join(base, unit)
    files, used = [], 0
    if os.path.isfile(target):
        walk = [(os.path.dirname(target), [], [os.path.basename(target)])]
    else:
        walk = os.walk(target)               # top-down: pruning dns below prunes the descent
    for dp, dns, fs in walk:
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        for f in sorted(fs):
            if f.startswith("."):
                continue
            fp = os.path.join(dp, f)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            body = "".join("%d\t%s" % (i, l) for i, l in enumerate(lines, 1))
            used += len(body)
            files.append((fp, body, len(lines)))
    return files, used, used > budget


def problems(desc, obj, line_counts):
    """What is wrong with `obj`, as sentences the engine can act on (at most MAX_PROBLEMS)."""
    out = []
    if not isinstance(obj, dict):
        return ["the answer is not one JSON object"]
    for name, spec in desc.items():
        req = spec.get("required", True)
        v = obj.get(name)
        t = (spec.get("type") or "string").lower()
        if req and name not in obj:
            out.append("missing field `%s`" % name)
            continue
        if t == "string" and req and not (isinstance(v, str) and v.strip()):
            out.append("`%s` is empty" % name)
        if t == "list" and isinstance(v, list):
            m = _OBJECTS.search(spec.get("description", ""))
            keys = [k.strip() for k in m.group(1).split(",")] if m else []
            for i, it in enumerate(v):
                if keys and not isinstance(it, dict):
                    out.append("`%s[%d]` is not an object" % (name, i))
                    continue
                for k in keys:
                    if isinstance(it, dict) and (k not in it or it[k] in ("", None, [])):
                        out.append("`%s[%d].%s` is empty" % (name, i, k))
            for em in _ENUM.finditer(spec.get("description", "")):
                fld, allowed = em.group(1), em.group(2).split("|")
                for i, it in enumerate(v):
                    if isinstance(it, dict) and fld in it and str(it[fld]) not in allowed:
                        out.append("`%s[%d].%s` = %r is not one of %s" % (name, i, fld, it[fld], allowed))
    if line_counts:
        for i, f in enumerate(obj.get("findings") or []):
            c = str(f.get("citation", "")) if isinstance(f, dict) else ""
            m = re.match(r"\s*`?([^\s:`]+):(\d+)", c)
            if not m:
                out.append("`findings[%d].citation` %r is not file:line" % (i, c[:80]))
                continue
            fn, ln = m.group(1), int(m.group(2))
            hits = [n for p, n in line_counts.items()
                    if p.endswith(fn) or os.path.basename(p) == os.path.basename(fn)]
            if not hits:
                out.append("`findings[%d].citation` names %r, which is not in the material" % (i, fn))
            elif all(ln > n for n in hits):
                out.append("`findings[%d].citation` line %d is past the end of %s (%d lines)"
                           % (i, ln, fn, max(hits)))
    return out[:MAX_PROBLEMS]


def _json_of(text):
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except ValueError:
        return None


def run_stage(chat, prompt, packet, exemplar=None, material_budget=MATERIAL_BUDGET,
              selfcheck=True, validate=True):
    """Run one stage with the v4 packaging. `chat(messages, schema)` -> (text, prompt_tokens,
    completion_tokens). -> (obj or None, meta). Never raises for a bad answer; `meta["error"]` says
    why there is no object. `meta["problems_final"]` is what is still wrong after the one retry."""
    t0 = time.time()
    meta = {"variant": "v4", "calls": 0, "retries": 0, "selfchecks": 0, "problems_first": 0,
            "problems_final": 0, "prompt_tokens": 0, "completion_tokens": 0, "material_chars": 0,
            "over_budget": False, "parsed": False, "error": None}
    desc = packet.get("output_schema") or {}
    schema = json_schema(desc) if desc else None
    checklist = CHECKLISTS.get(stage_of(packet), [])
    files, chars, over = read_material(prompt, material_budget)
    meta["material_chars"] = chars
    if over:
        meta["over_budget"] = True
        meta["error"] = ("the unit's material is %d characters, over the %d budget; it is not split "
                         "or truncated here, so this stage runs in Claude" % (chars, material_budget))
        meta["seconds"] = round(time.time() - t0, 1)
        return None, meta
    line_counts = {fp: n for fp, _, n in files}

    user = prompt
    if desc:
        user += "\n\n## Output fields (return exactly these, as one JSON object)\n" + fields_text(desc) + "\n"
    if checklist:
        user += "\n\n## Checklist (work through each point before answering)\n" + \
            "\n".join("%d. %s" % (i, c) for i, c in enumerate(checklist, 1)) + "\n"
    if exemplar:
        user += ("\n\n## Shape of an acceptable answer (for a DIFFERENT unit of this stage; copy the "
                 "shape and standard, never the content)\n\n```json\n"
                 + json.dumps(exemplar, indent=1, ensure_ascii=False)[:EXEMPLAR_BUDGET] + "\n```\n")
    if files:
        user += ("\n\n## MATERIAL (the unit's files, with line numbers; cite as file:line)\n"
                 + "".join("\n===== FILE %s (%d lines) =====\n%s" % (fp, n, body) for fp, body, n in files))

    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    def ask(history):
        text, pt, ct = chat(history, schema)
        meta["calls"] += 1
        meta["prompt_tokens"] += int(pt or 0)
        meta["completion_tokens"] += int(ct or 0)
        return _json_of(text or ""), text or ""

    try:
        obj, text = ask(msgs)
        if obj is not None and selfcheck and checklist:
            review = ("Audit the JSON answer you just gave against the checklist and the job's rules, "
                      "point by point. Fix anything that breaks a point. Then return the corrected "
                      "JSON object only.\n\n" + "\n".join("%d. %s" % (i, c) for i, c in enumerate(checklist, 1)))
            o2, t2 = ask(msgs + [{"role": "assistant", "content": text}, {"role": "user", "content": review}])
            meta["selfchecks"] += 1
            if isinstance(o2, dict):
                obj, text = o2, t2
        if validate and desc:
            probs = problems(desc, obj, line_counts)
            meta["problems_first"] = len(probs)
            if probs:
                fix = ("Your answer has these problems:\n" + "\n".join("- " + p for p in probs)
                       + "\n\nFix every one (for a citation that does not resolve, correct it from the "
                         "material or drop the finding). Return the corrected JSON object only.")
                o3, _ = ask(msgs + [{"role": "assistant", "content": text}, {"role": "user", "content": fix}])
                meta["retries"] += 1
                if isinstance(o3, dict):
                    obj = o3
            meta["problems_final"] = len(problems(desc, obj, line_counts))
    except Exception as e:                      # noqa: BLE001 -- the engine, the wire, anything
        meta["error"] = "%s: %s" % (type(e).__name__, e)
    meta["parsed"] = isinstance(obj, dict)
    if not meta["parsed"] and not meta["error"]:
        meta["error"] = "the engine's answer was not a JSON object"
    meta["seconds"] = round(time.time() - t0, 1)
    return (obj if meta["parsed"] else None), meta
