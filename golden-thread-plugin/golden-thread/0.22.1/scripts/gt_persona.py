#!/usr/bin/env python3
"""gt_persona.py -- personas: named bundles of defaults, shipped as data (0.21.0, Stage 1).

A persona changes what gt OFFERS, never what it ENFORCES. Stage 1 carries one section, `scaffold`:
extra files and extra sections for a new project, on top of the standard structure. 0.21.4 adds
three emphasis fields: `lotr_templates` (what gt-lotr suggests), `featured_skills` (what gt_hints
prefers as next steps) and `session_focus` (which SessionStart surface sections come first --
an order only, every section still shows), and the day-end emphasis `close_focus` and
`close_summary` (what /gt:gt-close puts first; an order only, nothing is skipped).

  gt_persona.py list [--packs DIR]
  gt_persona.py show <name> [--packs DIR]
  gt_persona.py resolve [--project SLUG] [--vault V] [--json] [--title T] [--packs DIR]
  gt_persona.py apply <project-dir> [--project SLUG] [--vault V] [--title T] [--personas a,b]
                                              # additive, idempotent; vault_init uses the same call
  gt_persona.py check [--packs DIR]          # schema check over every pack; exit 1 on a problem
  gt_persona.py featured [--project SLUG] [--vault V] [--personas a,b] [--json]
                                              # the persona's featured gt commands (0.21.4)
  gt_persona.py close-plan [--project SLUG] [--vault V] [--personas a,b] [--json]
                                              # what /gt:gt-close puts first (0.21.4); empty
                                              # when no persona sets close_focus or close_summary
  gt_persona.py handoff [--project SLUG] [--vault V] [--personas a,b]
                                              # the install summary's first-day lines for a plain-mode
                                              # persona (0.22.0, phase 95e); empty for the others
  gt_persona.py audience [--project SLUG] [--vault V]
                                              # the Audience block for writing agents and wrap-ups (#45);
                                              # empty when persona_aligned is off or no persona is set
  gt_persona.py farm-tasks [--project SLUG] [--vault V] [--personas a,b] [--engines] [--json]
                                              # the routine work this role can hand to a local engine, and
                                              # when Claude takes over (0.21.4); --engines adds, per task,
                                              # what each calibrated local engine of yours scored for it

RESOLUTION. The user's list is the `persona` key of ~/.claude/vault-config.json (a comma list or a
JSON array). A project's README frontmatter `persona:` REPLACES it for that project (`none` means
no persona). Several personas: the union of their scaffold files and sections; where two name the
same file (or the same section heading in one file) the first listed wins. No persona: an empty
scaffold, and callers change nothing -- behaviour is byte-identical to a gt without personas.

PACKS come from packs/personas/ in the RELEASE dir (the parent of this script's scripts/ folder),
never the caller's directory: a hook runs a copy of a script, and a copy without packs/ is the bug
the secrets scan had. `--packs` overrides for tests and local authoring.

REFUSED at load: any key named lockdown, sandbox_mode, unlock, test_gate or protected_paths at any
depth, any unknown key, a path that escapes the project or names a standard file, a name that does
not match its file. A refused pack is reported and never listed or resolved.
Stdlib only.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTECTED = ("lockdown", "sandbox_mode", "unlock", "test_gate", "protected_paths")
STANDARD_FILES = ("README.md", "idea.md", "source.md", "research.md", "decisions.md",
                  "design.md", "CLAUDE.md", "runbook.md", "memory/MEMORY.md")
SECTION_TARGETS = ("README.md", "idea.md", "source.md", "research.md", "decisions.md",
                   "design.md", "runbook.md")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_PACK_BYTES = 64 * 1024
TOP_KEYS = {"schema", "name", "title", "description", "scaffold", "pane", "audience",
            "lotr_templates", "featured_skills", "session_focus", "close_focus", "close_summary",
            "farm_tasks", "extends", "plain_mode", "handoff"}
AUDIENCE_MAX = 300
PANE_MAX, PANE_LABEL_MAX, PANE_PROMPT_MAX = 12, 22, 500
# 0.21.4 (ADR-18 build-out). Three emphasis fields; none of them hides or enforces anything.
#   lotr_templates   LOTR catalog template names gt-lotr suggests (`lotr setup`, `template list`)
#   featured_skills  5 to 8 gt commands, /gt:gt-<skill>, preferred when gt_hints picks next steps
#   session_focus    SessionStart surface sections to show FIRST; every other section follows
#                    in its usual order, so nothing is hidden
LOTR_TEMPLATES_MAX = 8
FEATURED_MIN, FEATURED_MAX = 5, 8
FEATURED_RE = re.compile(r"^/gt:(gt-[a-z0-9-]{1,40})$")
# gt_surface.build's sections, in their default order (a test holds the two together).
SURFACE_SECTIONS = ("must_do", "handoffs", "tasks", "write_queue", "sandbox", "pickup", "state")
# 0.21.4: what /gt:gt-close (gt-work's write-back, then the wrap-up) puts first for a role.
#   close_focus    items from CLOSE_ITEMS, in the order to lead with
#   close_summary  "brief-first" opens the wrap-up with a short plain-language brief, detail below;
#                  "standard" (or absent) is the wrap-up as it always was
# Order and emphasis only: every gt-work step still runs and everything it would capture is still
# captured. CLOSE_CAPTURE is what gt-work always captures, in its own order; a plan names each of
# them exactly once (the led ones first, then the rest), so a persona cannot drop one. The lenses
# are kinds of finding to look for first; each lands where gt-work already writes.
# key: (plain label, where it lands) -- one line each, the plan prints them within 80 columns
CLOSE_ITEMS = {
    "findings":   ("Research findings, with their evidence", "research.md"),
    "decisions":  ("Decisions and why, with what was rejected", "decisions.md (an ADR)"),
    "design":     ("Design changes and a spec when settled", "design.md (you review it), spec.md"),
    "runbook":    ("Runbook steps learned: commands, gotchas", "runbook.md"),
    "memory":     ("Memory notes", "memory/ and memory/MEMORY.md"),
    "tasks":      ("Tasks added, done and changed", "README.md ## Tasks"),
    "handoffs":   ("What did not reach a file: the handoff", "a handoff + one waiting task each"),
    "promotions": ("Patterns and promotion candidates", "asked one at a time; /gt:gt-promote"),
    "code":       ("Files and commits touched, unpushed work", "research.md; a handoff's repo state"),
    "deadlines":  ("Deadlines (must do) and waiting-on items", "tasks with due:: and waiting::"),
    "incidents":  ("Incidents: what broke, the impact, the fix", "research.md; incident-log.md if present"),
    "migrations": ("Migration, backup and rotation steps", "runbook.md; migration-log.md if present"),
    "config":     ("Config and network changes", "research.md; a change log if present"),
    "topology":   ("Hosts and where things run", "source.md and README topology:"),
}
CLOSE_CAPTURE = ("findings", "decisions", "design", "runbook", "memory", "tasks", "handoffs",
                 "promotions")
CLOSE_SUMMARIES = ("standard", "brief-first")


# 0.21.4: the routine, low-judgement work a role can hand to a local engine (gt-farm), each with the rule
# for when Claude takes over. A suggestion shown in /gt:gt-settings models, the role guide and the gt-farm
# skill; it enforces nothing and sends nothing (ADR-18). The classes are gt-farm's own task classes
# (gt_farm_probes.TASK_CLASSES: the farm ladder's names plus summarise and review); a test holds the two
# lists together.
FARM_CLASSES = ("log-triage", "quote-extract", "extract", "classify", "draft", "generalize", "review",
                "summarise")
FARM_TASKS_MIN, FARM_TASKS_MAX = 3, 6

# 0.22.0 (phase 95b, stage 2). Three more fields, all data and all presentation:
#   extends     up to EXTENDS_MAX pack names whose emphasis, scaffold, farm tasks and flags this pack
#               inherits. The pack's own values come first (so its own file wins a clash), then its
#               parents' in the order listed, each name once; a cycle is a `check` problem and is cut
#               at run time. An unknown parent is a `check` problem and is skipped at run time.
#   plain_mode  true: replies lead with what happened and the next step, jargon is glossed once, paths
#               only on request (the pane, the installer's pane question and the calm session start
#               read it). The first pack in the expanded order that sets it decides.
#   handoff     what the installer's summary says after an install with this persona: three core
#               commands and the demo track whose tour and walk-through to try (phase 95e).
# The `persona_primary` setting (vault-config.json) names which listed persona leads; unset, the first
# listed leads, as before.
EXTENDS_MAX = 3
HANDOFF_KEYS = {"commands", "track"}
FARM_TASK_KEYS = {"id", "class", "title", "does", "escalate"}
FARM_TITLE_MAX, FARM_DOES_MAX, FARM_ESCALATE_MAX = 60, 170, 220


class PersonaError(Exception):
    pass


def packs_dir(override=None):
    """packs/personas/ in the release dir this script lives in (or the override)."""
    if override:
        return override
    return os.path.join(os.path.dirname(HERE), "packs", "personas")


def _walk_keys(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield path + "/" + str(k), str(k)
            for x in _walk_keys(v, path + "/" + str(k)):
                yield x
    elif isinstance(node, list):
        for i, v in enumerate(node):
            for x in _walk_keys(v, "%s[%d]" % (path, i)):
                yield x


def _bad_path(p):
    if not isinstance(p, str) or not p or "\\" in p or p.startswith("/") or ":" in p:
        return True
    parts = p.split("/")
    if any(s in ("", ".", "..") or s.startswith(".") for s in parts):
        return True
    return not p.endswith(".md")


def _pane_text_ok(v, limit):
    return (isinstance(v, str) and v.strip() != "" and len(v) <= limit
            and not any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "\u2028\u2029" for c in v))


def _pane_problems(pane):
    """The gt pane's default buttons (item #41): up to 12 of {label, prompt}, one line each."""
    if not isinstance(pane, list) or not pane or len(pane) > PANE_MAX:
        return ["pane must be a list of 1 to %d buttons" % PANE_MAX]
    out = []
    for i, b in enumerate(pane):
        if not isinstance(b, dict) or set(b) != {"label", "prompt"}:
            out.append("pane[%d] must be {label, prompt}" % i)
        elif not _pane_text_ok(b["label"], PANE_LABEL_MAX):
            out.append("pane[%d].label must be 1 to %d characters with no control character" % (i, PANE_LABEL_MAX))
        elif not _pane_text_ok(b["prompt"], PANE_PROMPT_MAX):
            out.append("pane[%d].prompt must be one non-empty line of at most %d characters" % (i, PANE_PROMPT_MAX))
    return out


