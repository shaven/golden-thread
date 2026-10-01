#!/usr/bin/env python3
"""gt_intake_scan.py -- scan material BEFORE anything reads it for ingest.

    gt_intake_scan.py <path> [--unit U] [--unit-depth N] [--vault V] [--json]

The owner's rule (2026-09-30): every ingested project is scanned "right out the gate", before
any agent -- or the session itself -- opens it. Three kinds of finding, each a stop condition
in gt-ingest:

  credential        a credential in the tree. Found by gt_secrets.py, run as ITS OWN PROCESS
                    (never imported here: two output rules in one process is how 0.16.0 leaked).
  unsafe-code       a dangerous construct in source code: a download piped into a shell,
                    eval/exec of fetched or decoded data, an obfuscated blob beside an exec,
                    a destructive filesystem command, an install hook that fetches and runs,
                    a reverse shell, bidi control characters in code.
  prompt-injection  text addressed to the model that will read it: telling the reader to set
                    aside its earlier instructions, reassigning its role, fake chat/role
                    markers, instruction-bearing HTML comments, hidden Unicode tag characters
                    and zero-width runs.

THE OUTPUT CONTRACT. A finding is kind + file + line + rule id, and nothing else. The matched
text is NEVER printed: a credential must not enter a session, and hostile text must not be fed
back to the model the scan exists to protect. File names are printed with control and format
characters stripped, and a file NAME that itself matches an injection rule is withheld.

THE UNIT. Findings are grouped per unit, and gt-ingest stops per unit. By default a unit is one
top-level folder of the tree, plus the root-level files as the unit `.`. `--unit-depth 2` makes
`packages/foo` a unit (for a monorepo); `--unit U` scans that one unit only.

WHAT IS NOT SCANNED, AND SAID. Noise directories (.git, node_modules, caches) are skipped and
named in the output: nothing in them may be read for ingest. Binary files are counted. A file
this scan cannot read, a document format it cannot open (pdf, docx, ...), a file over the size
cap, or a credential scan that could not run makes the unit INCOMPLETE -- never clean.

Read-only: nothing here writes the scanned tree, a vault or a temp file.

Exit: 0 every unit clean | 1 findings (in any unit) | 2 usage | 3 nothing found BUT part of the
scan could not run (a scan that cannot run is not a pass). 1 outranks 3: either way it stops.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
CLEAN, FOUND, USAGE, CANNOT = 0, 1, 2, 3

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".DS_Store"}
SIZE_CAP = 8 * 1024 * 1024
MAX_FILES = 50000
SECRETS_TIMEOUT = 600
# Formats an agent can read (a PDF, a Word file) that a stdlib line scan cannot open. Their
# content is unscanned, so the unit is incomplete rather than clean.
OPAQUE_DOCS = {".pdf", ".docx", ".doc", ".odt", ".pptx", ".ppt", ".xlsx", ".xls", ".epub",
               ".pages", ".key", ".numbers"}

CODE_EXT = {".py", ".pyw", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".java",
            ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
            ".scala", ".sh", ".bash", ".zsh", ".fish", ".ksh", ".ps1", ".psm1", ".psd1", ".bat",
            ".cmd", ".vbs", ".lua", ".pl", ".pm", ".r", ".dart", ".ex", ".exs", ".clj", ".hs",
            ".sql", ".vue", ".svelte", ".groovy", ".gradle", ".mk", ".tf", ".nix", ".applescript"}
CODE_NAMES = {"makefile", "gnumakefile", "dockerfile", "containerfile", "jenkinsfile",
              "vagrantfile", "rakefile", "gemfile", "justfile", "pkgbuild", "package.json",
              ".gitlab-ci.yml", "tasks.json", "procfile"}


def _rx(pattern, flags=re.I):
    return re.compile(pattern, flags)


# -- prompt injection: every text file, code included (comments and strings are read too) ------
INJECT_RULES = [
    ("inject.ignore-instructions", _rx(
        r"\b(?:ignore|disregard|forget|override|bypass)\s+"
        r"(?:(?:all|any|the|your|of|these|those|my)\s+)*"
        r"(?:previous|prior|above|earlier|preceding|foregoing|original|system|existing)\s+"
        r"(?:instructions?|prompts?|directions?|directives?|rules|guidelines|context|"
        r"messages?|commands?)\b")),
    ("inject.role-reassignment", _rx(
        r"\byou\s+are\s+now\s+(?:a|an|the|in|my|no\s+longer|free)\b"
        r"|\bfrom\s+now\s+on,?\s+you\s+(?:are|will|must|should)\b"
        r"|\byour\s+(?:new|real|true|actual)\s+(?:instructions?|task|goal|objective|purpose|"
        r"role)\b"
        r"|\bnew\s+(?:system\s+)?instructions?\s*:")),
    ("inject.addressed-to-ai", _rx(
        r"\b(?:note|message|instructions?)\s+(?:to|for)\s+(?:any\s+|the\s+|all\s+)?"
        r"(?:ai|llm|large\s+language\s+model|language\s+model|ai\s+assistant|ai\s+agent|"
        r"chatbot)s?\b"
        r"|\b(?:if|when)\s+you\s+are\s+(?:an?\s+)?(?:ai|llm|large\s+language\s+model|"
        r"language\s+model|ai\s+assistant|ai\s+agent|automated\s+agent|chatbot)\b"
        r"(?!\s+(?:developer|engineer|researcher|team|company|startup|product))"
        r"|\b(?:any|the)\s+(?:ai|llm|ai\s+agent|ai\s+assistant|language\s+model)\s+"
        r"(?:reading|processing|summari[sz]ing|ingesting|parsing|analy[sz]ing)\s+this\b"
        r"|\b(?:dear|hey|attention)\s+(?:ai|llm|ai\s+assistant|ai\s+agent|claude|chatgpt|gpt)\b")),
    ("inject.fake-role-marker", _rx(
        r"<\|\s*(?:im_start|im_end|system|user|assistant|endoftext)\s*\|>"
        r"|\[/?INST\]|<</?SYS>>"
        r"|</?(?:system[-_]prompt|system[-_]reminder)>|</(?:system|assistant|human)>"
        r"|^\s*(?:Human|Assistant)\s*:\s", re.M)),
]
COMMENT_RE = re.compile(r"<!--(.{0,8000}?)-->", re.S)
# An HTML comment is invisible when rendered, which is what makes it a carrier. It counts when
# it holds an injection phrase, or names an AI reader AND tells it what to do. "agent", "model"
# and "Claude Code"/"CLAUDE.md" are deliberately not addressees: gt's own Tasks template comment
# ("... do not edit that file by hand") would otherwise stop every gt vault's ingest.
COMMENT_ADDRESSEE = _rx(r"\b(?:ai|llm|ai\s+assistant|chatgpt|gpt|language\s+model|"
                        r"claude(?!\.md|\s+code))s?\b")
COMMENT_IMPERATIVE = _rx(r"\b(?:ignore|disregard|you\s+must|you\s+should|you\s+will|"
                         r"you\s+are|pretend|act\s+as|reveal|instead\s+of|"
                         r"(?:do\s+not|don't)\s+(?:tell|mention|reveal|inform|warn|show)|"
                         r"without\s+(?:telling|mentioning))\b")
TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
ZERO_WIDTH_RUN = re.compile("[\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\u180e\ufeff]{3,}")
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")

# -- unsafe code: code files only (a README telling a human what to type is not source) ------
_SH = r"(?:ba|z|k|da|fi)?sh"
_FETCH = (r"(?:urlopen|urlretrieve|requests\.(?:get|post)|httpx\.|urllib|http\.client|"
          r"fetch\s*\(|axios|XMLHttpRequest|DownloadString|Invoke-WebRequest|iwr|curl|wget)")
_DECODE = (r"(?:b64decode|base64|atob|unhexlify|fromCharCode|decompress|marshal\.loads|"
           r"codecs\.decode|bytes\.fromhex|rot13)")
CODE_RULES = [
    ("code.pipe-download-to-shell", _rx(
        r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^|\n]*\|\s*"
        r"(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+)?(?:" + _SH + r"|python[0-9.]*|perl|ruby|node|php|"
        r"iex|invoke-expression)\b")),
    ("code.shell-runs-download", _rx(
        r"\b" + _SH + r"\s+(?:-\w+\s+)*<\(\s*(?:curl|wget)\b"
        r"|\b" + _SH + r"\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b"
        r"|\b(?:source|\.)\s+<\(\s*(?:curl|wget)\b"
        r"|\beval\s+[\"']?\$\(\s*(?:curl|wget)\b")),
    ("code.exec-of-fetched", _rx(
        r"\b(?:eval|exec|execfile|Function)\s*\([^\n]*\b" + _FETCH + r"\b"
        r"|\b(?:iex|invoke-expression)\b[^\n]*\b" + _FETCH + r"\b"
        r"|\b" + _FETCH + r"\b[^\n]*\|\s*(?:iex|invoke-expression)\b")),
    ("code.exec-of-decoded", _rx(
        r"\b(?:eval|exec|Function)\s*\([^\n]*\b" + _DECODE + r"\b"
        r"|\bbase64\s+(?:-d|--decode|-D)\b[^\n]*\|\s*(?:sudo\s+)?" + _SH + r"\b"
        r"|\b(?:iex|invoke-expression)\b[^\n]*FromBase64Str(?:ing)")),
    ("code.destructive-fs", _rx(
        r"\brm\s+(?:-[-\w]+\s+)*-[a-zA-Z]*(?:r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+"
        r"(?:-[-\w]+\s+)*[\"']?(?:/|/\*|~|~/|~/\*|\$HOME|\$\{HOME\}|\$HOME/\*)[\"']?"
        r"(?=\s|$|;|&|\|)"
        r"|\brm\s+[^\n]*--no-preserve-root"
        r"|\bmkfs(?:\.\w+)?\s+[^\n]*/dev/"
        r"|\bdd\s+[^\n]*\bof=/dev/(?:sd|hd|nvme|disk|rdisk|mmcblk)"
        r"|:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"
        r"|\bshutil\.rmtree\(\s*(?:[\"']/[\"']|[\"']~[\"'/]|os\.path\.expanduser\(\s*[\"']~"
        r"[\"']\s*\)|Path\.home\(\)\s*\))"
        r"|\bchmod\s+-R\s+0?777\s+/(?=\s|$)"
        r"|\bdiskutil\s+erase(?:Disk|Volume)\b"
        r"|\bRemove-Item\b[^\n]*-Recurse[^\n]*\s(?:C:\\\\?|\$env:USERPROFILE|~)[\"']?(?=\s|$)")),
    ("code.reverse-shell", _rx(
        r"/dev/tcp/[\w.\-]+/\d+"
        r"|\b(?:nc|ncat|netcat)\b[^\n]*\s-[ec]\s+/bin/" + _SH + r"\b"
        r"|\b" + _SH + r"\s+-i\s+>&"
        r"|\bpty\.spawn\(\s*[\"']/bin/" + _SH)),
]
INSTALL_HOOK = _rx(r"\"(?:pre|post)?install\"\s*:\s*\"[^\"\n]*(?:curl|wget|https?://|node\s+-e|"
                   r"powershell|invoke-webrequest)")
BLOB = re.compile(r"(?<![\w:;,])[\"'`](?:[A-Za-z0-9+/]{200,}={0,2})[\"'`]"
                  r"|(?:\\x[0-9a-fA-F]{2}){60,}")
DATA_URI = re.compile(r"data:[\w/+.\-]+;base64,")
EXECUTES = _rx(r"\b(?:eval|exec|Function|marshal\.loads|iex|invoke-expression)\b"
               r"|\|\s*" + _SH + r"\b")

KINDS = ("credential", "unsafe-code", "prompt-injection")
RULE_COUNT = {"unsafe-code": len(CODE_RULES) + 3,           # + install hook, blob, bidi
              "prompt-injection": len(INJECT_RULES) + 4}    # + comment, tags, zero-width, bidi


# -- helpers ----------------------------------------------------------------------------------
def clean_name(s, limit=160):
    """A path fit for a terminal: no control or format characters (zero-width, bidi, tags)."""
    s = "".join(ch for ch in str(s) if unicodedata.category(ch)[0] != "C")
    return s if len(s) <= limit else s[:limit - 3] + "..."


def display(rel):
    """The path to print. A file NAME that is itself injection text is withheld."""
    for rule, rx in INJECT_RULES:
        if rx.search(rel.replace(os.sep, " ").replace("_", " ").replace("-", " ")):
            parent = os.path.dirname(rel)
            return clean_name(os.path.join(parent, "[name withheld: %s]" % rule))
    if TAG_CHARS.search(rel) or ZERO_WIDTH_RUN.search(rel) or BIDI.search(rel):
        return clean_name(os.path.join(os.path.dirname(rel), "[name withheld: hidden chars]"))
    return clean_name(rel)


def unit_of(rel, depth):
    parts = rel.replace("\\", "/").split("/")[:-1]
    return "/".join(parts[:depth]) or "."


def is_code(rel, head):
    name = os.path.basename(rel).lower()
    ext = os.path.splitext(name)[1]
    norm = rel.replace("\\", "/").lower()
    if ext in CODE_EXT or name in CODE_NAMES or name.endswith(".mk"):
        return True
    if "/.github/workflows/" in "/" + norm and ext in (".yml", ".yaml"):
        return True
    return head.startswith(b"#!")


def line_at(text, offset):
    return text.count("\n", 0, offset) + 1


def scan_text(rel, text, code):
    """-> [(kind, line, rule)]. Positions only; the matched text never leaves this function."""
    hits = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, rx in INJECT_RULES:
            if rx.search(line):
                hits.add(("prompt-injection", lineno, rule))
        if TAG_CHARS.search(line):
            hits.add(("prompt-injection", lineno, "inject.unicode-tags"))
        if ZERO_WIDTH_RUN.search(line):
            hits.add(("prompt-injection", lineno, "inject.zero-width-run"))
        if BIDI.search(line):
            hits.add(("unsafe-code", lineno, "code.bidi-control") if code else
                     ("prompt-injection", lineno, "inject.bidi-control"))
        if not code:
            continue
        for rule, rx in CODE_RULES:
            if rx.search(line):
                hits.add(("unsafe-code", lineno, rule))
        if os.path.basename(rel).lower() == "package.json" and INSTALL_HOOK.search(line):
            hits.add(("unsafe-code", lineno, "code.install-hook-fetches"))
    for m in COMMENT_RE.finditer(text):
        body = m.group(1)
        if (COMMENT_ADDRESSEE.search(body) and COMMENT_IMPERATIVE.search(body)) or \
                any(rx.search(body) for _, rx in INJECT_RULES):
            hits.add(("prompt-injection", line_at(text, m.start()), "inject.html-comment"))
    if code and EXECUTES.search(text):
        for m in BLOB.finditer(text):
            if DATA_URI.search(text[max(0, m.start() - 64):m.start() + 1]):
                continue
            hits.add(("unsafe-code", line_at(text, m.start()), "code.obfuscated-blob"))
    return sorted(hits, key=lambda h: (h[1], h[2]))


def walk(root, only_unit, depth, state):
    """Yield (rel, abs) for every file, skipping noise dirs (named in state)."""
    if os.path.isfile(root):
        yield os.path.basename(root), root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS:
                state["skipped_dirs"].add(d)
            elif os.path.islink(full):
                state["links"].append(os.path.relpath(full, root))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if only_unit is not None and unit_of(rel, depth) != only_unit:
                continue
            if fn in SKIP_DIRS:
                continue
            if os.path.islink(full):
                state["links"].append(rel)
                continue
            state["walked"] += 1
            if state["walked"] > MAX_FILES:
                state["truncated"] = True
                return
            yield rel, full


# -- the credential scan: gt_secrets in its OWN process, through its OWN writer ---------------
SECRETS_WORKER = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
import gt_secrets as s
req = json.load(sys.stdin)
root = Path(req["root"])
rules, problems, _ = s.rules_from_slot(req.get("vault"))
out = s.Out(True)
for where, what in problems:
    out.note("PROBLEM %s: %s" % (where, what))
if not rules:
    sys.exit(out.fatal("gt-secrets: CANNOT RUN - the secrets slot resolved to 0 pattern(s)"))
n = {"scanned": 0, "skipped": 0, "binary": 0, "unreadable": 0}
bad = []
for rel in req["files"]:
    p = root / rel if rel != "" else root
    ok, why = s.in_scope(p, rel)
    if not ok:
        n["skipped"] += 1
        continue
    r = s.scan_file(p, rel, rules, out)
    if r == s.UNREADABLE:
        n["unreadable"] += 1
        bad.append(rel)
    elif r == s.BINARY:
        n["binary"] += 1
    else:
        n["scanned"] += 1
summary = dict(n, rules=len(rules), unreadable_files=bad,
               line="gt-secrets: %d file(s) scanned, %d rule(s)" % (n["scanned"], len(rules)))
out.emit(summary)
sys.exit(2 if (n["unreadable"] or problems) else (1 if out.findings else 0))
'''


def secrets_bin():
    return os.environ.get("GT_SECRETS_BIN") or os.path.join(HERE, "gt_secrets.py")


def run_secrets(root, rels, vault):
    """-> (findings [(rel, line, rule)], summary dict or None, problem or None)."""
    tool = secrets_bin()
    if not os.path.isfile(tool):
        return [], None, "credential scan could not run: gt_secrets.py not found at %s" \
            % clean_name(tool, 200)
    base = root if os.path.isdir(root) else os.path.dirname(os.path.abspath(root))
    req = {"root": base, "files": rels, "vault": vault}
    try:
        p = subprocess.run([sys.executable, "-c", SECRETS_WORKER, tool],
                           input=json.dumps(req), capture_output=True, text=True,
                           timeout=SECRETS_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], None, "credential scan could not run: %s" % exc.__class__.__name__
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        # stderr from gt_secrets' fatal() carries no value by its own contract; show one line.
        lines = (p.stderr or "").strip().splitlines()
        why = clean_name(lines[-1] if lines else "no output", 200)
        return [], None, "credential scan could not run (exit %d): %s" % (p.returncode, why)
    found = [(f["path"], int(f["line"]), str(f["rule"])) for f in doc.get("findings", [])]
    summ = doc.get("summary", {})
    problem = None
    if p.returncode not in (0, 1):
        problem = "credential scan incomplete: %d pack problem(s), %d unreadable file(s)" % (
            sum(1 for x in doc.get("notes", []) if str(x).startswith("PROBLEM")),
            int(summ.get("unreadable", 0)))
    return found, summ, problem


# -- the scan ---------------------------------------------------------------------------------
def scan(root, only_unit=None, depth=1, vault=None):
    state = {"skipped_dirs": set(), "links": [], "walked": 0, "truncated": False}
    units = {}

    def unit(name):
        return units.setdefault(name, {"unit": name, "status": "clean", "files": 0,
                                       "binary": 0, "findings": [], "incomplete": []})

    rels = []
    for rel, full in walk(root, only_unit, depth, state):
        u = unit(unit_of(rel, depth) if os.path.isdir(root) else ".")
        u["files"] += 1
        rels.append(rel)
        ext = os.path.splitext(rel)[1].lower()
        if ext in OPAQUE_DOCS:
            u["incomplete"].append("%s: %s content not scanned (document format)"
                                   % (display(rel), ext[1:]))
            continue
        try:
            size = os.path.getsize(full)
            if size > SIZE_CAP:
                u["incomplete"].append("%s: over the %d MB size cap, not scanned"
                                       % (display(rel), SIZE_CAP // (1024 * 1024)))
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            u["incomplete"].append("%s: unreadable (%s)" % (display(rel), exc.__class__.__name__))
            continue
        if b"\x00" in data[:8192]:
            u["binary"] += 1
            continue
        text = data.decode("utf-8", errors="replace")
        for kind, line, rule in scan_text(rel, text, is_code(rel, data[:2])):
            u["findings"].append({"kind": kind, "file": display(rel), "line": line,
                                  "rule": rule})
        if display(rel) != clean_name(rel):
            u["findings"].append({"kind": "prompt-injection", "file": display(rel), "line": 0,
                                  "rule": "inject.file-name"})

    for rel in state["links"]:
        target = os.path.realpath(os.path.join(root, rel))
        inside = target == os.path.realpath(root) or \
            target.startswith(os.path.realpath(root) + os.sep)
        if not inside and (only_unit is None or unit_of(rel, depth) == only_unit):
            unit(unit_of(rel, depth)).setdefault("findings", []).append(
                {"kind": "unsafe-code", "file": display(rel), "line": 0,
                 "rule": "fs.symlink-outside-tree"})

    found, secrets_summary, secrets_problem = run_secrets(root, rels, vault)
    for rel, line, rule in found:
        u = unit(unit_of(rel, depth) if os.path.isdir(root) else ".")
        u["findings"].append({"kind": "credential", "file": display(rel), "line": line,
                              "rule": rule})
    if secrets_problem:
        for u in units.values():
            u["incomplete"].append(secrets_problem)
    if state["truncated"]:
        for u in units.values():
            u["incomplete"].append("walk stopped at %d files; the rest was not scanned"
                                   % MAX_FILES)

    for u in units.values():
        u["findings"].sort(key=lambda f: (f["file"], f["line"], f["rule"]))
        u["status"] = "stop" if u["findings"] else ("incomplete" if u["incomplete"] else "clean")
    ordered = sorted(units.values(), key=lambda u: (u["unit"] != ".", u["unit"]))
    return {"root": os.path.abspath(root), "unit_depth": depth, "only_unit": only_unit,
            "units": ordered, "skipped_dirs": sorted(state["skipped_dirs"]),
            "checks": {"credential": secrets_summary and
                       {"rules": secrets_summary.get("rules"),
                        "scanned": secrets_summary.get("scanned")},
                       "credential_problem": secrets_problem,
                       "unsafe-code": {"rules": RULE_COUNT["unsafe-code"]},
                       "prompt-injection": {"rules": RULE_COUNT["prompt-injection"]}},
            "files": sum(u["files"] for u in ordered)}


def exit_code(report):
    if any(u["status"] == "stop" for u in report["units"]):
        return FOUND
    if any(u["status"] == "incomplete" for u in report["units"]):
        return CANNOT
    if report["only_unit"] is not None and not report["units"]:
        return CANNOT                       # the named unit does not exist: nothing was scanned
    return CLEAN


def verdict_line(report, code):
    stop = [u["unit"] for u in report["units"] if u["status"] == "stop"]
    inc = [u["unit"] for u in report["units"] if u["status"] == "incomplete"]
    c = report["checks"]
    cov = "checks: credential (%s), unsafe-code (%d rules), prompt-injection (%d rules); " \
          "%d file(s) in %d unit(s)" % (
              ("%s rules" % c["credential"]["rules"]) if c["credential"] else "DID NOT RUN",
              c["unsafe-code"]["rules"], c["prompt-injection"]["rules"], report["files"],
              len(report["units"]))
    if code == FOUND:
        head = "STOP - findings in %d unit(s): %s" % (len(stop), ", ".join(map(clean_name, stop)))
        if inc:
            head += "; incomplete: %s" % ", ".join(map(clean_name, inc))
    elif code == CANNOT:
        head = "INCOMPLETE - not a pass: %s" % (", ".join(map(clean_name, inc)) or
                                                "the named unit has no files")
    else:
        head = "clean"
    return "gt-intake-scan: %s -- %s" % (head, cov)


def print_text(report, code):
    print("gt-intake-scan: %s (unit depth %d)" % (clean_name(report["root"], 300),
                                                   report["unit_depth"]))
    for u in report["units"]:
        label = "(root files)" if u["unit"] == "." else ""
        counts = {}
        for f in u["findings"]:
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
        detail = ", ".join("%s %d" % (k, counts[k]) for k in KINDS if k in counts)
        print("UNIT %s %s %s  %d file(s)%s" % (clean_name(u["unit"]), label, u["status"].upper(),
                                               u["files"], ("  " + detail) if detail else ""))
        for f in u["findings"][:50]:
            print("  %-16s %s:%d  %s" % (f["kind"], f["file"], f["line"], f["rule"]))
        if len(u["findings"]) > 50:
            print("  ... %d more (use --json)" % (len(u["findings"]) - 50))
        for why in u["incomplete"][:10]:
            print("  not scanned: %s" % why)
        if len(u["incomplete"]) > 10:
            print("  ... %d more not scanned (use --json)" % (len(u["incomplete"]) - 10))
    if report["skipped_dirs"]:
        print("skipped (not scanned -- do not read these for ingest): %s"
              % ", ".join(report["skipped_dirs"]))
    print(verdict_line(report, code))


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_intake_scan.py",
        description="Scan material for credentials, unsafe code and prompt injection before "
                    "anything reads it for ingest. Read-only; never prints matched text.")
    ap.add_argument("path", help="the directory (or file) about to be ingested")
    ap.add_argument("--unit", metavar="U",
                    help="scan only this unit: a top-level folder name, or '.' for the "
                         "root-level files")
    ap.add_argument("--unit-depth", type=int, default=1, metavar="N",
                    help="folder levels that make a unit (default 1: each top-level folder; "
                         "2 splits a monorepo's packages/<name>)")
    ap.add_argument("--vault", help="the vault whose secrets packs the credential scan uses "
                                    "(default: GT_VAULT, then vault-config.json)")
    ap.add_argument("--json", action="store_true", help="the full report as JSON")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not os.path.exists(a.path):
        print("gt_intake_scan: %s does not exist" % clean_name(a.path, 300), file=sys.stderr)
        return USAGE
    if a.unit_depth < 1:
        print("gt_intake_scan: --unit-depth must be 1 or more", file=sys.stderr)
        return USAGE
    unit = a.unit.strip("/") if a.unit and a.unit != "." else a.unit
    report = scan(a.path, unit, a.unit_depth, a.vault)
    code = exit_code(report)
    report["exit"] = code
    report["verdict"] = verdict_line(report, code)
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
