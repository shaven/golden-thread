#!/usr/bin/env python3
"""gt_scope.py -- the scope contract in the approved plan, checked at commit (0.21.4).

    gt_scope.py check [--repo PATH] [--staged | --worktree | --base REF] [--gate G] [--json]
                      [--plan FILE] [--dry-run]
    gt_scope.py plan [--repo PATH] [--path] [--dry-run]
    gt_scope.py approve [--repo PATH] [--dry-run]

WHY. "Don't over-engineer" is a rule no hook can check. A plan that names its files only in
prose is a reminder at best: nothing reads it, and nothing in gt noticed a new dependency.
/gt:gt-plan now writes a strict `scope:` block (the branch it applies to, the files allowed to
change, generated files, the new dependencies allowed) and a `## Deviations` section. This tool
compares a change with that contract and says, for every file or dependency outside it,
whether the plan gives a written reason (justified) or not (unjustified). Whether an
abstraction was NECESSARY stays the user's call; this only makes sure the question is asked.

WHAT IS COMPARED. The staged files (the default: what a commit will hold), everything
uncommitted (--worktree), or the whole branch against its base (--base REF, or `--base auto`:
origin/HEAD, else main, master, trunk, develop, production), renames and deletions included. A
rename is one item and passes when its NEW path is allowed. The direct dependencies in
package.json, requirements*.txt, pyproject.toml, go.mod and Cargo.toml are read before and
after and compared by name; a version change is not a new dependency, and lockfiles and
transitive dependencies are not compared. Python 3.9 has no tomllib, so the two TOML files are
read by a small scanner here.

ALLOWED WITHOUT BEING LISTED. The plan file, documentation (markdown, rst, a docs/ folder, a
README, a changelog, a licence), and a test whose name pairs with an allowed file
(test_x.py, x_test.py, x_test.go, x.test.ts, x.spec.js, XTest.java for an allowed x).
Files under `generated:` are allowed and listed apart.

WHERE THE PLAN LIVES. In the repository's git directory (.git/gt/plan-current.md; a linked
worktree has its own), not the work tree. It cannot be staged by `git add -A`, `git clean`
cannot delete it, and dev/tree_is_commit.py never sees it as an untracked directory: at
.claude/gt-plan-current.md it tripped gt's own publish check on 2026-10-07. An existing
.claude/gt-plan-current.md is still read when there is no plan in the git directory.

WHEN IT DOES NOT APPLY. No plan, a draft, a plan with no scope block (written before 0.21.4),
a plan bound to another branch, a detached HEAD, or scope_gate off: it says so and exits 0.
It never refuses there.

THE APPROVAL IS STAMPED. `approve` writes `status: approved`, `approved:` and `scope_sha:` (a
hash of the scope block). Edit the block afterwards and the next `check` or `plan` finds the
hash changed and puts the plan back to `status: draft`, because what was approved is what
runs. Adding a line to `## Deviations` does not change the hash.

THE GATE. scope_gate off | warn | block (default warn; changing it needs the unlock
confirmation when gt unlock is on). warn prints unjustified deviations and exits 0. block
exits 1 on one, and /gt:gt-allin-commit then refuses the commit; --allow-findings does not
accept it. The fix is a line in `## Deviations`.

WHO RUNS IT. /gt:gt-allin-commit (a subprocess, `check --staged`), /gt:gt-implement (`--worktree`
each phase, `--base auto` at the finish) and, for a `git commit` the MODEL types in a Bash call,
the PreToolUse guard hooks/guard_scope_contract.py, which imports run_check and sets DEADLINE so a
hung git fails open. All three read the same plan with the same function, so one change gets one
verdict at every door.

Exit: 0 clean, not applicable, or only warnings | 1 refused under block (check), or a plan
that cannot be approved (approve) | 2 usage | 3 could not complete (git failed)
"""
import argparse
import fnmatch
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))

OK, REFUSED, USAGE, FAILED = 0, 1, 2, 3
GATES = ("off", "warn", "block")
DEFAULT_GATE = "warn"
BASE_CANDIDATES = ("main", "master", "trunk", "develop", "production")
LEGACY_PLAN = ".claude/gt-plan-current.md"         # where plans lived before 0.21.4
PLAN_IN_GITDIR = ("gt", "plan-current.md")
KEYS = ("branch", "allowed", "generated", "new_dependencies")
REQUIRED = ("branch", "allowed", "new_dependencies")

# Documentation is allowed without being listed (anchored at the repo root; ** crosses folders).
DOC_GLOBS = ("**/*.md", "**/*.mdx", "**/*.rst", "**/*.adoc", "docs/**", "doc/**",
             "README", "README.*", "CHANGELOG", "CHANGELOG.*", "LICENSE", "LICENSE.*",
             "NOTICE", "NOTICE.*", "AUTHORS", "CONTRIBUTING", "CONTRIBUTING.*")

# (regex on the file's base name, what it is a test OF). The first group is the subject's stem.
TEST_NAMES = (
    re.compile(r"^test_(?P<s>.+)\.[A-Za-z0-9]+$"),
    re.compile(r"^(?P<s>.+)_test\.[A-Za-z0-9]+$"),
    re.compile(r"^(?P<s>.+)\.(?:test|spec)\.[A-Za-z0-9]+$"),
    re.compile(r"^(?P<s>.+)_spec\.rb$"),
    re.compile(r"^(?P<s>.+?)(?:Tests?|Spec)\.(?:java|kt|scala|cs|swift)$"),
)
# A file whose own name says nothing: its folder names the module.
PACKAGE_FILES = ("__init__", "index", "mod", "main", "lib")


class ScopeError(Exception):
    """A scope block that is not valid; .problems is the list to show the user."""

    def __init__(self, problems):
        Exception.__init__(self, "; ".join(problems))
        self.problems = list(problems)


class ScopeTimeout(Exception):
    """A git call would run past DEADLINE (or hung past it)."""