def _str_list(v, lo, hi, ok):
    """A list of lo..hi distinct strings, each passing ok(). -> True/False."""
    return (isinstance(v, list) and lo <= len(v) <= hi and len(set(map(str, v))) == len(v)
            and all(isinstance(x, str) and ok(x) for x in v))


def _extends_problems(data, name=None):
    out = []
    if "extends" in data and not _str_list(data["extends"], 1, EXTENDS_MAX,
                                           lambda x: bool(NAME_RE.match(x)) and x != name):
        out.append("extends must be 1 to %d distinct other pack names" % EXTENDS_MAX)
    if "plain_mode" in data and not isinstance(data["plain_mode"], bool):
        out.append("plain_mode must be true or false")
    if "handoff" in data:
        h = data["handoff"]
        if (not isinstance(h, dict) or set(h) != HANDOFF_KEYS
                or not _str_list(h.get("commands"), 3, 3, lambda x: bool(FEATURED_RE.match(x)))
                or not isinstance(h.get("track"), str) or not NAME_RE.match(h["track"])):
            out.append("handoff must be {commands: three gt commands like /gt:gt-open, track: a demo track name}")
    return out


def _emphasis_problems(data):
    """lotr_templates, featured_skills, session_focus (0.21.4): shape only. Whether each named
    template is in the LOTR catalog and each command has a skill is checked by the tests and, for
    skills, by `check` when the release's skills/ folder is beside the packs."""
    out = []
    if "lotr_templates" in data and not _str_list(data["lotr_templates"], 0, LOTR_TEMPLATES_MAX,
                                                  lambda x: bool(NAME_RE.match(x))):
        out.append("lotr_templates must be a list of at most %d distinct template names"
                   % LOTR_TEMPLATES_MAX)
    if "featured_skills" in data and not _str_list(data["featured_skills"], FEATURED_MIN, FEATURED_MAX,
                                                   lambda x: bool(FEATURED_RE.match(x))):
        out.append("featured_skills must be %d to %d distinct gt commands like /gt:gt-open"
                   % (FEATURED_MIN, FEATURED_MAX))
    if "session_focus" in data and not _str_list(data["session_focus"], 1, len(SURFACE_SECTIONS),
                                                 lambda x: x in SURFACE_SECTIONS):
        out.append("session_focus must be a list of distinct sections from: %s"
                   % ", ".join(SURFACE_SECTIONS))
    if "close_focus" in data and not _str_list(data["close_focus"], 1, len(CLOSE_ITEMS),
                                               lambda x: x in CLOSE_ITEMS):
        out.append("close_focus must be a list of distinct items from: %s"
                   % ", ".join(CLOSE_ITEMS))
    if "close_summary" in data and data["close_summary"] not in CLOSE_SUMMARIES:
        out.append("close_summary must be one of: %s" % ", ".join(CLOSE_SUMMARIES))
    return out


