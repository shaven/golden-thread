#!/usr/bin/env python3
"""gt_farm_ladder.py -- the pure half of ladder mode (no network): the task-class table, the
checks that FILTER a reply, the audit state, and the per-class report. `gt_farm_send.py ladder`
does the sending; this module decides which rung to start at and whether a reply is good enough
to stop climbing. It is imported by gt_farm_send.py and has no command line of its own.

THE LADDER. Four rungs, cheapest first. A packet starts at its task class's rung and climbs only
when a check on the reply fails:

    1  a small local engine        (service-table column `rung` = 1)
    2  a big local engine          (rung = 2)
    3  a hosted engine, PUBLIC material only (rung = 3)
    4  Claude                      (never sent from here: `ladder` stops and says so)

A service table without a `rung` column keeps working: a row with `local: yes` is rung 2, any
other chat-completions row is rung 3. (These ladder rungs are not the transport "rung 0 / rung 1"
of the skill, where 0 is a person pasting a packet and 1 is the script sending it.)

THE TASK-CLASS TABLE (`farm-ladder.tsv`, tab-separated, one row per class):

    task_class  start_rung  check  audit_rate  public

`check` is one of the names in CHECKS. `audit_rate` is the share of ACCEPTED results marked for
a blind Claude audit ("-" or blank: the `farm_audit_rate` setting, default 0.2). `public` (optional
column, default no) says material of this class may be sent to rung 3 when the packet also clears
the Releasable gate. The built-in default is DEFAULT_TABLE; `<vault>/farm-ladder.tsv` or
`--ladder-table FILE` overrides it. A class the table does not name uses its `default` row.

CHECKS FILTER, THEY DO NOT FAIL. A check gets the packet and the parsed reply and returns the
reply with what is wrong REMOVED, plus a verdict on what is left: the quote check drops quotes
that are not in the material and passes when at least three remain; the log check drops problems
whose cited lines do not exist. An empty answer is a failure unless the packet says results may be
empty. Every check ships with a test against known-good and known-bad output
(tests/test_farm_ladder.py).

THE AUDIT LOOP. A sampled accepted result carries `audit: pending` in the savings ledger. A
Claude subagent grades it blind and `gt_farm_send.py audit-result KEY pass|fail` records the
verdict and moves the class's effective start rung: two failed audits raise it by one, five clean
audits in a row lower it by one, never below the table's start rung minus one, never above 4, and
never onto rung 3 for a class that is not `public`. The state is a small JSON file beside the
ladder run log, under ~/.claude/golden-thread/farm/.
"""
import datetime
import json
import os
import re

CHECK_NAMES = ("quote", "log-lines", "schema")
DEFAULT_AUDIT_RATE = 0.2
FAILS_TO_RAISE = 2
PASSES_TO_LOWER = 5
MAX_RUNG = 4

DEFAULT_TABLE = (
    "task_class\tstart_rung\tcheck\taudit_rate\tpublic\n"
    "log-triage\t1\tlog-lines\t-\tno\n"
    "quote-extract\t2\tquote\t-\tno\n"
    "extract\t2\tschema\t-\tno\n"
    "classify\t2\tschema\t-\tno\n"
    "draft\t2\tschema\t-\tno\n"
    "generalize\t4\tschema\t-\tno\n"
    "default\t4\tschema\t-\tno\n")


def farm_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm")


def state_path():
    return os.path.join(farm_dir(), "ladder-state.json")


def runs_path():
    return os.path.join(farm_dir(), "ladder-runs.jsonl")


def _truthy(v):
    return str(v or "").strip().lower() in ("yes", "y", "true", "on", "1")


# ---- rungs and the task-class table -----------------------------------------------------

def rung_of(value, local):
    """The rung a service-table row sits on: its `rung` cell when it is 1-3, else the default
    for tables written before the column existed (local engine -> 2, anything else -> 3).
    4 is Claude and is never a row."""
    try:
        r = int(str(value).strip())
        if 1 <= r <= 3:
            return r
    except (TypeError, ValueError):
        pass
    return 2 if local else 3


