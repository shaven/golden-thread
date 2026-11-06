#!/usr/bin/env python3
"""gt_findings.py -- does a recorded finding's verification label match the evidence it carries? (0.21.0)

The findings half of the ONE calibrate entry point: `gt_model_calibrate.py findings` and
`gt_model_calibrate.py relabel` (/gt:gt-calibrate) call this module. Request
2026-10-08-calibrate-verification-state-of-findings.

    findings  [--project SLUG] [--text-file F] [--all] [--json]     report; writes nothing
    relabel   <finding-id> [--yes] [--session ID]                    the owner's yes, through the queue

WHAT IT CHECKS, per finding (a `## ` section of a project's research.md, a memory note, or the
text of a write-back about to land):

  stated      the verification label the entry claims: unverified | self-verified |
              independently verified (an explicit `Verification:` line wins; otherwise the words)
  evidence    what is attached: a COMMAND AND ITS OUTPUT, a MEASUREMENT, the user's own STATEMENT, or
              a FILE AND LINE that was read. A label rests on evidence or it rests on nothing.
  supported   the highest label that evidence earns:
                none attached                                  -> unverified, however it is worded
                a file and line, read (a mechanism claim)      -> unverified (inferred, not run)
                command+output, measurement, statement,
                or a file and line for a plain fact            -> self-verified
                the above AND an independent check named
                (gt-validate, a zero-context validator ...)    -> independently verified
  mechanism   a causal claim ("because", "the cause is", "root cause", "due to"). An observed
              SYMPTOM and its inferred MECHANISM are graded apart: a finding that ran something and
              saw a result, then explains why from reading the source, keeps `self-verified` for the
              symptom and gets `unverified` for the mechanism.

A finding is FLAGGED (the reconcile.json / skeptic rubric's kinds, reused, not a second rubric) when
its stated label is above what its evidence supports (`unverified`: a label with no evidence behind
it) or when an inferred mechanism is written as fact (`overclaimed`: the entry's own note says the
source was read and nothing was run). A finding with NO label and no evidence is `unevidenced`:
unverified by definition, counted, never mislabelled and never flagged for being short.

THE REPORT WRITES NOTHING. A re-label is a NEW entry appended to the project's research.md through
the write queue (Core rule 1), after the owner's yes (`relabel --yes`), saying what the calibrated
label is and why; the recorded finding is never rewritten (research.md is append-only).

Heuristic, deterministic, no model call: the point is a floor, not a verdict. It reads words and
shapes; it cannot know a command's output was real. What it can refuse to do is let a label stand
on nothing.

Exit: 0 clean | 1 something is flagged | 2 usage.
"""
import datetime
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

LEVELS = ("unverified", "self-verified", "independently verified")

# -- the vocabulary (kept short; each is a judgement a reader can see) ---------------------------
INDEPENDENT = re.compile(
    r"independently[\s-]+(?:verified|validated|re-?derived|confirmed)|gt-validate|zero-context|"
    r"fresh[- ]context (?:validat|verif)|second session (?:verified|re-?derived)|re-?derived by", re.I)
SELF = re.compile(r"self[\s-]+verified|(?<!un)(?<!not )(?<!not yet )\bverified\b|\bconfirmed\b", re.I)
UNVER = re.compile(r"\bunverified\b|\bunconfirmed\b", re.I)
LABEL_LINE = re.compile(r"^\W*\**\s*verification\s*\**\s*:\s*\**\s*(.+)$", re.I | re.M)
CAUSE = re.compile(
    r"\bbecause\b|\broot[- ]cause\b|\bthe cause\b|\bcaused by\b|\bdue to\b|\bthe reason\b|"
    r"\bmechanism\b|\bso that the\b|\bwhich is why\b|\bthat is why\b|\bexplains why\b", re.I)