def _farm_task_problems(tasks):
    """farm_tasks (0.21.4): 3 to 6 of {id, class, title, does, escalate}; one line each."""
    if not isinstance(tasks, list) or not FARM_TASKS_MIN <= len(tasks) <= FARM_TASKS_MAX:
        return ["farm_tasks must be a list of %d to %d tasks" % (FARM_TASKS_MIN, FARM_TASKS_MAX)]
    out, seen = [], set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            out.append("farm_tasks[%d] must be an object" % i)
            continue
        extra = set(t) - FARM_TASK_KEYS
        if extra and not (extra & set(PROTECTED)):
            out.append("farm_tasks[%d] has unknown key(s): %s" % (i, ", ".join(sorted(extra))))
        for k in sorted(FARM_TASK_KEYS - set(t)):
            out.append("farm_tasks[%d] is missing %s" % (i, k))
        if not isinstance(t.get("id"), str) or not NAME_RE.match(t.get("id") or ""):
            out.append("farm_tasks[%d].id must match %s" % (i, NAME_RE.pattern))
        elif t["id"] in seen:
            out.append("farm_tasks[%d].id %r is repeated" % (i, t["id"]))
        else:
            seen.add(t["id"])
        if t.get("class") not in FARM_CLASSES:
            out.append("farm_tasks[%d].class must be one of: %s" % (i, ", ".join(FARM_CLASSES)))
        for k, limit in (("title", FARM_TITLE_MAX), ("does", FARM_DOES_MAX), ("escalate", FARM_ESCALATE_MAX)):
            if k in t and not _pane_text_ok(t[k], limit):
                out.append("farm_tasks[%d].%s must be one non-empty line of at most %d characters"
                           % (i, k, limit))
    return out