# A time.monotonic() value after which no git call is started and a running one is given up on.
# None (the CLI's value) is no limit. The PreToolUse commit guard (hooks/guard_scope_contract.py)
# sets it so a git that hangs costs a commit a few seconds and not the hook's whole timeout.
DEADLINE = None


def _time_left():
    """Seconds until DEADLINE (None when there is none); ScopeTimeout once it has passed."""
    if DEADLINE is None:
        return None
    left = DEADLINE - time.monotonic()
    if left <= 0:
        raise ScopeTimeout("the git work ran past its time budget")
    return left


# ------------------------------------------------------------------------------- git ----

def _run_git(repo, args, left):
    """-> (returncode, stdout bytes); ScopeTimeout past `left` seconds. Not subprocess.run: on
    Windows, after a timeout it kills git and then waits for the output pipes to close, and a
    process git started (a pager, a helper, a bash wrapper's own child) still holds them, so a
    hanging git held the commit guard for that child's whole life (found 2026-10-16). Here the
    whole process tree is killed and the pipes are not waited on."""
    p = subprocess.Popen(["git", "--no-optional-locks", "-C", repo] + list(args),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, _err = p.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        raise ScopeTimeout("git %s did not finish in time" % (args[0] if args else ""))
    return p.returncode, out


def _kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        p.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    for fh in (p.stdout, p.stderr):
        try:
            fh.close()
        except (OSError, AttributeError):
            pass
    try:
        p.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _git(repo, *args):
    """-> (returncode, stdout text). Read-only commands take no optional locks."""
    left = _time_left()
    try:
        rc, out = _run_git(repo, args, left)
    except OSError as exc:
        return 127, str(exc)
    return rc, out.decode("utf-8", "replace")


def _git_bytes(repo, *args):
    left = _time_left()
    try:
        rc, out = _run_git(repo, args, left)
    except OSError:
        return None
    return out if rc == 0 else None


def resolve_repo(path):
    rc, out = _git(path, "rev-parse", "--show-toplevel")
    return out.strip() if rc == 0 and out.strip() else None


def git_dir(repo):
    rc, out = _git(repo, "rev-parse", "--absolute-git-dir")
    # normpath: Windows git prints C:/..., and the plan path joined to it would mix / and \
    return os.path.normpath(out.strip()) if rc == 0 and out.strip() else None


def current_branch(repo):
    """The branch HEAD is on, or None when detached. Works before the first commit."""
    rc, out = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def has_head(repo):
    return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0


def resolve_base(repo, ref):
    """`--base auto` -> origin/HEAD's branch, else the first of BASE_CANDIDATES that exists."""
    if ref != "auto":
        return ref
    rc, out = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    for cand in BASE_CANDIDATES:
        if _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + cand)[0] == 0:
            return cand
    return None


class Change(object):
    """One file in the change. code: A added | M modified | D deleted | R renamed | C copied."""

    def __init__(self, code, path, old=None):
        self.code, self.path, self.old = code, path, old

    @property
    def verb(self):
        return {"A": "added", "M": "modified", "D": "deleted", "R": "renamed",
                "C": "copied"}.get(self.code, "modified")

    def label(self):
        return "%s -> %s" % (self.old, self.path) if self.code == "R" and self.old else self.path


def parse_name_status(out):
    """`git diff --name-status -z`: STATUS NUL path NUL, and two paths for a rename or copy."""
    toks, i, res = out.split("\0"), 0, []
    while i < len(toks):
        st = toks[i]
        if not st:
            i += 1
            continue
        code = st[0]
        if code in "RC" and i + 2 < len(toks):
            res.append(Change(code, toks[i + 2], toks[i + 1]))
            i += 3
        elif i + 1 < len(toks):
            res.append(Change(code if code in "ADM" else "M", toks[i + 1]))
            i += 2
        else:
            break
    return res


def _untracked(repo):
    rc, out = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return [Change("A", p) for p in out.split("\0") if p] if rc == 0 else []


def collect_changes(repo, mode, base=None):
    """-> (changes, before_ref, label, problem). before_ref is the commit the dependency lists
    are compared with (None before the first commit); problem is set when the base is unusable."""
    head = has_head(repo)
    if mode == "staged":
        rc, out = _git(repo, "diff", "--cached", "-M", "--name-status", "-z")
        if rc != 0:
            return [], None, "staged", "git diff --cached failed"
        return parse_name_status(out), ("HEAD" if head else None), "staged change", None
    if mode == "worktree":
        if head:
            rc, out = _git(repo, "diff", "-M", "--name-status", "-z", "HEAD")
        else:
            rc, out = _git(repo, "diff", "--cached", "-M", "--name-status", "-z")
        if rc != 0:
            return [], None, "uncommitted change", "git diff failed"
        return (parse_name_status(out) + _untracked(repo), ("HEAD" if head else None),
                "uncommitted change", None)
    # the whole branch: the merge base with BASE, against the working tree
    ref = resolve_base(repo, base or "auto")
    if not ref:
        return [], None, "branch", ("cannot find the base branch (origin/HEAD, main, master, "
                                    "trunk, develop, production); pass --base REF")
    rc, out = _git(repo, "merge-base", ref, "HEAD")
    if rc != 0 or not out.strip():
        return [], None, "branch", "no common ancestor between %s and HEAD" % ref
    mb = out.strip()
    rc, out = _git(repo, "diff", "-M", "--name-status", "-z", mb)
    if rc != 0:
        return [], None, "branch", "git diff %s failed" % mb[:10]
    return parse_name_status(out) + _untracked(repo), mb, "branch against %s" % ref, None


# ------------------------------------------------------------------------------ globs ----