INFERRED_CUE = re.compile(
    r"read (?:the )?(?:source|code)|source (?:was )?read|no test (?:was )?run|not (?:yet )?"
    r"(?:run|tested|executed|observed|reproduced|measured)|from the code|never executed|"
    r"only read|untested|\binferred\b|not verified", re.I)
HEDGE = re.compile(r"\bprobably\b|\blikely\b|\bmay\b|\bmight\b|\bappears?\b|\bseems?\b|\bsuspect|"
                   r"\bhypothes|\bpossibly\b|\bperhaps\b", re.I)
OUTPUT_MARK = re.compile(
    r"printed|prints|output|returned|\bexit(?:ed)?\b\s*\d?|→|->|=>|showed|shows|gave|\bgot\b|"
    r"reported|refused|failed with|\bpassed\b|\bresult\b", re.I)
COMMAND = re.compile(r"`[^`\n]+`|^\s*\$ .+$|^```", re.M)
RAN = re.compile(r"\b(?:ran|run|running|executed|invoked|tried|observed live|reproduced)\b", re.I)
MEASURE = re.compile(
    r"\b(?:measured|timed|benchmark(?:ed)?|counted)\b[^.\n]{0,80}\d|\d[\d,.]*\s?"
    r"(?:ms|sec|seconds|MB|GB|KB|tokens|%)\b[^.\n]{0,40}\b(?:measured|timed)\b", re.I)
STATEMENT = re.compile(
    r"\b(?:owner|user)\b[^.\n]{0,40}\b(?:said|stated|confirmed|decided|answered|told|asked)\b|"
    r"\((?:owner|user)[,)]|\b(?:owner|user)'s own (?:statement|words)|"
    r"[\"“][^\"”\n]{6,}[\"”]\s*[—-]\s*(?:the )?(?:owner|user)", re.I)
FILE_LINE = re.compile(r"(?<![\w/.-])[\w./-]+\.(?:py|sh|md|json|js|ts|go|rs|c|h|java|yml|yaml|toml|"
                       r"swift|cmd|ps1):\d+\b|\bline\s+\d+\b", re.I)
SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text):
    return [s.strip() for s in SENT.split(text) if s.strip()]


def entry_id(path, heading):
    return "f-" + hashlib.sha1((path + "\n" + heading).encode("utf-8")).hexdigest()[:8]


# -- reading the entries ----------------------------------------------------------------------
def split_sections(text):
    """-> [(heading, body)] for each `## ` section; text before the first is ignored."""
    out, head, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if head is not None:
                out.append((head, "\n".join(buf).strip("\n")))
            head, buf = line[3:].strip(), []
        elif head is not None:
            buf.append(line)
    if head is not None:
        out.append((head, "\n".join(buf).strip("\n")))
    return out


def _title(heading):
    return re.sub(r"^\d{4}-\d{2}-\d{2}:?\s*", "", heading).strip() or heading