def validate_pack(data, name):
    """-> a list of problems; empty means the pack is acceptable."""
    if not isinstance(data, dict):
        return ["pack is not a JSON object"]
    probs = []
    for where, key in _walk_keys(data):
        if key in PROTECTED:
            probs.append("names %s (%s): a persona cannot touch enforcement" % (key, where))
    for k in data:
        if k not in TOP_KEYS and k not in PROTECTED:
            probs.append("unknown key %r" % k)
    if data.get("schema") != 1:
        probs.append("schema must be 1")
    if not isinstance(data.get("name"), str) or not NAME_RE.match(data.get("name") or ""):
        probs.append("name must match %s" % NAME_RE.pattern)
    elif data["name"] != name:
        probs.append("name %r does not match its file name %r" % (data["name"], name))
    if not isinstance(data.get("title"), str) or not data.get("title"):
        probs.append("title must be a non-empty string")
    if "description" in data and not isinstance(data["description"], str):
        probs.append("description must be a string")
    if "audience" in data and not _pane_text_ok(data["audience"], AUDIENCE_MAX):
        probs.append("audience must be one non-empty line of at most %d characters" % AUDIENCE_MAX)
    if "pane" in data:
        probs.extend(_pane_problems(data["pane"]))
    probs.extend(_emphasis_problems(data))
    probs.extend(_extends_problems(data, data.get("name") if isinstance(data.get("name"), str) else None))
    if "farm_tasks" in data:
        probs.extend(_farm_task_problems(data["farm_tasks"]))
    sc = data.get("scaffold")
    if not isinstance(sc, dict):
        return probs + ["scaffold must be an object"]
    for k in sc:
        if k not in ("files", "sections") and k not in PROTECTED:
            probs.append("unknown scaffold key %r" % k)
    files = sc.get("files", [])
    if not isinstance(files, list):
        probs.append("scaffold.files must be a list")
        files = []
    for i, f in enumerate(files):
        if not isinstance(f, dict) or set(f) - {"path", "content"} or "path" not in f or "content" not in f:
            probs.append("scaffold.files[%d] must be {path, content}" % i)
            continue
        if _bad_path(f["path"]):
            probs.append("scaffold.files[%d].path %r must be a relative .md path inside the project" % (i, f["path"]))
        elif f["path"] in STANDARD_FILES:
            probs.append("scaffold.files[%d].path %r is a standard project file; a persona adds, never replaces" % (i, f["path"]))
        if not isinstance(f["content"], str):
            probs.append("scaffold.files[%d].content must be a string" % i)
    secs = sc.get("sections", [])
    if not isinstance(secs, list):
        probs.append("scaffold.sections must be a list")
        secs = []
    for i, s in enumerate(secs):
        if not isinstance(s, dict) or set(s) != {"file", "heading", "body"}:
            probs.append("scaffold.sections[%d] must be {file, heading, body}" % i)
            continue
        if s["file"] not in SECTION_TARGETS:
            probs.append("scaffold.sections[%d].file %r is not one of %s" % (i, s["file"], ", ".join(SECTION_TARGETS)))
        if not isinstance(s["heading"], str) or not s["heading"].strip() or "\n" in s["heading"]:
            probs.append("scaffold.sections[%d].heading must be one non-empty line" % i)
        if not isinstance(s["body"], str):
            probs.append("scaffold.sections[%d].body must be a string" % i)
    return probs


def _load_file(path, name):
    """-> (data, problems)."""
    try:
        if os.path.getsize(path) > MAX_PACK_BYTES:
            return None, ["pack is larger than %d bytes" % MAX_PACK_BYTES]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, ["cannot be read as JSON (%s)" % type(exc).__name__]
    probs = validate_pack(data, name)
    return (None if probs else data), probs


def _pack_files(pdir):
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return []
    return [n for n in names if n.endswith(".json") and not n.endswith(".schema.json")]


def skills_dir():
    """The release's skills/ folder, or None (a hooks-dir copy has none)."""
    d = os.path.join(os.path.dirname(HERE), "skills")
    return d if os.path.isdir(d) else None


def featured_missing(data, sdir):
    """-> the featured_skills commands of a pack with no skills/<name>/SKILL.md under sdir."""
    out = []
    for c in (data or {}).get("featured_skills") or []:
        m = FEATURED_RE.match(c)
        if not m or not os.path.isfile(os.path.join(sdir, m.group(1), "SKILL.md")):
            out.append(c)
    return out


def check_all(pdir=None, sdir=None):
    """-> [(file, problem)] for every pack under pdir; a missing directory is itself a problem.
    With a skills folder (sdir, else the release's own), a featured command with no skill is one."""
    pdir = packs_dir(pdir)
    sdir = sdir or skills_dir()
    if not os.path.isdir(pdir):
        return [(pdir, "persona pack directory is missing")]
    out, graph = [], {}
    for fn in _pack_files(pdir):
        data, probs = _load_file(os.path.join(pdir, fn), fn[:-5])
        out.extend((fn, p) for p in probs)
        if data and sdir:
            out.extend((fn, "featured_skills names %s, which has no skill in %s" % (c, sdir))
                       for c in featured_missing(data, sdir))
        if data:
            graph[fn[:-5]] = data.get("extends") or []
    for n, parents in graph.items():
        for par in parents:
            if par not in graph:
                out.append((n + ".json", "extends names %s, which is not a valid pack" % par))
        if _reaches(graph, n, n):
            out.append((n + ".json", "extends makes a cycle"))
    return out