def parse_table(text):
    """-> {task_class: row}. Raises ValueError listing every bad row."""
    rows, problems, header = {}, [], None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if header is None:
            header = [re.sub(r"[\s-]+", "_", c.lower()) for c in cells]
            if "task_class" not in header:
                problems.append("line %d: the first row must be the header (task_class, "
                                "start_rung, check, audit_rate[, public])" % n)
                break
            continue
        d = dict(zip(header, cells))
        cls = d.get("task_class", "").lower()
        if not cls:
            problems.append("line %d: no task_class" % n)
            continue
        try:
            start = int(d.get("start_rung", ""))
            if not 1 <= start <= MAX_RUNG:
                raise ValueError
        except ValueError:
            problems.append("line %d (%s): start_rung must be 1-4, got %r"
                            % (n, cls, d.get("start_rung", "")))
            continue
        check = d.get("check", "").lower() or "schema"
        if check not in CHECK_NAMES:
            problems.append("line %d (%s): unknown check %r (known: %s)"
                            % (n, cls, check, ", ".join(CHECK_NAMES)))
            continue
        rate = None
        raw = d.get("audit_rate", "")
        if raw not in ("", "-"):
            try:
                rate = float(raw)
                if not 0.0 <= rate <= 1.0:
                    raise ValueError
            except ValueError:
                problems.append("line %d (%s): audit_rate must be 0-1 or '-', got %r" % (n, cls, raw))
                continue
        rows[cls] = {"task_class": cls, "start_rung": start, "check": check, "audit_rate": rate,
                     "public": _truthy(d.get("public"))}
    if header is None:
        problems.append("the table is empty")
    if problems:
        raise ValueError("; ".join(problems))
    return rows


def load_table(explicit, vault):
    """-> (rows, where). --ladder-table FILE, else <vault>/farm-ladder.tsv, else the built-in."""
    path = None
    if explicit:
        path = os.path.expanduser(explicit)
    elif vault and os.path.isfile(os.path.join(vault, "farm-ladder.tsv")):
        path = os.path.join(vault, "farm-ladder.tsv")
    if path is None:
        return parse_table(DEFAULT_TABLE), "built-in default"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return parse_table(text), path


def class_row(table, cls):
    """-> (row, used_default). A class the table does not name takes its `default` row."""
    key = (cls or "").strip().lower()
    if key in table:
        return table[key], False
    d = table.get("default") or {"task_class": "default", "start_rung": 4, "check": "schema",
                                 "audit_rate": None, "public": False}
    return dict(d, task_class=key or "default"), True


# ---- audit state ------------------------------------------------------------------------