@functools.lru_cache(maxsize=512)
def glob_regex(pat):
    """A path glob anchored at the repo root: `*` and `?` stay inside a folder, `**` crosses
    folders, `**/` may match no folder, a trailing `/` means everything below."""
    pat = pat.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                j = i + 2
                if pat[j:j + 1] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and pat.find("]", i + 2) != -1:
            j = pat.find("]", i + 2)
            body = pat[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(pat, path):
    try:
        return bool(glob_regex(pat).match(path))
    except re.error:
        return pat == path


def matches_any(pats, path):
    return any(glob_match(p, path) for p in pats)


def _stem(path):
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if stem in PACKAGE_FILES and "/" in path:
        return path.rsplit("/", 2)[-2]
    return stem


def subject_of_test(path):
    """The stem a test file is a test OF, or None when the name does not look like a test."""
    base = path.rsplit("/", 1)[-1]
    for rx in TEST_NAMES:
        m = rx.match(base)
        if m:
            return m.group("s")
    return None


# ------------------------------------------------------------------- the plan file ----

def split_frontmatter(text):
    """-> (meta dict, (first, last) line indexes of the frontmatter body or None, lines)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, None, lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for ln in lines[1:i]:
                m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
                if m:
                    meta[m.group(1)] = _unquote(m.group(2).strip())
            return meta, (1, i), lines
    return {}, None, lines


def set_frontmatter(lines, span, put, drop=()):
    """Return the lines with `put` (ordered pairs) set and `drop` keys removed, keeping every
    other line as it was. New keys go straight after `status:` (or the opening rule)."""
    lines = list(lines)
    if span is None:
        head = ["---"] + ["%s: %s" % kv for kv in put] + ["---"]
        return head + lines
    first, last = span
    for key in drop:
        for i in range(first, last):
            if re.match(r"^%s\s*:" % re.escape(key), lines[i]):
                del lines[i]
                last -= 1
                break
    anchor = first - 1
    for key, value in put:
        found = None
        for i in range(first, last):
            if re.match(r"^%s\s*:" % re.escape(key), lines[i]):
                found = i
                break
        if found is not None:
            lines[found] = "%s: %s" % (key, value)
            anchor = found
        else:
            if key != "status":
                for i in range(first, last):
                    if re.match(r"^status\s*:", lines[i]):
                        anchor = max(anchor, i)
            lines.insert(anchor + 1, "%s: %s" % (key, value))
            anchor += 1
            last += 1
    return lines


def _unquote(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _strip_comment(s):
    out, q = [], None
    for i, ch in enumerate(s):
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def fenced_blocks(lines):
    """-> [(first content line index, [content lines])] for every fenced code block."""
    blocks, i = [], 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(1)
        j = i + 1
        while j < len(lines):
            s = lines[j].strip()
            if s.startswith(fence[0] * len(fence)) and not s.strip(fence[0]):
                break
            j += 1
        blocks.append((i + 1, lines[i + 1:j]))
        i = j + 1
    return blocks


def scope_blocks(lines):
    """The fenced blocks whose first line is `scope:`."""
    found = []
    for start, content in fenced_blocks(lines):
        for ln in content:
            t = _strip_comment(ln)
            if t.strip():
                if t.strip() == "scope:":
                    found.append((start, content))
                break
    return found


def normalised(content):
    """The block as the hash sees it: no blank lines, no comments, no trailing blanks."""
    return [t for t in (_strip_comment(ln) for ln in content) if t.strip()]


def scope_sha(blocks):
    text = "\n---\n".join("\n".join(normalised(c)) for _s, c in blocks)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Scope(object):
    def __init__(self, branch, allowed, generated, new_dependencies, notes):
        self.branch, self.allowed, self.generated = branch, allowed, generated
        self.new_dependencies, self.notes = new_dependencies, notes


def _split_flow(s):
    items, cur, q = [], [], None
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return [i for i in items if i]


def parse_scope(content):
    """The strict grammar. -> Scope, or raises ScopeError listing every problem found.

        scope:
          branch: feat/add-widget
          allowed:
            - src/widget/**
            - tests/test_widget.py
          generated:
            - dist/**
          new_dependencies: none          (or a list)

    Unknown keys, duplicates, a missing branch, allowed or new_dependencies, an unfilled
    <placeholder>, an absolute or `..` glob and tabs are all refused: a contract that accepts
    typos is a contract that quietly allows nothing, or everything."""
    problems, data = [], {}
    rows = []
    for ln in content:
        t = _strip_comment(ln)
        if not t.strip():
            continue
        if "\t" in t[:len(t) - len(t.lstrip())]:
            problems.append("indent with spaces, not tabs: %r" % t.strip())
            continue
        rows.append((len(t) - len(t.lstrip(" ")), t.strip()))
    if not rows or rows[0][1] != "scope:":
        raise ScopeError(["the block must start with `scope:`"])
    base, key_indent, cur = rows[0][0], None, None
    for ind, txt in rows[1:]:
        if txt == "-" or txt.startswith("- "):
            if cur is None or data[cur][0] != "list":
                problems.append("a list item with no list key above it: %r" % txt)
                continue
            data[cur][1].append(_unquote(txt[2:].strip()))
            continue
        if ind <= base:
            problems.append("keys must be indented under `scope:`: %r" % txt)
            continue
        if key_indent is None:
            key_indent = ind
        if ind != key_indent:
            problems.append("unexpected indentation: %r" % txt)
            continue
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", txt)
        if not m:
            problems.append("expected `key: value`, got %r" % txt)
            continue
        key, val = m.group(1), m.group(2).strip()
        if key not in KEYS:
            problems.append("unknown key %r (the keys are %s)" % (key, ", ".join(KEYS)))
            cur = None
            continue
        if key in data:
            problems.append("%r appears twice" % key)
            cur = None
            continue
        cur = key
        if val == "":
            data[key] = ("list", [])
        elif val.startswith("["):
            if not val.endswith("]"):
                problems.append("%s: a [list] must close on the same line" % key)
                data[key] = ("list", [])
            else:
                data[key] = ("list", [_unquote(x) for x in _split_flow(val[1:-1])])
        else:
            data[key] = ("scalar", _unquote(val))
    for key in REQUIRED:
        if key not in data:
            problems.append("missing %r" % key)
    branch = ""
    if "branch" in data:
        kind, val = data["branch"]
        if kind == "scalar" and re.search(r"<[^<>]+>", val):
            pass                                  # reported once, below, as an unfilled placeholder
        elif kind != "scalar" or not val or re.search(r"\s", val):
            problems.append("branch: one branch name, no spaces")
        else:
            branch = val

    def listing(key, none_ok=False):
        if key not in data:
            return []
        kind, val = data[key]
        if kind == "scalar":
            if none_ok and val.lower() in ("none", "null", "~"):
                return []
            problems.append("%s: a list (`- item` lines or [a, b])%s" % (
                key, ", or `none`" if none_ok else ""))
            return []
        return [v for v in val if v != ""]

    allowed = listing("allowed")
    generated = listing("generated")
    deps = listing("new_dependencies", none_ok=True)
    if "allowed" in data and not allowed:
        problems.append("allowed: list at least one file or glob")
    notes = []
    for key, vals in (("allowed", allowed), ("generated", generated)):
        for v in vals:
            bad = _bad_glob(v)
            if bad:
                problems.append("%s: %r %s" % (key, v, bad))
            elif v.strip("*/") == "" or v in ("**/*", "*"):
                notes.append("%s: %r allows every file, so only dependencies are checked" % (
                    key, v))
    raw = [data["branch"][1]] if "branch" in data and data["branch"][0] == "scalar" else []
    raw += [x for k in ("allowed", "generated", "new_dependencies")
            for x in (data[k][1] if k in data and data[k][0] == "list" else [])]
    for v in raw:
        if v and re.search(r"<[^<>]+>", v):
            problems.append("%r still holds a <placeholder>; fill it in" % v)
    if problems:
        raise ScopeError(problems)
    return Scope(branch, [a[2:] if a.startswith("./") else a for a in allowed],
                 [g[2:] if g.startswith("./") else g for g in generated], deps, notes)


def _bad_glob(v):
    if v.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", v):
        return "is absolute; write it relative to the repository root"
    if "\\" in v:
        return "uses a backslash; write / between folders"
    if ".." in v.split("/"):
        return "climbs out of the repository with .."
    return None


class Deviation(object):
    """One line of `## Deviations`: a path or glob (or `dependency: name`), then the reason."""

    def __init__(self, subject, reason, kind, lineno):
        self.subject, self.reason, self.kind, self.lineno = subject, reason, kind, lineno


_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")


def parse_deviations(lines):
    """The bullets under a `## Deviations` heading (any level), up to the next heading."""
    out, inside, fence = [], False, None
    for n, ln in enumerate(lines, 1):
        m = _FENCE_OPEN.match(ln)
        if m:
            fence = None if fence == m.group(1)[0] else (fence or m.group(1)[0])
            continue
        if fence:
            continue
        h = _HEADING.match(ln)
        if h:
            inside = h.group(1).strip().lower() == "deviations"
            continue
        if not inside:
            continue
        b = _BULLET.match(ln)
        if not b:
            continue
        dev = _deviation_line(b.group(1), n)
        if dev:
            out.append(dev)
    return out


def _deviation_line(text, lineno):
    m = re.match(r"^(?:`([^`]+)`|(\S+))\s*(.*)$", text.strip())
    if not m:
        return None
    subject, rest, kind = (m.group(1) or m.group(2)), m.group(3), None
    low = subject.lower()
    if low in ("dependency:", "dep:", "dependency", "dep") and rest:
        m2 = re.match(r"^(?:`([^`]+)`|(\S+))\s*(.*)$", rest)
        if m2:
            subject, rest, kind = (m2.group(1) or m2.group(2)), m2.group(3), "dep"
    elif low.startswith(("dependency:", "dep:")):
        subject, kind = subject.split(":", 1)[1].strip("`"), "dep"
    subject = subject.strip().rstrip(":,;")
    if subject.startswith("./"):
        subject = subject[2:]
    reason = re.sub(r"^[\s:;,—–-]+", "", rest).strip()
    if not subject:
        return None
    return Deviation(subject, reason if re.search(r"\w", reason) else "", kind, lineno)


def load_plan(path):
    """-> dict: path, text, lines, meta, span, blocks, deviations."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    meta, span, lines = split_frontmatter(text)
    body = lines[span[1] + 1:] if span else lines
    offset = (span[1] + 1) if span else 0
    blocks = [(s + offset, c) for s, c in scope_blocks(body)]
    return {"path": path, "text": text, "lines": lines, "meta": meta, "span": span,
            "blocks": blocks, "deviations": parse_deviations(body)}


def locate_plan(repo):
    """-> (existing plan path or None, where a new one is written, whether it is the legacy one)."""
    gd = git_dir(repo)
    new = os.path.join(gd, *PLAN_IN_GITDIR) if gd else None
    if new and os.path.isfile(new):
        return new, new, False
    legacy = os.path.join(repo, *LEGACY_PLAN.split("/"))
    if os.path.isfile(legacy):
        return legacy, new or legacy, True
    return None, new or legacy, False


def now_stamp():
    return time.strftime("%Y-%m-%d %H:%M %Z")


def _write_atomic(path, text):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def plan_state(plan, write):
    """-> (state, note, reverted). state is approved | draft (anything not approved is a draft);
    reverted is True when this call found the scope block edited after approval.

    An approved plan whose scope block no longer hashes to `scope_sha:` was edited after the
    user approved it: it goes back to draft (rewritten on disk unless write is false)."""
    status = (plan["meta"].get("status") or "draft").strip().lower()
    if status != "approved":
        return "draft", None, False
    stamp = plan["meta"].get("scope_sha", "")
    if not stamp:
        if plan["blocks"]:
            return "approved", ("approved without a scope_sha (not through `gt_scope.py approve`):"
                                " an edit to the scope block cannot be detected"), False
        return "approved", None, False
    if scope_sha(plan["blocks"]) == stamp:
        return "approved", None, False
    note = "the scope block was edited after it was approved; the plan is back to draft"
    if write:
        lines = set_frontmatter(plan["lines"], plan["span"], [
            ("status", "draft"),
            ("reverted", "%s scope block edited after approval; approve it again" % now_stamp())],
            drop=("approved", "scope_sha"))
        try:
            _write_atomic(plan["path"], "\n".join(lines))
        except OSError as exc:
            note += " (could not rewrite %s: %s)" % (plan["path"], exc)
    return "draft", note, True


# ------------------------------------------------------------------ dependency lists ----

def _norm_dep(name):
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def manifest_kind(path):
    base = path.rsplit("/", 1)[-1]
    if base == "package.json":
        return "npm"
    if base == "pyproject.toml":
        return "pyproject"
    if base == "go.mod":
        return "go"
    if base == "Cargo.toml":
        return "cargo"
    if base.endswith(".txt") and ("requirements" in base or "/requirements/" in "/" + path):
        return "requirements"
    return None


def parse_package_json(text):
    data = json.loads(text.lstrip("﻿") or "{}")
    deps = {}
    if isinstance(data, dict):
        for sec in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
            if isinstance(data.get(sec), dict):
                for name in data[sec]:
                    deps[_norm_dep(name)] = name
    return deps


def _pep508_name(spec):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return m.group(1) if m else None


def parse_requirements(text):
    deps = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        s = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not s:
            continue
        egg = re.search(r"#egg=([A-Za-z0-9._-]+)", s)
        if s.startswith("-"):
            if egg and s.startswith(("-e ", "--editable")):
                deps[_norm_dep(egg.group(1))] = egg.group(1)
            continue
        head = s.split(";", 1)[0].strip()
        if "://" in head.split("@", 1)[0] or head.startswith(("./", "/", "../")):
            if egg:
                deps[_norm_dep(egg.group(1))] = egg.group(1)
            continue
        name = _pep508_name(head)
        if name:
            deps[_norm_dep(name)] = name
    return deps


def _toml_key(text, i, stop):
    """A (dotted, maybe quoted) key from i up to `stop` or the end of the line."""
    parts, cur, n = [], [], len(text)
    while i < n:
        c = text[i]
        if c == stop or c == "\n":
            break
        if c in "\"'":
            q = c
            i += 1
            while i < n and text[i] != q and text[i] != "\n":
                if q == '"' and text[i] == "\\":
                    i += 1
                if i < n:
                    cur.append(text[i])
                i += 1
            i += 1
            continue
        if c == ".":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p], i


def _toml_value(text, i):
    """The raw text of one value from i to the end of its logical line (arrays may span lines)."""
    n, depth, out = len(text), 0, []
    while i < n:
        c = text[i]
        if c == "\n" and depth <= 0:
            break
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "\"'":
            if text.startswith(c * 3, i):
                end = text.find(c * 3, i + 3)
                end = n if end < 0 else end + 3
                out.append(text[i:end])
                i = end
                continue
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                if c == '"' and text[j] == "\\":
                    j += 1
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        out.append(c)
        i += 1
    return "".join(out).strip(), i


def toml_statements(text):
    """-> [(table path, key path, raw value)]; a table header is (path, None, None). This reads
    the shape of the file, not its values: enough to find dependency lists, and no more."""
    out, section, i, n = [], [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if text[i] == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i] == "[":
            start = i + (2 if text.startswith("[[", i) else 1)
            section, j = _toml_key(text, start, "]")
            while j < n and text[j] != "\n":
                j += 1
            out.append((section, None, None))
            i = j
            continue
        key, j = _toml_key(text, i, "=")
        if j >= n or text[j] != "=":
            while i < n and text[i] != "\n":
                i += 1
            continue
        raw, i = _toml_value(text, j + 1)
        out.append((section, key, raw))
    return out


def toml_array_strings(raw):
    """The string items sitting directly in an array (not inside a nested table)."""
    items, depth, i, n = [], 0, 0, len(raw)
    while i < n:
        c = raw[i]
        if c in "\"'":
            if raw.startswith(c * 3, i):
                end = raw.find(c * 3, i + 3)
                end = n if end < 0 else end
                s, i = raw[i + 3:end], end + 3
            else:
                j, buf = i + 1, []
                while j < n and raw[j] != c:
                    if c == '"' and raw[j] == "\\" and j + 1 < n:
                        j += 1
                    buf.append(raw[j])
                    j += 1
                s, i = "".join(buf), j + 1
            if depth == 1:
                items.append(s)
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return items


def parse_pyproject(text):
    deps = {}

    def add(name):
        if name and name.lower() != "python":
            deps[_norm_dep(name)] = name

    def add_specs(raw):
        for spec in toml_array_strings(raw):
            add(_pep508_name(spec))

    for section, key, raw in toml_statements(text):
        if key is None:
            s = section
            if s[:3] in (["tool", "poetry", "dependencies"],
                         ["tool", "poetry", "dev-dependencies"]) and len(s) == 4:
                add(s[3])
            elif s[:3] == ["tool", "poetry", "group"] and len(s) == 6 and s[4] == "dependencies":
                add(s[5])
            continue
        full = section + key
        if full == ["project", "dependencies"] or full == ["build-system", "requires"] \
                or full == ["tool", "uv", "dev-dependencies"] \
                or (full[:2] == ["project", "optional-dependencies"] and len(full) == 3) \
                or (full[:1] == ["dependency-groups"] and len(full) == 2):
            add_specs(raw)
        elif full[:3] in (["tool", "poetry", "dependencies"],
                          ["tool", "poetry", "dev-dependencies"]) and len(full) >= 4:
            add(full[3])
        elif full[:3] == ["tool", "poetry", "group"] and len(full) >= 6 \
                and full[4] == "dependencies":
            add(full[5])
    return deps


_CARGO_KINDS = ("dependencies", "dev-dependencies", "build-dependencies")


def _cargo_table(parts):
    return bool(parts) and parts[-1] in _CARGO_KINDS and (
        len(parts) == 1 or parts[0] in ("workspace", "target"))


def parse_cargo(text):
    deps = {}

    def add(name):
        if name:
            deps[re.sub(r"[-_]+", "-", name.strip().lower())] = name

    for section, key, raw in toml_statements(text):
        if key is None:
            if len(section) >= 2 and _cargo_table(section[:-1]):
                add(section[-1])
            continue
        if _cargo_table(section):
            real = re.search(r"""package\s*=\s*["']([^"']+)["']""", raw or "")
            add(real.group(1) if real else key[0])
    return deps


def parse_go_mod(text):
    deps, in_block = {}, False

    def take(line):
        code, _sep, comment = line.partition("//")
        if "indirect" in comment:
            return
        toks = code.split()
        if len(toks) >= 2:
            deps[_norm_dep(toks[0].strip('"'))] = toks[0].strip('"')

    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if in_block:
            if s.startswith(")"):
                in_block = False
            elif s:
                take(s)
            continue
        if re.match(r"^require\s*\($", s):
            in_block = True
        elif re.match(r"^require\s+\S+\s+\S+", s):
            take(s[len("require"):].strip())
    return deps


PARSERS = {"npm": parse_package_json, "requirements": parse_requirements,
           "pyproject": parse_pyproject, "go": parse_go_mod, "cargo": parse_cargo}


def _read_at(repo, ref, path):
    data = _git_bytes(repo, "show", "%s:%s" % (ref, path))
    return None if data is None else data.decode("utf-8", "replace")


def _read_after(repo, mode, path):
    if mode == "staged":
        return _read_at(repo, "", path)          # `git show :path` is the index
    try:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def new_dependencies(repo, mode, before_ref, changes, notes):
    """-> [(name, manifest path, kind)] direct dependencies the change adds, by name."""
    added = []
    for ch in changes:
        kind = manifest_kind(ch.path)
        if not kind or ch.code == "D":
            continue
        after_text = _read_after(repo, mode, ch.path)
        if after_text is None:
            continue
        before_text = None
        if before_ref and ch.code != "A":
            before_text = _read_at(repo, before_ref, ch.old or ch.path)
        try:
            after = PARSERS[kind](after_text)
            before = PARSERS[kind](before_text) if before_text is not None else {}
        except (ValueError, KeyError, TypeError) as exc:
            notes.append("%s could not be read (%s); its dependencies were not compared" % (
                ch.path, exc.__class__.__name__))
            continue
        for norm in sorted(set(after) - set(before)):
            added.append((after[norm], ch.path, kind))
    return added


# ----------------------------------------------------------------------- evaluation ----

def is_docs(path):
    return matches_any(DOC_GLOBS, path)


def classify(changes, scope, repo):
    """-> [(Change, verdict, why)]; verdict: allowed | generated | plan | docs | test | outside."""
    rc, out = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    universe = set(p for p in out.split("\0") if p) if rc == 0 else set()
    for ch in changes:
        universe.add(ch.path)
        if ch.old:
            universe.add(ch.old)
    by_stem = {}
    for p in universe:
        if subject_of_test(p) is None and matches_any(scope.allowed, p):
            by_stem.setdefault(_stem(p), []).append(p)
    result = []
    for ch in changes:
        p = ch.path
        if matches_any(scope.allowed, p):
            result.append((ch, "allowed", "inside the allowed files"))
        elif matches_any(scope.generated, p):
            result.append((ch, "generated", "generated"))
        elif p == LEGACY_PLAN:
            result.append((ch, "plan", "the plan file"))
        elif is_docs(p):
            result.append((ch, "docs", "documentation"))
        else:
            subject = subject_of_test(p)
            mate = sorted(by_stem.get(subject, [])) if subject else []
            if mate:
                result.append((ch, "test", "a test of %s" % mate[0]))
            else:
                result.append((ch, "outside", "outside the allowed files"))
    return result


def justification_for_file(devs, ch):
    for d in devs:
        if d.kind == "dep":
            continue
        for p in (ch.path, ch.old):
            if p and (d.subject == p or glob_match(d.subject, p)):
                return d
    return None


def justification_for_dependency(devs, name):
    want = _norm_dep(name)
    for d in devs:
        if d.kind not in ("dep", None):
            continue
        if fnmatch.fnmatchcase(want, _norm_dep(d.subject)):
            return d
    return None


def run_check(repo, mode, base, plan_path, gate, write):
    """-> the result dict (see report_text). Never raises for a plan that does not apply."""
    res = {"applicable": False, "reason": "", "gate": gate, "mode": mode, "plan": None,
           "repo": repo,
           "branch": None, "files": 0, "counts": {}, "deviations": [], "unjustified": 0,
           "justified": 0, "notes": [], "refused": False, "label": ""}
    if gate == "off":
        res["reason"] = "scope_gate is off"
        return res
    if plan_path:
        path = plan_path
    else:
        path = locate_plan(repo)[0]
    if not path or not os.path.isfile(path):
        res["reason"] = "no plan for this repository"
        return res
    res["plan"] = path
    plan = load_plan(path)
    state, note, reverted = plan_state(plan, write)
    if state != "approved":
        if reverted:
            res["reason"] = ("the plan is back to draft: its scope block was edited after "
                             "approval, so it needs approving again")
        else:
            res["reason"] = "the plan is a draft (status: %s)" % (
                plan["meta"].get("status") or "draft")
        return res
    if note:
        res["notes"].append(note)
    if not plan["blocks"]:
        res["reason"] = "the plan has no scope block (plans written before gt 0.21.4 have none)"
        return res
    branch = current_branch(repo)
    res["branch"] = branch
    try:
        if len(plan["blocks"]) > 1:
            raise ScopeError(["more than one block starts with `scope:`; keep exactly one"])
        scope = parse_scope(plan["blocks"][0][1])
    except ScopeError as exc:
        res["applicable"] = True
        res["reason"] = "the scope block in the plan is not valid"
        for prob in exc.problems:
            res["deviations"].append({
                "kind": "contract", "subject": "scope block", "old_path": None,
                "change": "invalid", "detail": prob, "justified": False, "reason": ""})
        return _finish(res)
    if branch is None:
        res["reason"] = "HEAD is detached, so the plan's branch (%s) cannot match" % scope.branch
        return res
    if scope.branch != branch:
        res["reason"] = "the plan is for branch %s; this is %s" % (scope.branch, branch)
        return res
    res["applicable"] = True
    res["notes"].extend(scope.notes)
    changes, before_ref, label, problem = collect_changes(repo, mode, base)
    res["label"] = label
    if problem:
        res["applicable"] = False
        res["reason"] = problem
        res["failed"] = True
        return res
    res["files"] = len(changes)
    verdicts = classify(changes, scope, repo)
    counts = {}
    devs = plan["deviations"]
    for ch, verdict, why in verdicts:
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict != "outside":
            continue
        d = justification_for_file(devs, ch)
        res["deviations"].append({
            "kind": "file", "subject": ch.path, "old_path": ch.old, "change": ch.verb,
            "detail": why, "justified": bool(d and d.reason), "reason": d.reason if d else "",
            "named_without_reason": bool(d and not d.reason)})
    listed = [_norm_dep(x) for x in scope.new_dependencies]
    for name, manifest, kind in new_dependencies(repo, mode, before_ref, changes, res["notes"]):
        if any(fnmatch.fnmatchcase(_norm_dep(name), pat) for pat in listed):
            counts["dependency-listed"] = counts.get("dependency-listed", 0) + 1
            continue
        d = justification_for_dependency(devs, name)
        res["deviations"].append({
            "kind": "dependency", "subject": name, "old_path": None, "change": "added",
            "detail": "added in %s, not in new_dependencies" % manifest,
            "justified": bool(d and d.reason), "reason": d.reason if d else "",
            "named_without_reason": bool(d and not d.reason)})
    res["counts"] = counts
    return _finish(res)


def _finish(res):
    res["unjustified"] = sum(1 for d in res["deviations"] if not d["justified"])
    res["justified"] = sum(1 for d in res["deviations"] if d["justified"])
    res["refused"] = res["gate"] == "block" and res["unjustified"] > 0
    return res


_ORDER = ("allowed", "test", "docs", "generated", "plan", "dependency-listed")
_NAMES = {"allowed": "allowed", "test": "paired test", "docs": "docs", "generated": "generated",
          "plan": "plan file", "dependency-listed": "listed dependency"}


def wrap(text, indent, width=78):
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _shown(path, repo):
    """The plan's path as short as it can be written: relative to the repo when it is inside."""
    if path and repo and path.startswith(repo.rstrip(os.sep) + os.sep):
        return os.path.relpath(path, repo).replace(os.sep, "/")
    return path


def report_text(res):
    out = []
    if not res["applicable"]:
        out.append("scope: not applicable -- %s" % res["reason"])
        for n in res["notes"]:
            out.append("scope: note: %s" % n)
        return "\n".join(out)
    out.append("scope: plan %s, branch %s" % (_shown(res["plan"], res.get("repo")), res["branch"]))
    if res["reason"]:
        out.append("scope: %s" % res["reason"])
    inside = [("%d %s" % (res["counts"][k], _NAMES[k])) for k in _ORDER if res["counts"].get(k)]
    if res["label"]:
        out.append("scope: %s, %d file(s)%s" % (
            res["label"], res["files"], (": " + ", ".join(inside)) if inside else ""))
    for n in res["notes"]:
        out.append("scope: note: %s" % n)
    for d in res["deviations"]:
        tag = "JUSTIFIED  " if d["justified"] else "UNJUSTIFIED"
        if d["kind"] == "dependency":
            what = "dependency %s" % d["subject"]
        elif d["kind"] == "contract":
            what = "the scope block"
        elif d["old_path"] and d["change"] == "renamed":
            what = "%s -> %s" % (d["old_path"], d["subject"])
        else:
            what = d["subject"]
        out.append("  %s  %s  (%s: %s)" % (tag, what, d["change"], d["detail"]))
        if d["justified"]:
            out.extend(wrap("why: " + d["reason"], " " * 15))
        elif d.get("named_without_reason"):
            out.append("               its Deviations line gives no reason")
    if not res["deviations"]:
        out.append("scope: nothing outside the contract.")
        return "\n".join(out)
    out.append("scope: %d unjustified, %d justified." % (res["unjustified"], res["justified"]))
    if res["unjustified"]:
        fix = ("Fix: add one line per UNJUSTIFIED item to `## Deviations` in the plan -- what "
               "it is, why it was needed, and the simpler alternative you rejected -- or take "
               "the change out.")
        if any(d["kind"] == "contract" for d in res["deviations"]):
            fix = ("Fix: correct the scope block in the plan, then approve it again with "
                   "`gt_scope.py approve`.")
        if res["gate"] == "block":
            out.extend(wrap("scope_gate is block: the commit is refused. " + fix, ""))
        else:
            out.extend(wrap("scope_gate is warn: reported, not refused. " + fix, ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- commands ----

def gate_setting():
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_settings
        value = gt_settings.get("scope_gate")
    except Exception:                            # noqa: BLE001 - unreadable means the default
        value = None
    return value if value in GATES else DEFAULT_GATE


def _repo_or_die(path):
    repo = resolve_repo(os.path.abspath(os.path.expanduser(path)))
    if not repo:
        print("not a git repository: %s" % path, file=sys.stderr)
        return None
    return repo


def cmd_check(args):
    repo = _repo_or_die(args.repo)
    if not repo:
        return USAGE
    mode = "worktree" if args.worktree else ("branch" if args.base else "staged")
    gate = args.gate or gate_setting()
    plan_path = os.path.abspath(args.plan) if args.plan else None
    if plan_path and not os.path.isfile(plan_path):
        print("no such plan file: %s" % args.plan, file=sys.stderr)
        return USAGE
    try:
        res = run_check(repo, mode, args.base, plan_path, gate, write=not args.dry_run)
    except (OSError, ValueError) as exc:
        print("scope: could not complete: %s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
        return FAILED
    if res.get("failed"):
        print("scope: could not complete: %s" % res["reason"], file=sys.stderr)
        return FAILED
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        print(report_text(res))
    return REFUSED if res["refused"] else OK


def cmd_plan(args):
    repo = _repo_or_die(args.repo)
    if not repo:
        return USAGE
    path, where, legacy = locate_plan(repo)
    if args.path:
        print(path or where)
        return OK
    print("where:    %s" % where)
    if not path:
        print("plan:     (none)")
        print("state:    missing")
        return OK
    plan = load_plan(path)
    state, note, _reverted = plan_state(plan, write=not args.dry_run)
    print("plan:     %s%s" % (path, "   (the pre-0.21.4 location)" if legacy else ""))
    print("state:    %s" % state)
    branch = "(no scope block)"
    problem = None
    if plan["blocks"]:
        try:
            branch = parse_scope(plan["blocks"][0][1]).branch
            if len(plan["blocks"]) > 1:
                problem = "more than one block starts with `scope:`"
        except ScopeError as exc:
            branch, problem = "(invalid)", exc.problems[0]
    print("branch:   %s" % branch)
    if problem:
        print("problem:  %s" % problem)
    if note:
        print("note:     %s" % note)
    return OK


def cmd_approve(args):
    repo = _repo_or_die(args.repo)
    if not repo:
        return USAGE
    path, where, _legacy = locate_plan(repo)
    if not path:
        print("no plan to approve; write it to %s first" % where, file=sys.stderr)
        return REFUSED
    plan = load_plan(path)
    if not plan["blocks"]:
        print("not approved: the plan has no scope block (a fenced block that starts with "
              "`scope:`)", file=sys.stderr)
        return REFUSED
    problems = []
    scope = None
    if len(plan["blocks"]) > 1:
        problems.append("more than one block starts with `scope:`; keep exactly one")
    else:
        try:
            scope = parse_scope(plan["blocks"][0][1])
        except ScopeError as exc:
            problems = exc.problems
    if problems:
        print("not approved: the scope block is not valid", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return REFUSED
    sha = scope_sha(plan["blocks"])
    stamp = now_stamp()
    lines = set_frontmatter(plan["lines"], plan["span"], [
        ("status", "approved"), ("approved", stamp), ("scope_sha", sha)], drop=("reverted",))
    if args.dry_run:
        print("dry run: would approve %s (scope_sha %s)" % (path, sha[:12]))
    else:
        _write_atomic(path, "\n".join(lines))
        print("approved: %s" % path)
        print("scope_sha: %s" % sha)
    here = current_branch(repo)
    if here != scope.branch:
        print("note: the plan is bound to branch %s; you are on %s. The scope check runs on "
              "%s only." % (scope.branch, here or "a detached HEAD", scope.branch))
    for n in scope.notes:
        print("note: %s" % n)
    return OK


class _Fmt(argparse.RawDescriptionHelpFormatter):
    """Help that never runs past 78 columns, whatever the terminal says."""

    def __init__(self, prog):
        argparse.RawDescriptionHelpFormatter.__init__(self, prog, max_help_position=26, width=78)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_scope.py", formatter_class=_Fmt,
        description="Check a change against the scope contract in the approved plan.",
        epilog=("The plan is the git directory's gt/plan-current.md (written by\n"
                "/gt:gt-plan). A plan with a `scope:` block, approved for this branch, is\n"
                "checked; anything else is not applicable and never refused. The gate is\n"
                "the scope_gate setting: off, warn (default) or block.\n"
                "\n"
                "Exit: 0 ok, not applicable, or warnings | 1 refused (block) | 2 usage |\n"
                "3 could not complete."))
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("check", formatter_class=_Fmt,
                       help="compare a change with the plan's scope contract",
                       description="Compare the staged files (default), the uncommitted\n"
                                   "files or the whole branch with the plan's scope contract,\n"
                                   "and the dependencies they add with the base.")
    p.add_argument("--repo", default=".", help="the repository (default: here)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true",
                   help="the index against HEAD (default)")
    g.add_argument("--worktree", action="store_true",
                   help="everything uncommitted, new files included")
    g.add_argument("--base", metavar="REF",
                   help="the whole branch against REF's merge base; 'auto' finds REF")
    p.add_argument("--plan", metavar="FILE", help="read this plan file instead of the repo's")
    p.add_argument("--gate", choices=GATES,
                   help="off, warn or block (default: the scope_gate setting)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--dry-run", action="store_true",
                   help="write nothing (a check may put an edited plan back to draft)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("plan", formatter_class=_Fmt,
                       help="where the plan is, and whether it is approved",
                       description="Say where this repository's plan is (or will be\n"
                                   "written), and its state: missing, draft or approved.")
    p.add_argument("--repo", default=".", help="the repository (default: here)")
    p.add_argument("--path", action="store_true",
                   help="print only the path of the plan, or where to write it")
    p.add_argument("--dry-run", action="store_true",
                   help="write nothing (a plan with an edited scope block stays as it is)")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("approve", formatter_class=_Fmt,
                       help="record the user's approval of the plan",
                       description="After the user says yes: check the scope block is valid,\n"
                                   "then write status: approved, approved: and scope_sha: into\n"
                                   "the plan. Refuses a plan whose scope block is missing,\n"
                                   "malformed or still holds a <placeholder>.")
    p.add_argument("--repo", default=".", help="the repository (default: here)")
    p.add_argument("--dry-run", action="store_true", help="validate and report; write nothing")
    p.set_defaults(fn=cmd_approve)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:                                      # a reason written with a dash must not crash a
            stream.reconfigure(errors="replace")  # console that cannot print it (Windows cp1252)
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return USAGE
    try:
        return args.fn(args)
    except ScopeTimeout as exc:                   # only when a caller set DEADLINE (the commit guard)
        print("scope: could not complete: %s" % exc, file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