def _reaches(graph, start, goal):
    """True when goal is reachable from start's parents (so start == goal is a cycle)."""
    todo, seen = list(graph.get(start, [])), set()
    while todo:
        n = todo.pop()
        if n == goal:
            return True
        if n not in seen:
            seen.add(n)
            todo.extend(graph.get(n, []))
    return False


def list_packs(pdir=None):
    """-> [{name, title, description}] for the packs that pass the schema, in name order."""
    pdir = packs_dir(pdir)
    out = []
    for fn in _pack_files(pdir):
        data, _ = _load_file(os.path.join(pdir, fn), fn[:-5])
        if data:
            out.append({"name": data["name"], "title": data["title"], "description": data.get("description", "")})
    return out


def valid_names(pdir=None):
    return [p["name"] for p in list_packs(pdir)]


def load_pack(name, pdir=None):
    pdir = packs_dir(pdir)
    valid = valid_names(pdir)
    if name not in valid:
        raise PersonaError("unknown persona %r; valid personas: %s" % (name, ", ".join(valid) or "(none installed)"))
    data, _ = _load_file(os.path.join(pdir, name + ".json"), name)
    return data


def _names(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("[", "").replace("]", "").split(",")
    out = []
    for v in value if isinstance(value, list) else []:
        v = str(v).strip().strip("'\"").lower()
        if v and v != "none" and v not in out:
            out.append(v)
    return out


def _config_path(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".claude", "vault-config.json")


def user_personas(home=None):
    try:
        with open(_config_path(home), encoding="utf-8") as fh:
            return _names(json.load(fh).get("persona"))
    except (OSError, ValueError, AttributeError):
        return []


def _vault(vault, home=None):
    if vault:
        return vault
    if os.environ.get("GT_VAULT"):
        return os.environ["GT_VAULT"]
    try:
        with open(_config_path(home), encoding="utf-8") as fh:
            return json.load(fh).get("vault_path")
    except (OSError, ValueError, AttributeError):
        return None


def _project_readme(vault, slug):
    base = os.path.join(vault, "Projects")
    cands = [os.path.join(base, slug, "README.md")]
    try:
        for d in sorted(os.listdir(base)):
            cands.append(os.path.join(base, d, slug, "README.md"))
    except OSError:
        pass
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def project_override(vault, slug):
    """-> None (no `persona:` key) or the project's list (possibly empty for `none`)."""
    readme = _project_readme(vault, slug) if vault and slug else None
    if not readme:
        return None
    try:
        with open(readme, encoding="utf-8") as fh:
            text = fh.read().replace("\r\n", "\n")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    for line in text[4:end].split("\n"):
        m = re.match(r"^persona:\s*(.*)$", line)
        if m:
            return _names(m.group(1))
    return None


def primary(home=None):
    """-> the `persona_primary` name from vault-config.json, or "" (0.22.0)."""
    try:
        with open(_config_path(home), encoding="utf-8") as fh:
            v = json.load(fh).get("persona_primary")
    except (OSError, ValueError, AttributeError):
        return ""
    return v.strip().lower() if isinstance(v, str) and v.strip().lower() != "none" else ""


def resolve(vault=None, slug=None, home=None):
    """-> the effective persona names: the project's README `persona:` if present, else the user's list.
    A `persona_primary` that is among them moves first (it leads a conflict); unset, the order stands."""
    ov = project_override(_vault(vault, home), slug) if slug else None
    names = ov if ov is not None else user_personas(home)
    first = primary(home)
    if first and first in names:
        names = [first] + [n for n in names if n != first]
    return names


def expand(names, pdir=None):
    """-> `names` with every inherited pack added: each name, then its parents depth-first in the order
    listed, once each (0.22.0, `extends`). A parent that is not a valid pack is skipped and a cycle is
    cut, so a stale pack never fails a hook; a name the caller listed is kept whether valid or not."""
    out = []

    def add(n, listed):
        if n in out:
            return
        if not listed:
            try:
                load_pack(n, pdir)
            except PersonaError:
                return
        out.append(n)
        try:
            parents = load_pack(n, pdir).get("extends") or []
        except PersonaError:
            return
        for par in parents:
            add(par, False)
    for n in names or []:
        add(n, True)
    return out


def plain_mode(names, pdir=None):
    """-> True when the personas ask for plain mode (0.22.0): the first pack in the expanded order that
    sets `plain_mode` decides, so a child can turn an inherited True off. No persona: False."""
    for p in _packs_quiet(names, pdir):
        if "plain_mode" in p:
            return bool(p["plain_mode"])
    return False


def handoff_text(names, pdir=None):
    """-> the install summary's persona hand-off (phase 95e), or "" when no listed persona has one.
    The first persona (own pack, then inherited) with a `handoff` leads: its three core commands, the
    demo tour and the walk-through, each on one line of at most 100 characters."""
    for p in _packs_quiet(names, pdir):
        h = p.get("handoff")
        if not h:
            continue
        does = _does()
        lines = ["Your first day as %s:" % p["title"].lower()]
        for c in h["commands"]:
            lines.append(_fit("  %-18s %s" % (c, does.get(c, "")), 100).rstrip())
        lines.append("  Tour it on a pretend project:   /gt-demo:gt-demo tour %s" % h["track"])
        lines.append("  Read it as a page, offline:     /gt-demo:gt-demo walkthrough %s" % h["track"])
        return "\n".join(lines) + "\n"
    return ""


def _packs_quiet(names, pdir=None):
    """-> the loaded packs for `names` and what they extend, in order, once each; an unknown or refused
    name is skipped. For the emphasis readers, which run inside hooks and must never fail on a stale
    setting."""
    out, done = [], []
    for n in expand(names, pdir):
        if n in done:
            continue
        done.append(n)
        try:
            out.append(load_pack(n, pdir))
        except PersonaError:
            continue
    return out


def _union(names, key, pdir=None):
    out = []
    for p in _packs_quiet(names, pdir):
        for v in p.get(key) or []:
            if v not in out:
                out.append(v)
    return out


def lotr_templates(names, pdir=None):
    """-> suggested LOTR template names: the union over the personas, first listed first."""
    return _union(names, "lotr_templates", pdir)


def featured_skills(names, pdir=None):
    """-> the featured gt commands: the union over the personas, first listed first."""
    return _union(names, "featured_skills", pdir)


def farm_tasks(names, pdir=None):
    """-> [task] the routine work these personas can hand to a local engine (0.21.4): the union in persona
    order, a task id listed once (the first persona to name it wins), each task carrying its `persona`."""
    out, seen = [], set()
    for p in _packs_quiet(names, pdir):
        for t in p.get("farm_tasks") or []:
            if t["id"] not in seen:
                seen.add(t["id"])
                out.append(dict(t, persona=p["name"]))
    return out


def farm_tasks_lines(tasks, engines=None):
    """-> the tasks as fixed-width lines (100 columns at most). `engines` is gt-farm's overview rows; when
    given, each task says what the calibrated engines scored for its class."""
    if not tasks:
        return []
    out = ["Work your local engines can take for you (a suggestion; nothing is sent until you ask):"]
    for t in tasks:
        out.append(_fit("  %s [%s]  %s" % (t["id"], t["class"], t["does"]), 100))
        out.append(_fit("      If it goes wrong: %s" % t["escalate"], 100))
        if engines is not None:
            got = ["%s: %s" % (e["engine"], e["kinds"].get(t["class"]) or "not measured")
                   for e in engines if e["state"] == "calibrated"]
            out.append(_fit("      Your engines: %s" % (", ".join(got) if got else
                        "none calibrated yet (python3 gt_farm_calibrate.py --status)"), 100))
    out.append("Ask Claude \"what can I farm out\" to hand one over (/gt-farm:gt-farm).")
    return out


def surface_order(names, pdir=None):
    """-> every SessionStart surface section, in the order to show them: the personas'
    session_focus sections first (first persona leads), then the rest in the default order.
    No persona, or none with a focus, gives exactly SURFACE_SECTIONS."""
    focus = [s for s in _union(names, "session_focus", pdir) if s in SURFACE_SECTIONS]
    return tuple(focus + [s for s in SURFACE_SECTIONS if s not in focus])


def close_plan(names, pdir=None):
    """-> what /gt:gt-close puts first: {"personas", "lead", "rest", "summary"}.
    lead: the personas' close_focus items, union in order (first persona leads); rest: every
    CLOSE_CAPTURE item not led, in gt-work's own order -- so each capture item appears exactly
    once and none is dropped. summary: the first persona's close_summary that is set, else
    "standard". An unknown or refused persona is skipped (this runs at session end, quietly)."""
    packs = _packs_quiet(names, pdir)
    lead = []
    for p in packs:
        for v in p.get("close_focus") or []:
            if v in CLOSE_ITEMS and v not in lead:
                lead.append(v)
    summary = next((p["close_summary"] for p in packs if p.get("close_summary")), "standard")
    return {"personas": [p["name"] for p in packs], "lead": lead,
            "rest": [c for c in CLOSE_CAPTURE if c not in lead], "summary": summary}


def close_plan_text(plan):
    """-> the plan as fixed-width lines (80 columns at most), or "" when there is nothing to
    change: no persona, or none sets close_focus or close_summary. Empty means gt-close runs
    exactly as it always has."""
    if not plan["lead"] and plan["summary"] == "standard":
        return ""
    out = [_fit("close plan for %s" % ", ".join(plan["personas"]))]
    if plan["lead"]:
        out.append("Put these first, in this order, in the write-back and the wrap-up:")
        for i, k in enumerate(plan["lead"], 1):
            label, lands = CLOSE_ITEMS[k]
            out.append(_fit("  %d. %-10s %s" % (i, k, label)))
            out.append(_fit("     %-10s -> %s" % ("", lands)))
    rest = ", ".join(plan["rest"]) or "(none: every item is led)"
    out.append("Then the rest, in gt-work's usual order:")
    words, line = rest.split(" "), "  "
    for w in words:                       # wrap the list itself; never cut an item name
        if len(line) + len(w) + 1 > LINE_MAX:
            out.append(line.rstrip())
            line = "  "
        line += w + " "
    out.append(line.rstrip())
    if plan["summary"] == "brief-first":
        out += ["Wrap-up: brief first. Open with at most five plain lines: what moved,",
                "what was decided, what is blocked or waiting on someone. Then all the",
                "detail below it, as usual."]
    out += ["Order and emphasis only: every gt-work step runs, everything it would",
            "capture is captured, and the Core rules apply as always."]
    return "\n".join(out) + "\n"


def audience_block(names, pdir=None):
    """-> the "Audience:" block for the agents that WRITE for the user (item #45), or "".
    One line per persona that has an `audience`, in the order given; the first leads. Presentation
    only -- the block says so, and gt_agent_spec.py sends it to writing stages and nothing else."""
    lines, done = [], []
    for n in expand(names, pdir):
        if n in done:
            continue
        done.append(n)
        a = (load_pack(n, pdir).get("audience") or "").strip()
        if a:
            lines.append("- %s" % a)
    if not lines:
        return ""
    return "\n".join(
        ["Audience:", "You are writing for the reader below. Several roles are listed in order; the "
         "first leads, so where they differ follow the first."] + lines +
        ["This changes how you present the result (order, plain words, how much detail), never what "
         "is recorded as true: every fact, citation and verification state stays exactly as the "
         "material gives it, and you add no claim to suit the reader. If the request says \"give me the "
         "details\", give the full detail for that answer."]) + "\n"


def resolve_scaffold(names, title, pdir=None):
    """-> {"files": [{path, content}], "sections": [{file, heading, body}]}, {TITLE} filled.
    Union over the personas in order; the first to name a file (or a file+heading) wins."""
    files, sections, seen_f, seen_s = [], [], set(), set()
    done = []
    for n in expand(names, pdir):
        if n in done:
            continue
        done.append(n)
        sc = load_pack(n, pdir)["scaffold"]
        for f in sc.get("files", []):
            if f["path"] not in seen_f:
                seen_f.add(f["path"])
                files.append({"path": f["path"], "content": f["content"].replace("{TITLE}", title or "")})
        for s in sc.get("sections", []):
            key = (s["file"], s["heading"].strip().lower())
            if key not in seen_s:
                seen_s.add(key)
                sections.append({"file": s["file"], "heading": s["heading"],
                                 "body": s["body"].replace("{TITLE}", title or "")})
    return {"files": files, "sections": sections}


def apply_scaffold(proj, scaffold):
    """Write a resolved scaffold into the project dir `proj`, additively: a file that exists is left
    alone, a section is appended only when its heading is absent. -> [("CREATED"|"UPDATED"|"SKIPPED", rel)]."""
    out = []
    for f in scaffold["files"]:
        dest = os.path.join(proj, *f["path"].split("/"))
        if os.path.exists(dest):
            out.append(("SKIPPED", f["path"]))
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f["content"])
        out.append(("CREATED", f["path"]))
    for s in scaffold["sections"]:
        dest = os.path.join(proj, s["file"])
        head = "## " + s["heading"].strip()
        try:
            with open(dest, encoding="utf-8", newline="") as fh:
                cur = fh.read()
        except OSError:
            out.append(("SKIPPED", s["file"]))
            continue
        if re.search(r"(?m)^%s\s*$" % re.escape(head), cur):
            out.append(("SKIPPED", s["file"] + " " + head))
            continue
        nl = "\r\n" if "\r\n" in cur else "\n"
        lead = "" if (cur == "" or cur.endswith("\n")) else "\n"
        add = lead + "\n" + head + "\n\n" + s["body"].rstrip("\n") + "\n"
        with open(dest, "a", encoding="utf-8", newline="") as fh:
            fh.write(add.replace("\n", nl))
        out.append(("UPDATED", s["file"] + " " + head))
    return out