def vault_entries(vault, project=None):
    """-> [{id, path, heading, title, body, source}] for research.md sections and memory notes."""
    out = []
    root = os.path.join(vault, "Projects")
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith((".", "_")) and d not in ("spool", "archive"))
        rel_dir = os.path.relpath(dp, vault).replace(os.sep, "/")
        parts = rel_dir.split("/")
        for f in sorted(fn):
            rel = rel_dir + "/" + f
            slug = "/".join(parts[1:2]) if len(parts) > 1 else ""
            if project and slug != project:
                continue
            try:
                with open(os.path.join(dp, f), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if f == "research.md":
                for head, body in split_sections(text):
                    out.append({"id": entry_id(rel, head), "path": rel, "heading": head,
                                "title": _title(head), "body": body, "source": "research"})
            elif "memory" in parts and f.endswith(".md") and f != "MEMORY.md":
                m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
                body = m.group(2) if m else text
                name = re.search(r"^name:\s*(.+)$", m.group(1), re.M) if m else None
                head = name.group(1).strip().strip("\"'") if name else f[:-3]
                out.append({"id": entry_id(rel, head), "path": rel, "heading": head,
                            "title": head, "body": body.strip(), "source": "memory"})
    return out


def text_entries(text, path="(write-back)"):
    return [{"id": entry_id(path, h), "path": path, "heading": h, "title": _title(h), "body": b,
             "source": "text"} for h, b in split_sections(text)]


# -- grading ----------------------------------------------------------------------------------
def stated_label(body):
    """-> a LEVELS member, or None when the entry states none. An explicit Verification: line wins."""
    m = LABEL_LINE.search(body)
    scope = m.group(1) if m else body
    if INDEPENDENT.search(scope):
        return LEVELS[2]
    if re.search(r"\bunverified\b|\binferred\b|\bassumed\b", scope, re.I) and m:
        return LEVELS[0]
    if SELF.search(scope):
        return LEVELS[1]
    if m and UNVER.search(scope):
        return LEVELS[0]
    return None


def evidence_of(body):
    """-> {executed, measurement, statement, read, independent}: booleans, from the entry's words."""
    executed = bool((COMMAND.search(body) or RAN.search(body)) and OUTPUT_MARK.search(body)
                    and (COMMAND.search(body) or re.search(r"\bran\b|\bexecuted\b", body, re.I)))
    return {"executed": executed,
            "measurement": bool(MEASURE.search(body)),
            "statement": bool(STATEMENT.search(body)),
            "read": bool(FILE_LINE.search(body)),
            "independent": bool(INDEPENDENT.search(body))}


def _split(body):
    """-> (observed, inferred) sentences of a mixed claim. The label and evidence lines are dropped."""
    obs, inf = [], []
    for s in sentences(body):
        if re.match(r"^\W*\**\s*(verification|evidence)\b", s, re.I):
            continue
        (inf if (CAUSE.search(s) or INFERRED_CUE.search(s)) else obs).append(s)
    return " ".join(obs), " ".join(inf)


def grade(entry):
    body = entry["body"]
    stated = stated_label(body)
    ev = evidence_of(body)
    has_mech = bool(CAUSE.search(body))
    observed_ev = ev["executed"] or ev["measurement"] or ev["statement"]
    if observed_ev or (ev["read"] and not has_mech):
        supported = 1
    else:
        supported = 0
    if supported and ev["independent"]:
        supported = 2
    inferred_note = bool(INFERRED_CUE.search(body))
    hedged = bool(HEDGE.search(body))
    kinds = []
    rank = {lvl: i for i, lvl in enumerate(LEVELS)}
    if stated is not None and rank[stated] > supported:
        kinds.append("unverified" if supported == 0 else "overclaimed")
    mech_as_fact = has_mech and not observed_ev and not hedged and (inferred_note or
                                                                    (stated is not None and rank[stated] >= 1))
    mech_over_symptom = has_mech and inferred_note and not hedged
    if (mech_as_fact or mech_over_symptom) and "overclaimed" not in kinds:
        kinds.append("overclaimed")
    calibrated = {"overall": LEVELS[supported]}
    split = None
    if has_mech:
        calibrated["mechanism"] = LEVELS[0] if (inferred_note or not observed_ev or supported == 0) \
            else LEVELS[supported]
        calibrated["symptom"] = LEVELS[1] if observed_ev else LEVELS[0]
        if observed_ev and calibrated["mechanism"] == LEVELS[0]:
            obs, inf = _split(body)
            split = {"observed": obs, "inferred": inf}
    out = {"id": entry["id"], "path": entry["path"], "heading": entry["heading"],
           "title": entry["title"], "stated": stated, "evidence": ev, "mechanism": has_mech,
           "kinds": kinds, "calibrated": calibrated, "split": split,
           "flagged": bool(kinds), "has_evidence": bool(observed_ev or ev["read"])}
    if split is None and kinds and has_mech:
        obs, inf = _split(body)
        out["split"] = {"observed": obs, "inferred": inf}
    return out


def scan(entries):
    graded = [grade(e) for e in entries]
    flagged = [g for g in graded if g["flagged"]]
    clean = [g for g in graded if not g["flagged"] and g["has_evidence"]]
    unevid = [g for g in graded if not g["flagged"] and not g["has_evidence"]]
    return {"scanned": len(graded), "flagged": flagged,
            "clean": [{"id": g["id"], "title": g["title"], "calibrated": g["calibrated"]["overall"]}
                      for g in clean],
            "unevidenced": [{"id": g["id"], "title": g["title"], "path": g["path"],
                             "calibrated": {"overall": LEVELS[0]}} for g in unevid]}


# -- rendering ----------------------------------------------------------------------------------
def render(rep, limit=15):
    lines = ["Findings: %d checked -- %d overstated, %d supported, %d with no evidence "
             "(unverified by definition)" % (rep["scanned"], len(rep["flagged"]), len(rep["clean"]),
                                             len(rep["unevidenced"]))]
    for g in rep["flagged"][:limit]:
        lines.append("  OVERSTATED %s  %s  (%s)" % (g["id"], g["title"][:80], g["path"]))
        stated = g["stated"] or "no label"
        c = g["calibrated"]
        if "mechanism" in c:
            lines.append("      stated %s; calibrated: symptom %s, mechanism %s [%s]" % (
                stated, c["symptom"], c["mechanism"], ", ".join(g["kinds"])))
        else:
            lines.append("      stated %s; calibrated %s -- no evidence attached [%s]" % (
                stated, c["overall"], ", ".join(g["kinds"]))
                if not g["has_evidence"] else "      stated %s; calibrated %s [%s]" % (
                    stated, c["overall"], ", ".join(g["kinds"])))
        if g["split"]:
            lines.append("      split: observed (%s): %s" % (c.get("symptom", "self-verified"),
                                                             (g["split"]["observed"] or "-")[:160]))
            lines.append("             inferred (%s): %s" % (c.get("mechanism", "unverified"),
                                                             (g["split"]["inferred"] or "-")[:160]))
        lines.append("      re-label (only on the owner's yes): gt_model_calibrate.py relabel %s --yes"
                     % g["id"])
    if len(rep["flagged"]) > limit:
        lines.append("  ... %d more (--json lists all)" % (len(rep["flagged"]) - limit))
    return "\n".join(lines)


def relabel_entry(g, today=None, session=""):
    """The note the queue appends for a finding the owner agreed to re-label."""
    today = today or datetime.date.today().isoformat()
    c = g["calibrated"]
    parts = ["Calibrated verification state of the entry \"%s\" (%s): **%s**; it was stated as %s."
             % (g["heading"], g["path"], c["overall"], g["stated"] or "unlabelled")]
    if g.get("split"):
        parts.append("Observed (%s): %s" % (c.get("symptom", "self-verified"),
                                            g["split"]["observed"] or "-"))
        parts.append("Inferred (%s): %s" % (c.get("mechanism", "unverified"),
                                            g["split"]["inferred"] or "-"))
    parts.append("Why: %s." % ("; ".join(
        {"unverified": "a label above unverified with no evidence attached",
         "overclaimed": "an inferred mechanism written as fact"}[k] for k in g["kinds"])))
    return ("## %s: Recalibrated: %s\n\n%s\n\n- **Verification**: unverified\n"
            "- **Evidence**: the owner confirmed this re-label after a /gt:gt-calibrate findings "
            "report (session %s). The original entry is unchanged.\n"
            % (today, g["title"], "\n".join(parts), session or "unknown"))


def research_path_for(path):
    """The research.md a calibration note about `path` is appended to."""
    if path.endswith("/research.md") or "/memory/" not in path:
        return path
    return path.split("/memory/")[0] + "/research.md"