def load_state():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(farm_dir(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, state_path())


def effective_rung(table_start, offset, public):
    """The rung a class starts at once the audit offset is applied. Held to [table_start - 1, 4]
    and never 3 for a class that is not `public` once an offset has moved it (a non-public class
    skips from 2 straight to 4, and does not step down from 4 onto 3)."""
    lo = max(1, table_start - 1)
    e = min(MAX_RUNG, max(lo, table_start + offset))
    if e == 3 and not public and offset != 0:
        e = 4
    return e


def class_offset(state, cls):
    try:
        return int((state.get(cls) or {}).get("offset", 0))
    except (TypeError, ValueError):
        return 0


def audit_update(state, cls, row, verdict):
    """Apply one audit verdict to `state` in place. -> (before, after) effective start rung."""
    s = state.setdefault(cls, {})
    for k in ("offset", "fails", "streak", "audits", "passes"):
        s.setdefault(k, 0)
    start, public = row["start_rung"], row["public"]
    before = effective_rung(start, s["offset"], public)
    s["audits"] += 1
    if verdict == "fail":
        s["streak"] = 0
        s["fails"] += 1
        if s["fails"] >= FAILS_TO_RAISE:
            s["fails"] = 0
            cand = s["offset"] + 1
            if effective_rung(start, cand, public) > before:
                s["offset"] = cand
    else:
        s["passes"] += 1
        s["streak"] += 1
        if s["streak"] >= PASSES_TO_LOWER:
            s["streak"] = 0
            cand = s["offset"] - 1
            if effective_rung(start, cand, public) < before:
                s["offset"] = cand
                s["fails"] = 0
    return before, effective_rung(start, s["offset"], public)


# ---- the run log (one line per ladder run, for the escalation rate) -----------------------

def append_run(rec):
    os.makedirs(farm_dir(), exist_ok=True)
    with open(runs_path(), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")


def read_runs(days):
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    try:
        with open(runs_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and str(r.get("ts", ""))[:10] >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def class_report(farm_lines, runs, state, table):
    """-> {task_class: figures}: runs, escalated past the start rung, sent on to Claude, audits
    graded, audits passed, and the effective start rung now."""
    rep = {}

    def slot(c):
        return rep.setdefault(c, {"runs": 0, "escalated": 0, "to_claude": 0, "audits": 0,
                                  "audit_pass": 0, "pending": 0})
    for r in runs:
        s = slot(str(r.get("task_class") or "default"))
        s["runs"] += 1
        if int(r.get("end", 0) or 0) > int(r.get("start", 0) or 0):
            s["escalated"] += 1
        if int(r.get("end", 0) or 0) >= MAX_RUNG:
            s["to_claude"] += 1
    for e in farm_lines:
        if not e.get("ladder") or not e.get("task_class"):
            continue
        a = e.get("audit")
        if a not in ("pending", "pass", "fail"):
            continue
        s = slot(str(e["task_class"]))
        if a == "pending":
            s["pending"] += 1
        else:
            s["audits"] += 1
            s["audit_pass"] += 1 if a == "pass" else 0
    for c, s in rep.items():
        s["escalation_rate"] = round(s["escalated"] / s["runs"], 3) if s["runs"] else None
        s["audit_pass_rate"] = round(s["audit_pass"] / s["audits"], 3) if s["audits"] else None
        row, _ = class_row(table, c)
        s["table_start"] = row["start_rung"]
        s["effective_start"] = effective_rung(row["start_rung"], class_offset(state, c), row["public"])
    return rep


# ---- reading a reply ---------------------------------------------------------------------

def parse_text_contract(t):
    """A FINDINGS:/GAPS: text block -> the same dict the JSON contract gives, or None."""
    m = re.search(r"^\s*GAPS:", t, re.M)
    if not (re.search(r"^\s*FINDINGS:", t, re.M) and m):
        return None
    head, tail = t[:m.start()], t[m.end():]
    findings, cur = [], None
    for line in head.splitlines():
        mm = re.match(r"\s*-?\s*(claim|source|confidence):\s*(.*)$", line)
        if not mm:
            continue
        k, v = mm.group(1), mm.group(2).strip()
        if k == "claim":
            cur = {"claim": v, "source": "NONE", "confidence": "low"}
            findings.append(cur)
        elif cur is not None:
            cur[k] = v
    gaps = [g.strip() for g in re.findall(r"^\s*-\s*(.+)$", tail, re.M) if g.strip()]
    return {"findings": findings, "gaps": gaps}


def parse_reply(text):
    """-> (obj, how) or (None, why). The reply as a dict: JSON (fenced or not), else the
    FINDINGS:/GAPS: text contract."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n|\n```$", "", t).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj, "JSON"
        return None, "the reply is JSON but not an object"
    except ValueError:
        pass
    s, e = t.find("{"), t.rfind("}")
    if 0 <= s < e:
        try:
            obj = json.loads(t[s:e + 1])
            if isinstance(obj, dict):
                return obj, "JSON (extracted from prose)"
        except ValueError:
            pass
    obj = parse_text_contract(t)
    if obj is not None:
        return obj, "text contract"
    return None, "the reply is neither a JSON object nor a FINDINGS:/GAPS: block"


# ---- the packet --------------------------------------------------------------------------

def inputs_text(body):
    """The material: everything between `### INPUTS` and `### RETURN CONTRACT`."""
    i = body.find("### INPUTS")
    if i == -1:
        return ""
    j = body.find("### RETURN CONTRACT", i)
    return body[i + len("### INPUTS"):j if j != -1 else len(body)].strip("\n")


MAY_BE_EMPTY = re.compile(
    r"(may|can|could|might) (legitimately |also )?(be|come back|return|come out) (empty|none|nothing)"
    r"|(empty|no) (list|result|results|answer|answers|findings) (is|are) (fine|ok|okay|acceptable|valid|allowed|expected)"
    r"|(return|give|answer with|reply with) (an )?empty (list|result|array|answer)"
    r"|if (there is |there are )?(nothing|no [a-z ]{1,40}|none)[^.\n]{0,60}(return|reply|answer|say)[^.\n]{0,30}(empty|none|nothing)"
    r"|results? may be empty|zero results (is|are) (fine|ok|acceptable)", re.I)


def may_be_empty(body):
    return bool(MAY_BE_EMPTY.search(body or ""))


# ---- the checks ---------------------------------------------------------------------------
# A check is  f(body, obj) -> (ok, why, obj_filtered, dropped).  It never mutates `obj`.

_QUOTE_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                             " ": " "})


def _norm(s):
    return re.sub(r"\s+", " ", str(s).translate(_QUOTE_FOLD)).strip()


def _unwrap_quote(q):
    """A model wraps a quote in quotation marks, a leading `Quote:` label or an ellipsis; none of
    those is part of the passage."""
    q = _norm(q)
    q = re.sub(r"^(quote|passage)\s*:\s*", "", q, flags=re.I)
    q = q.strip().strip("\"'`").strip()
    q = re.sub(r"^(\.\.\.|…)\s*|\s*(\.\.\.|…)$", "", q)
    return q


def _copy(obj):
    return json.loads(json.dumps(obj))


def _items_key(obj, names):
    for k in names:
        if isinstance(obj.get(k), list):
            return k
    return None


def _note_gap(out, text):
    gaps = out.get("gaps")
    if isinstance(gaps, list):
        gaps.append(text)
    elif "gaps" not in out:
        out["gaps"] = [text]


def _quote_threshold(body):
    m = re.search(r"\b(?:up to|at most|no more than|exactly|only)\s+(\d+)\s+(?:verbatim\s+)?"
                  r"(?:quotes?|passages?|quotations?)", body or "", re.I)
    if m:
        return max(1, min(3, int(m.group(1))))
    return 3


def check_quote(body, obj):
    """Drop every quote that is not in the material (whitespace and curly quotes normalised);
    pass if at least three remain (fewer when the packet asks for fewer)."""
    key = _items_key(obj, ("quotes", "findings"))
    if key is None:
        return False, "the reply has no quotes[] or findings[] list", obj, 0
    material = _norm(inputs_text(body) or body)
    out, kept, dropped = _copy(obj), [], []
    for it in obj[key]:
        raw = it.get("quote", it.get("claim", it.get("text", ""))) if isinstance(it, dict) else it
        q = _unwrap_quote(raw)
        (kept if q and q in material else dropped).append(it)
    out[key] = kept
    need = _quote_threshold(body)
    if dropped:
        _note_gap(out, "%d of %d quotes were dropped: not verbatim in the inputs"
                  % (len(dropped), len(dropped) + len(kept)))
    if len(kept) >= need:
        return True, "%d quote%s verbatim%s" % (len(kept), "" if len(kept) == 1 else "s",
                                               (", %d dropped" % len(dropped)) if dropped else ""), \
            out, len(dropped)
    if not obj[key] and may_be_empty(body):
        return True, "no quotes, and the packet says results may be empty", out, 0
    return False, "%d of %d quotes verbatim (need %d)" % (len(kept), len(obj[key]), need), out, len(dropped)


_LINE_REF = re.compile(r"\b(?:lines?|ln|l)\.?\s*[:#]?\s*((?:\d+(?:\s*[-–]\s*\d+)?)"
                       r"(?:\s*(?:,|;|and|&)\s*(?:\d+(?:\s*[-–]\s*\d+)?))*)", re.I)
ERRORISH = re.compile(r"error|fail|exception|traceback|fatal|denied|refused|timed? ?out|abort|"
                      r"panic|cannot|could not|unable", re.I)


def parse_line_refs(text):
    """Line numbers named in free text: 'line 12', 'lines 3-5, 9', 'L40'."""
    out = []
    for m in _LINE_REF.finditer(str(text or "")):
        for part in re.split(r"\s*(?:,|;|and|&)\s*", m.group(1)):
            r = re.split(r"\s*[-–]\s*", part.strip())
            try:
                if len(r) == 2:
                    a, b = int(r[0]), int(r[1])
                    out.extend(range(a, min(b, a + 2000) + 1))
                elif r and r[0]:
                    out.append(int(r[0]))
            except ValueError:
                continue
    return out


def _log_lines_of(material):
    """-> the set of line numbers the material has. Numbered lines ('12<TAB>text', '12: text',
    '12| text') name themselves; otherwise 1..N over the non-blank lines."""
    lines = [l for l in material.splitlines() if l.strip()]
    nums = [int(m.group(1)) for l in lines for m in [re.match(r"\s*(\d+)\s*[\t:|]", l)] if m]
    if nums and len(nums) >= max(1, len(lines) // 2):
        return set(nums)
    return set(range(1, len(lines) + 1))


def check_log_lines(body, obj):
    """Verify cited line numbers exist in the log: drop a cited line that does not, drop a problem
    that is left with none. Pass if a problem remains, or the log shows nothing error-like, or the
    packet says an empty answer is fine."""
    key = _items_key(obj, ("problems", "findings"))
    if key is None:
        return False, "the reply has no problems[] or findings[] list", obj, 0
    material = inputs_text(body) or body
    valid = _log_lines_of(material)
    out, kept, dropped = _copy(obj), [], 0
    for it in obj[key]:
        if not isinstance(it, dict):
            dropped += 1
            continue
        refs = []
        for x in (it.get("lines") or []) if isinstance(it.get("lines"), list) else []:
            try:
                refs.append(int(x))
            except (TypeError, ValueError):
                pass
        refs += parse_line_refs(it.get("source")) + parse_line_refs(it.get("claim"))
        good = sorted({r for r in refs if r in valid})
        if not good:
            dropped += 1
            continue
        it = dict(it)
        if isinstance(it.get("lines"), list):
            it["lines"] = good
        kept.append(it)
    out[key] = kept
    if dropped:
        _note_gap(out, "%d of %d problems were dropped: they cite no line that exists in the log"
                  % (dropped, dropped + len(kept)))
    if kept:
        return True, "%d problem%s, every cited line exists%s" % (
            len(kept), "" if len(kept) == 1 else "s", (", %d dropped" % dropped) if dropped else ""), \
            out, dropped
    if not obj[key]:
        if may_be_empty(body):
            return True, "no problems, and the packet says results may be empty", out, 0
        if not any(ERRORISH.search(l) for l in material.splitlines()):
            return True, "no problems, and the log shows nothing error-like", out, 0
        return False, "empty answer for a log that has error-like lines", out, 0
    return False, "all %d problems cite lines that do not exist" % dropped, out, dropped


def check_schema(body, obj):
    """The generic contract check: the reply honours the FINDINGS/GAPS contract, no finding has an
    empty claim, and an empty findings list is a failure unless the packet allows it."""
    if not isinstance(obj.get("findings"), list) or not isinstance(obj.get("gaps"), list):
        return False, "the reply does not carry findings[] and gaps[]", obj, 0
    out, kept, dropped = _copy(obj), [], 0
    for f in obj["findings"]:
        if (isinstance(f, dict) and str(f.get("claim") or "").strip()
                and all(k in f for k in ("claim", "source", "confidence"))):
            kept.append(f)
        else:
            dropped += 1
    out["findings"] = kept
    if dropped:
        _note_gap(out, "%d malformed findings were dropped (no claim, source or confidence)" % dropped)
    if kept:
        return True, "%d finding%s, contract honoured%s" % (
            len(kept), "" if len(kept) == 1 else "s", (", %d dropped" % dropped) if dropped else ""), \
            out, dropped
    if not obj["findings"] and may_be_empty(body):
        return True, "no findings, and the packet says results may be empty", out, 0
    return False, ("empty answer: no findings, and the packet does not say results may be empty"
                   if not obj["findings"] else "every finding was malformed"), out, dropped


CHECKS = {"quote": check_quote, "log-lines": check_log_lines, "schema": check_schema}


def run_check(name, body, obj):
    """-> (ok, why, filtered_obj, dropped)."""
    return CHECKS[name](body, obj)