LINE_MAX = 80


def _fit(line, width=LINE_MAX):
    """One fixed-width line that never wraps: cut at a word boundary with '...' (owner rule)."""
    if len(line) <= width:
        return line
    cut = line[:width - 3].rsplit(" ", 1)[0].rstrip(",;: ")
    return cut + "..."


def _does():
    """{command: what it does} from the skills' plain blocks (gt_hints), or {} when unavailable."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_hints
        out = {}
        for c, b in gt_hints.catalog().items():
            does = " ".join(str(b.get("does") or "").split())
            out.setdefault(c, does)
            out.setdefault("/gt:" + str(b.get("skill") or ""), does)   # /gt:gt-create task -> /gt:gt-create
        return out
    except Exception:                                    # noqa: BLE001 - the list is enough
        return {}


def _audience_setting():
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_settings
        v = gt_settings.get("persona_aligned")
        return v if v in ("on", "off") else "on"
    except Exception:                                    # noqa: BLE001 - the default is on
        return "on"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_persona.py", description=__doc__.split("\n")[0])
    ap.add_argument("--packs", default=None, help="persona pack directory (default: the release's packs/personas)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    rs = sub.add_parser("resolve")
    rs.add_argument("--project")
    rs.add_argument("--vault")
    rs.add_argument("--json", action="store_true")
    rs.add_argument("--title", default="")
    sub.add_parser("check")
    fe = sub.add_parser("featured")
    fe.add_argument("--project")
    fe.add_argument("--vault")
    fe.add_argument("--personas", help="comma list; default: resolve")
    fe.add_argument("--json", action="store_true")
    cp = sub.add_parser("close-plan")
    cp.add_argument("--project")
    cp.add_argument("--vault")
    cp.add_argument("--personas", help="comma list; default: resolve")
    cp.add_argument("--json", action="store_true")
    au = sub.add_parser("audience")
    au.add_argument("--project")
    au.add_argument("--vault")
    ft = sub.add_parser("farm-tasks")
    ft.add_argument("--project")
    ft.add_argument("--vault")
    ft.add_argument("--personas", help="comma list; default: resolve")
    ft.add_argument("--engines", action="store_true", help="add what each calibrated local engine scored")
    ft.add_argument("--json", action="store_true")
    ho = sub.add_parser("handoff")
    ho.add_argument("--project")
    ho.add_argument("--vault")
    ho.add_argument("--personas", help="comma list; default: resolve")
    ap_ = sub.add_parser("apply")
    ap_.add_argument("project_dir")
    ap_.add_argument("--project")
    ap_.add_argument("--vault")
    ap_.add_argument("--title", default="")
    ap_.add_argument("--personas", help="comma list; default: resolve")
    # --packs is accepted after the subcommand too
    for p in sub.choices.values():
        p.add_argument("--packs", dest="packs_sub", default=None)
    a = ap.parse_args(argv)
    pdir = a.packs_sub or a.packs
    try:
        if a.cmd == "list":
            for p in list_packs(pdir):
                print("%-18s %s" % (p["name"], p["title"]))
            for fn, prob in check_all(pdir):
                sys.stderr.write("gt_persona: refused %s: %s\n" % (fn, prob))
        elif a.cmd == "show":
            print(json.dumps(load_pack(a.name, pdir), indent=2))
        elif a.cmd == "check":
            probs = check_all(pdir)
            for fn, prob in probs:
                print("%s: %s" % (fn, prob))
            print("persona packs: %s (%d checked)" % ("FAIL" if probs else "ok", len(_pack_files(packs_dir(pdir)))))
            return 1 if probs else 0
        elif a.cmd == "handoff":
            # Prints nothing unless a persona has a hand-off: the installer's summary then adds it.
            names = _names(a.personas) if a.personas else resolve(a.vault, a.project)
            sys.stdout.write(handoff_text(names, pdir))
        elif a.cmd == "audience":
            # Empty output when persona_aligned is off or no persona has an audience line.
            if _audience_setting() == "on":
                sys.stdout.write(audience_block(resolve(a.vault, a.project), pdir))
        elif a.cmd == "close-plan":
            # Prints nothing with no persona (or none that sets close_focus / close_summary):
            # gt-close then runs exactly as it always has.
            names = _names(a.personas) if a.personas else resolve(a.vault, a.project)
            plan = close_plan(names, pdir)
            if a.json:
                print(json.dumps(plan, indent=2))
            else:
                sys.stdout.write(close_plan_text(plan))
        elif a.cmd == "farm-tasks":
            names = _names(a.personas) if a.personas else resolve(a.vault, a.project)
            tasks = farm_tasks(names, pdir)
            rows = None
            if a.engines:
                try:
                    import gt_farm_link
                    lib = gt_farm_link.farm_module()
                    rows = lib.overview(a.vault or _vault(None), None) if lib else None
                except Exception:                            # noqa: BLE001 - the list is enough
                    rows = None
            if a.json:
                print(json.dumps({"personas": names, "tasks": tasks,
                                  **({"engines": rows} if rows is not None else {})}, indent=2, default=str))
            elif not names:
                print("no persona set: gt_settings.py set persona a,b (gt_persona.py list)")
            elif not tasks:
                print("no farm tasks for: %s" % ", ".join(names))
            else:
                print("\n".join(farm_tasks_lines(tasks, rows)))
        elif a.cmd == "featured":
            names = _names(a.personas) if a.personas else resolve(a.vault, a.project)
            cmds = featured_skills(names, pdir)
            if a.json:
                print(json.dumps({"personas": names, "featured_skills": cmds}, indent=2))
            elif not names:
                print("no persona set: gt_settings.py set persona a,b (gt_persona.py list)")
            elif not cmds:
                print("no featured commands for: %s" % ", ".join(names))
            else:
                print(_fit("featured for %s:" % ", ".join(names)))
                does = _does()
                for c in cmds:
                    print(_fit("  %-22s %s" % (c, does.get(c, ""))).rstrip())
        elif a.cmd == "apply":
            names = _names(a.personas) if a.personas else resolve(a.vault, a.project)
            for kind, rel in apply_scaffold(a.project_dir, resolve_scaffold(names, a.title, pdir)):
                print("%s %s" % (kind, rel))
        else:
            names = resolve(a.vault, a.project)
            if a.json:
                print(json.dumps({"personas": names, "scaffold": resolve_scaffold(names, a.title, pdir)}, indent=2))
            else:
                for n in names:
                    print(n)
    except PersonaError as exc:
        sys.stderr.write("gt_persona: %s\n" % exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
