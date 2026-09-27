#!/usr/bin/env python3
"""gt_scan_code.py -- source validation: check code against the `lint` rules in effect here.

A LEAF command, like gt_scan_language.py, sequenced by gt_scan.py. Unlike gt_secrets.py it
DOES print source text, because here the text is the finding rather than the thing being
protected -- which is exactly why the two may never share a process.

    gt_scan_code.py <path> [--vault V] [--sarif FILE] [--json] [--exclude GLOB]
                           [--baseline FILE] [--write-baseline FILE] [--rules]

RULES ARE DATA, NEVER CODE. Every rule comes from a `lint` pack in the slot registry, in a
documented SUBSET of ast-grep's rule schema: `id`, `message`, `severity`, `note`, `files`,
`ignores`, and a `rule` object with atomic (`pattern`, `kind`, `regex`), relational (`inside`,
`has`, with `stopBy`) and composite (`all`, `any`, `not`) forms. Same names, same nesting,
same meanings as upstream, so a user can read ast-grep's published rule reference and have it
be true here. gt evaluates that data; it never executes anything a pack supplies.

THE RULE THIS FILE TURNS ON:

    A rule that needs an evaluator tier which is not present is reported as SKIPPED,
    never silently passed.

TIERS, and what is honestly available:
    text       always. Regex over raw lines. Any language, no parser.
    stdlib     always, PYTHON ONLY. Structural matching via the `ast` module.
    astgrep    only if `ast_grep_py` imports. Structural matching for every language,
               including shell and markdown.
    treesitter only if `tree_sitter` plus a grammar imports.

On a machine with neither optional package -- which includes the machine this was written on
(Python 3.9.6, `self-verified`) -- shell and markdown get line matching only, and a rule
declaring a structural bash matcher SAYS it did not run. That skip line is the documented
weaker behaviour gt's use-if-importable pattern requires. It is also the single lesson of
2026-09-24, when selftest.sh printed PASSED over three failures and a `lint` check wired to
nothing reported clean: a scanner that quietly evaluates fewer rules than it was given is
that same defect wearing a new coat.

WHAT `kind` MEANS HERE, and the honest caveat. ast-grep's node kinds come from tree-sitter
grammars (`function_definition`); Python's `ast` uses different names (`FunctionDef`). The two
vocabularies share 1 name out of 129 (`self-verified` by intersecting tree-sitter-python's
node-types.json with ast's class list), and no published mapping exists, so KIND_MAP below is
gt's own and gt maintains it. Three classes of node cannot be mapped at any effort, because
`ast` discards them: comments, parentheses, line continuations and implicit string
concatenation. A rule asking for one of those at the stdlib tier is REFUSED, not under-matched
-- which is why "a TODO with no owner" is a `text` rule here and not a `kind: comment` rule.

Exit: 0 clean | 1 findings | 2 usage | 3 partial: a rule was skipped or a pack problem was
reported, so this scan does not cover what it was asked to cover | 4 nothing to scan with.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_registry                                       # noqa: E402

SLOT = "lint"
CLEAN, FINDINGS, USAGE, PARTIAL, NOTHING_LOADED = 0, 1, 2, 3, 4

# ---------------------------------------------------------------------------- scope (§1)
# A file is code if ANY holds: a known extension, a `#!` first two bytes, or the exec bit.
# Decided from a census rather than by taste: the two -- now THREE -- extensionless files in
# templates/githooks are `#!/bin/sh` and run on every commit in every vault gt touches, so an
# extension-only definition would skip the widest-reach code gt ships. Markdown is NOT code
# by default; a rule opts in with an explicit `files:` glob.
CODE_SUFFIX = {".py", ".sh", ".json"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "vendor", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache"}
LANG_BY_SUFFIX = {".py": "python", ".sh": "shell", ".json": "json", ".md": "markdown"}
LANG_BY_SHEBANG = ((("sh", "bash", "dash", "zsh"), "shell"), (("python",), "python"))
SIZE_CAP = 2 * 1024 * 1024


def language_of(path: Path, head: bytes) -> str | None:
    suffix_lang = LANG_BY_SUFFIX.get(path.suffix)
    if suffix_lang:
        return suffix_lang
    if head.startswith(b"#!"):
        first = head.split(b"\n", 1)[0].decode("utf-8", "replace")
        for names, lang in LANG_BY_SHEBANG:
            if any(n in first for n in names):
                return lang
    return None


def in_scope(path: Path, rel: str, excludes) -> tuple[bool, str | None]:
    for g in excludes:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*"):
            return False, "excluded by %s" % g
    try:
        if path.stat().st_size > SIZE_CAP:
            return False, "over the size cap"
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError as exc:
        return False, "unreadable (%s)" % exc.__class__.__name__
    if path.suffix in CODE_SUFFIX or head.startswith(b"#!"):
        return True, None
    if os.access(path, os.X_OK) and path.is_file() and not path.suffix:
        return True, None
    return False, "not code by the union rule"


def walk(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


# ---------------------------------------------------------------------------- the kind map
# gt's own, because no published tree-sitter <-> ast mapping exists. Keys are tree-sitter
# python node names as ast-grep spells them; values are ast class names. A kind absent here
# is REFUSED for the stdlib tier rather than silently never matching.
KIND_MAP = {
    "module": ("Module",),
    "function_definition": ("FunctionDef", "AsyncFunctionDef"),
    "class_definition": ("ClassDef",),
    "call": ("Call",),
    "attribute": ("Attribute",),
    "identifier": ("Name",),
    "assignment": ("Assign", "AnnAssign"),
    "augmented_assignment": ("AugAssign",),
    "if_statement": ("If",),
    "for_statement": ("For", "AsyncFor"),
    "while_statement": ("While",),
    "try_statement": ("Try",),
    "except_clause": ("ExceptHandler",),
    "with_statement": ("With", "AsyncWith"),
    "return_statement": ("Return",),
    "raise_statement": ("Raise",),
    "assert_statement": ("Assert",),
    "import_statement": ("Import",),
    "import_from_statement": ("ImportFrom",),
    "pass_statement": ("Pass",),
    "break_statement": ("Break",),
    "continue_statement": ("Continue",),
    "lambda": ("Lambda",),
    "comparison_operator": ("Compare",),
    "boolean_operator": ("BoolOp",),
    "binary_operator": ("BinOp",),
    "unary_operator": ("UnaryOp",),
    "list": ("List",),
    "dictionary": ("Dict",),
    "set": ("Set",),
    "tuple": ("Tuple",),
    "string": ("Constant",),
    "integer": ("Constant",),
    "float": ("Constant",),
    "true": ("Constant",),
    "false": ("Constant",),
    "none": ("Constant",),
    "subscript": ("Subscript",),
    "await": ("Await",),
    "yield": ("Yield",),
    "decorator": ("Name", "Call", "Attribute"),
    "parameters": ("arguments",),
    "argument_list": ("arguments",),
    "global_statement": ("Global",),
    "nonlocal_statement": ("Nonlocal",),
    "delete_statement": ("Delete",),
    "expression_statement": ("Expr",),
    "conditional_expression": ("IfExp",),
    "list_comprehension": ("ListComp",),
    "dictionary_comprehension": ("DictComp",),
    "set_comprehension": ("SetComp",),
    "generator_expression": ("GeneratorExp",),
    "keyword_argument": ("keyword",),
}
# Nodes `ast` throws away. Named so the refusal can say WHY, which is the difference between
# "gt has not got round to this" and "this is not possible at this tier".
IMPOSSIBLE_KINDS = {
    "comment": "ast discards comments entirely",
    "parenthesized_expression": "ast discards parentheses",
    "line_continuation": "ast discards line continuations",
    "concatenated_string": "ast merges adjacent string literals into one Constant",
    "block": "ast has no block node; a body is a plain list",
    "string_start": "ast keeps no string delimiters",
    "string_end": "ast keeps no string delimiters",
}

# Keys gt implements, and keys it explicitly does not. §2.2's discipline: publish the list and
# reject an unknown key LOUDLY, so a rule pasted from upstream fails rather than under-matches.
SUPPORTED_KEYS = {"pattern", "kind", "regex", "all", "any", "not", "has", "inside", "stopBy"}
REJECTED_KEYS = {
    "nthChild": "not implemented at any gt tier",
    "range": "not implemented at any gt tier",
    "precedes": "not implemented at any gt tier",
    "follows": "not implemented at any gt tier",
    "matches": "rule composition by reference is not implemented",
    "field": "named tree-sitter fields have no ast equivalent",
}

META = re.compile(r"\$\$\$([A-Za-z_][A-Za-z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")
MULTI_PREFIX, META_PREFIX = "__GT_MULTI_", "__GT_META_"


def rewrite_metavars(src: str) -> str:
    """`$A` and `$$$A` are not legal Python, so a pattern cannot be parsed as written.

    Rewritten to legal identifiers, which the matcher then treats as wildcards. Verified
    against `$A == $A`, `open($$$A)` and `subprocess.run($$$A)`, all three of which are a
    SyntaxError before this and parse after it.
    """
    def sub(m):
        return (MULTI_PREFIX + m.group(1)) if m.group(1) else (META_PREFIX + m.group(2))
    return META.sub(sub, src)


def is_multi(node) -> str | None:
    if isinstance(node, ast.Name) and node.id.startswith(MULTI_PREFIX):
        return node.id[len(MULTI_PREFIX):]
    if isinstance(node, ast.Expr):
        return is_multi(node.value)
    return None


def meta_name(node) -> str | None:
    if isinstance(node, ast.Name) and node.id.startswith(META_PREFIX):
        return node.id[len(META_PREFIX):]
    return None


# ---------------------------------------------------------------------------- the evaluator
class RuleError(Exception):
    """A rule gt cannot honour. Raised, never swallowed: an unhonoured rule must not pass."""


def match_node(pat, node, binds: dict) -> bool:
    """Structural equality between a pattern AST and a target AST, with metavariables.

    A `$META` matches any node and BINDS, so a second occurrence must match the same text --
    which is what makes `$A == $A` a self-comparison rule rather than "any comparison".
    """
    name = meta_name(pat)
    if name:
        try:
            text = ast.unparse(node)
        except Exception:
            return False
        if name in binds:
            return binds[name] == text
        binds[name] = text
        return True
    if type(pat) is not type(node):
        return False
    if isinstance(pat, ast.Constant):
        return pat.value == node.value
    if isinstance(pat, ast.Call):
        # A CALL IS MATCHED AS ONE UNIT, not field by field, and this is the args/keywords
        # split in its final form. tree-sitter gives a call ONE flat `argument_list` that
        # contains keyword arguments; `ast.Call` splits them into `args` and `keywords`.
        # Matching the fields independently means a pattern's empty `keywords` list demands
        # that the target have none -- so `open($$$A)` matched `open(p)` and `open(p, "rb")`
        # and silently MISSED `open(p, errors="ignore")`. Measured: two real sites in this
        # repo were missed exactly that way, and a regex written to compensate got a third
        # site wrong in the other direction.
        if not match_node(pat.func, node.func, binds):
            return False
        if any(is_multi(p) for p in pat.args):
            # `$$$` absorbs positional AND keyword arguments, because upstream they are one
            # list. Any fixed positional prefix before the wildcard must still match.
            head = pat.args[:next(i for i, p in enumerate(pat.args) if is_multi(p))]
            if len(node.args) < len(head):
                return False
            return all(match_node(p, n, binds) for p, n in zip(head, node.args))
        if len(pat.args) != len(node.args) or len(pat.keywords) != len(node.keywords):
            return False
        if not all(match_node(p, n, binds) for p, n in zip(pat.args, node.args)):
            return False
        return all(p.arg == n.arg and match_node(p.value, n.value, binds)
                   for p, n in zip(pat.keywords, node.keywords))
    for field in pat._fields:
        if field == "ctx":                      # Load/Store context is not part of a pattern
            continue
        p, n = getattr(pat, field, None), getattr(node, field, None)
        if isinstance(p, list):
            if not isinstance(n, list):
                return False
            if not match_sequence(p, n, binds, node, field):
                return False
        elif isinstance(p, ast.AST):
            if not isinstance(n, ast.AST) or not match_node(p, n, binds):
                return False
        elif p != n:
            return False
    return True


def match_sequence(pats, nodes, binds, parent, field) -> bool:
    """Match a list field, honouring `$$$` as "any number of nodes".

    THE ARGS/KEYWORDS SPLIT, which is the subtlest thing in this file. tree-sitter gives a
    call ONE flat `argument_list` containing keyword arguments; `ast.Call` has SEPARATE
    `args` and `keywords`. So a naive `$$$` matching only `args` then requires `keywords ==
    []`, and `open(f, errors="ignore")` silently fails to match `open($$$A)`. Measured: two
    real sites in this repo were missed exactly that way. When a `$$$` is present in `args`
    it therefore absorbs the keywords too.
    """
    if any(is_multi(p) for p in pats):
        if field == "args" and isinstance(parent, ast.Call):
            return True                 # absorbs args AND keywords; see the docstring
        if field == "keywords" and isinstance(parent, ast.Call):
            return True
        fixed = [p for p in pats if not is_multi(p)]
        if len(nodes) < len(fixed):
            return False
        # Positional prefix then suffix; a single `$$$` in the middle is the common shape.
        head = pats[:pats.index(next(p for p in pats if is_multi(p)))]
        tail = pats[pats.index(next(p for p in pats if is_multi(p))) + 1:]
        if len(nodes) < len(head) + len(tail):
            return False
        for p, n in zip(head, nodes[:len(head)]):
            if not match_node(p, n, binds):
                return False
        for p, n in zip(tail, nodes[len(nodes) - len(tail):] if tail else []):
            if not match_node(p, n, binds):
                return False
        return True
    if len(pats) != len(nodes):
        return False
    return all(match_node(p, n, binds) for p, n in zip(pats, nodes))


class Unit:
    """One file, parsed once, with the parent map `inside` needs."""

    def __init__(self, path: Path, rel: str, lang: str, text: str):
        self.path, self.rel, self.lang, self.text = path, rel, lang, text
        self.lines = text.splitlines()
        self.tree = None
        self.parents: dict = {}
        self.parse_error = None
        if lang == "python":
            try:
                self.tree = ast.parse(text)
            except SyntaxError as exc:
                self.parse_error = "SyntaxError line %s" % exc.lineno
                return
            for parent in ast.walk(self.tree):
                for child in ast.iter_child_nodes(parent):
                    self.parents[child] = parent

    def nodes(self):
        return ast.walk(self.tree) if self.tree is not None else ()

    def ancestors(self, node):
        cur = self.parents.get(node)
        while cur is not None:
            yield cur
            cur = self.parents.get(cur)


def kinds_for(kind: str):
    if kind in IMPOSSIBLE_KINDS:
        raise RuleError("kind %r is impossible at the stdlib tier: %s"
                        % (kind, IMPOSSIBLE_KINDS[kind]))
    if kind not in KIND_MAP:
        raise RuleError("kind %r is not in gt's stdlib kind map" % kind)
    return KIND_MAP[kind]


def check_keys(matcher: dict):
    for k in matcher:
        if k in REJECTED_KEYS:
            raise RuleError("key %r is not supported: %s" % (k, REJECTED_KEYS[k]))
        if k not in SUPPORTED_KEYS:
            raise RuleError("key %r is not an ast-grep rule key gt implements" % k)


def eval_matcher(matcher, node, unit: Unit) -> bool:
    """Does `matcher` hold at `node`? Recursive, and every unknown key raises."""
    if not isinstance(matcher, dict):
        raise RuleError("a matcher must be an object, got %s" % type(matcher).__name__)
    check_keys(matcher)

    if "all" in matcher:
        if not all(eval_matcher(m, node, unit) for m in matcher["all"]):
            return False
    if "any" in matcher:
        if not any(eval_matcher(m, node, unit) for m in matcher["any"]):
            return False
    if "not" in matcher:
        if eval_matcher(matcher["not"], node, unit):
            return False
    if "kind" in matcher:
        if type(node).__name__ not in kinds_for(matcher["kind"]):
            return False
    if "pattern" in matcher:
        try:
            pat_mod = ast.parse(rewrite_metavars(matcher["pattern"]))
        except SyntaxError:
            raise RuleError("pattern %r does not parse as Python" % matcher["pattern"][:60])
        if not pat_mod.body:
            raise RuleError("pattern is empty")
        pat = pat_mod.body[0]
        pat = pat.value if isinstance(pat, ast.Expr) else pat
        if not match_node(pat, node, {}):
            return False
    if "regex" in matcher:
        try:
            text = ast.unparse(node) if isinstance(node, ast.AST) else str(node)
        except Exception:
            return False
        try:
            if not re.search(matcher["regex"], text):
                return False
        except re.error as exc:
            raise RuleError("regex does not compile: %s" % exc)
    if "has" in matcher:
        if not _relational(matcher["has"], node, unit, matcher.get("stopBy", "neighbor"),
                           descend=True):
            return False
    if "inside" in matcher:
        if not _relational(matcher["inside"], node, unit, matcher.get("stopBy", "neighbor"),
                           descend=False):
            return False
    return True


def _relational(inner, node, unit, stop_by, descend: bool) -> bool:
    """`has` and `inside`, with ast-grep's `stopBy` depth.

    THE DEFAULT IS `neighbor`, meaning immediate children (or the immediate parent), and it
    is not a detail. Measured on this repo: a mutable-default-argument rule written with an
    unbounded `has` produced 201 findings, every one of them a function that merely contained
    an empty list somewhere in its body. The same rule with `stopBy: neighbor` produced 0 on
    the repo and still matched the fixture. 201 to 0, correctness preserved -- so an
    unbounded default would have made this scanner unusable on its first run, which is the
    one run that decides whether anyone runs it twice.

    `stopBy: "end"` is the opposite request -- search to the leaves -- and is needed by real
    rules, so both directions exist and neither can be the only behaviour.
    """
    if stop_by not in ("neighbor", "end"):
        raise RuleError("stopBy must be 'neighbor' or 'end', got %r" % (stop_by,))
    if descend:
        if stop_by == "neighbor":
            candidates = list(ast.iter_child_nodes(node)) if isinstance(node, ast.AST) else []
        else:
            candidates = [n for n in ast.walk(node) if n is not node] \
                if isinstance(node, ast.AST) else []
    else:
        anc = list(unit.ancestors(node))
        candidates = anc[:1] if stop_by == "neighbor" else anc
    return any(eval_matcher(inner, c, unit) for c in candidates)


# ---------------------------------------------------------------------------- rules & tiers
def available_tiers() -> list:
    tiers = ["text", "stdlib"]
    try:
        import ast_grep_py                                # noqa: F401
        tiers.append("astgrep")
    except Exception:
        pass
    try:
        import tree_sitter                                # noqa: F401
        tiers.append("treesitter")
    except Exception:
        pass
    return tiers


def required_tier(entry) -> str:
    """The evaluator tier a rule needs: what it declares in `evaluator`, else inferred.

    The field is `evaluator` and NOT `tier` on purpose: a pack already has a `tier`, meaning
    its SUPPORT tier (A or D, set by which directory it was merged into and never by the
    submission). Two different meanings under one name in one file is how a reader ends up
    believing a rule declared something it did not.

    Inference is deliberately conservative -- anything structural needs `stdlib` -- because
    guessing `text` for a structural rule would make it match nothing and report success.
    """
    declared = entry.get("evaluator")
    if declared:
        return declared
    matcher = entry.get("rule") or {}
    text = json.dumps(matcher)
    if any(k in text for k in ('"pattern"', '"kind"', '"has"', '"inside"')):
        return "stdlib"
    return "text"


def load_rules(vault):
    effective, shadowed, retracted, problems = gt_registry.resolve(SLOT, vault=vault)
    rules = []
    for rec in effective:
        e = rec["entry"]
        rules.append({"id": e.get("id") or "?", "lang": e.get("lang") or "*",
                      "message": e.get("message") or e.get("id") or "",
                      "severity": e.get("severity") or "warn",
                      "matcher": e.get("rule") or {}, "files": e.get("files"),
                      "tier": required_tier(e), "source": rec["source"]})
    return rules, problems, retracted


# ---------------------------------------------------------------------------- SARIF
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = ("https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
                "sarif-schema-2.1.0.json")
LEVEL = {"error": "error", "warn": "warning", "info": "note"}


def sarif_document(rules, findings, skips, scanned, tool_version="0.17.0") -> dict:
    """SARIF 2.1.0, an OASIS Standard, emitted by stdlib json.

    Required properties were taken from the schema's own `required` arrays rather than from
    the prose: sarifLog[version, runs], run[tool], tool[driver], toolComponent[name],
    result[message], reportingDescriptor[id] and -- the one a prose reading gets wrong --
    invocation[executionSuccessful]. Every object is additionalProperties:false, so gt extras
    live in `properties` bags only.
    """
    descriptors = [{"id": r["id"],
                    "shortDescription": {"text": r["message"]},
                    "defaultConfiguration": {"level": LEVEL.get(r["severity"], "warning")},
                    "properties": {"gt.tier": r["tier"], "gt.source": r["source"]}}
                   for r in rules]
    results = [{"ruleId": f["rule"],
                "level": LEVEL.get(f["severity"], "warning"),
                "message": {"text": f["message"]},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f["path"]},
                    "region": {"startLine": f["line"]}}}]}
               for f in findings]
    notifications = [{
        "level": "warning",
        "message": {"text": "Rule %r was SKIPPED: %s" % (s["rule"], s["why"])},
        "descriptor": {"id": "gt.rule-skipped"},
        "associatedRule": {"id": s["rule"]},
        "properties": {"gt.requiredTier": s.get("tier"),
                       "gt.availableTiers": available_tiers()},
    } for s in skips]
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {"name": "gt-scan-code", "version": tool_version,
                                "informationUri": "https://github.com/",
                                "rules": descriptors}},
            "invocations": [{
                "executionSuccessful": not skips,
                "toolExecutionNotifications": notifications,
                "endTimeUtc": datetime.datetime.now(datetime.timezone.utc)
                                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "properties": {"gt.filesScanned": scanned},
            }],
            "results": results,
        }],
    }


# ---------------------------------------------------------------------------- the scan
def line_of(node) -> int:
    return getattr(node, "lineno", 1) or 1


TEXT_KEYS = {"regex", "all", "any", "not"}


def eval_text(matcher, text):
    """-> (holds?, offset or None). A matcher over raw file text, for the `text` tier.

    Composite keys are supported here for one concrete reason: the useful shell rules are
    ABSENCE rules ("this script never sets -u"), and absence written as a regex needs a
    negative lookahead -- which Python allows and **Rust does not**. ast-grep's `regex` is a
    Rust regex with no lookaround, so a pack relying on `(?!...)` would work here and fail
    upstream, quietly breaking the bidirectional-migration promise §2.2 makes. Expressed as
    `{"not": {"regex": "set -u"}}` the same rule is portable, and the negation is visible in
    the rule rather than buried in regex syntax.
    """
    if not isinstance(matcher, dict):
        raise RuleError("a matcher must be an object, got %s" % type(matcher).__name__)
    for k in matcher:
        if k in REJECTED_KEYS:
            raise RuleError("key %r is not supported: %s" % (k, REJECTED_KEYS[k]))
        if k not in TEXT_KEYS:
            raise RuleError("key %r is not available at the text tier (no parser); "
                            "text rules take regex/all/any/not" % k)
    at = None
    if "regex" in matcher:
        try:
            m = re.search(matcher["regex"], text, re.M)
        except re.error as exc:
            raise RuleError("regex does not compile: %s" % exc)
        if not m:
            return False, None
        at = m.start()
    if "all" in matcher:
        for sub in matcher["all"]:
            ok, sub_at = eval_text(sub, text)
            if not ok:
                return False, None
            at = at if at is not None else sub_at
    if "any" in matcher:
        hit = False
        for sub in matcher["any"]:
            ok, sub_at = eval_text(sub, text)
            if ok:
                hit = True
                at = at if at is not None else sub_at
                break
        if not hit:
            return False, None
    if "not" in matcher:
        ok, _ = eval_text(matcher["not"], text)
        if ok:
            return False, None
    return True, at


def scan_unit(unit: Unit, rules, findings, skipped_ids):
    for r in rules:
        if r["lang"] not in ("*", unit.lang):
            continue
        if r["files"] and not fnmatch.fnmatch(unit.rel, r["files"]):
            continue
        if r["id"] in skipped_ids:
            continue
        if r["tier"] == "text":
            try:
                ok, at = eval_text(r["matcher"], unit.text)
            except (RuleError, re.error) as exc:
                skipped_ids[r["id"]] = str(exc)
                continue
            if ok:
                line = unit.text[:at].count("\n") + 1 if at is not None else 1
                findings.append({"path": unit.rel, "line": line, "rule": r["id"],
                                 "message": r["message"], "severity": r["severity"],
                                 "text": unit.lines[line - 1].strip()
                                 if 0 < line <= len(unit.lines) else ""})
            continue
        if unit.lang != "python" or unit.tree is None:
            continue
        for node in unit.nodes():
            try:
                ok = eval_matcher(r["matcher"], node, unit)
            except RuleError as exc:
                skipped_ids[r["id"]] = str(exc)
                break
            if ok:
                ln = line_of(node)
                findings.append({"path": unit.rel, "line": ln, "rule": r["id"],
                                 "message": r["message"], "severity": r["severity"],
                                 "text": unit.lines[ln - 1].strip()
                                 if 0 < ln <= len(unit.lines) else ""})


def key_of(f):
    return [f["path"], f["rule"], f["line"]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="check code against the lint rules in effect")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--vault")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sarif", metavar="FILE", help="write SARIF 2.1.0 here")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline", metavar="FILE")
    ap.add_argument("--rules", action="store_true",
                    help="list the rules in effect and the tier each needs, then exit")
    a = ap.parse_args(argv)

    rules, problems, retracted = load_rules(a.vault)
    tiers = available_tiers()

    if a.rules:
        for r in rules:
            mark = "ok" if r["tier"] in tiers else "SKIPPED"
            print("%-28s %-6s %-10s %-8s %s" % (r["id"], r["lang"], r["tier"], mark,
                                                r["source"]))
        print("tiers available here: %s" % ", ".join(tiers))
        return CLEAN

    if not rules:
        # The emptiness guard sits on the SLOT. A caller must never be able to read
        # "no findings" as "no rules exist".
        print("gt-scan-code: CANNOT RUN — the `lint` slot resolved to 0 rule(s). An empty "
              "rule set is not a clean scan. Check `gt_registry.py sources`.", file=sys.stderr)
        return NOTHING_LOADED

    skipped_ids = {}
    for r in rules:
        if r["tier"] not in tiers:
            skipped_ids[r["id"]] = "requires evaluator tier %r (available: %s)" % (
                r["tier"], ", ".join(tiers))

    root = Path(a.path).resolve()
    findings, scanned, skipped_files, excluded, unparsed = [], 0, 0, 0, 0
    for p in walk(root):
        rel = str(p.relative_to(root)) if p != root else p.name
        ok, why = in_scope(p, rel, a.exclude)
        if not ok:
            if why and why.startswith("excluded by"):
                excluded += 1
            else:
                skipped_files += 1
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped_files += 1
            continue
        with p.open("rb") as fh:
            head = fh.read(256)
        lang = language_of(p, head) or "unknown"
        unit = Unit(p, rel, lang, text)
        if unit.parse_error:
            unparsed += 1
            continue
        scan_unit(unit, rules, findings, skipped_ids)
        scanned += 1

    base = None
    if a.baseline and Path(a.baseline).is_file():
        try:
            base = {tuple(x) for x in json.loads(Path(a.baseline).read_text())["accepted"]}
        except Exception:
            print("gt-scan-code: PROBLEM baseline could not be read — reporting everything",
                  file=sys.stderr)
    if base is not None:
        findings = [f for f in findings if tuple(key_of(f)) not in base]

    if a.write_baseline:
        # A baselined finding is INVISIBLE, which is the whole hazard of having a baseline at
        # all. `reasons` is carried through a rewrite so the record of WHY something was
        # accepted survives the next --write-baseline; dropping it would leave a list of
        # silenced findings with nothing saying who silenced them or what for.
        out = Path(a.write_baseline)
        reasons = {}
        if out.is_file():
            try:
                reasons = json.loads(out.read_text()).get("reasons") or {}
            except Exception:
                reasons = {}
        doc = {"accepted": sorted(key_of(f) for f in findings)}
        doc["reasons"] = reasons
        doc["_note"] = ("Each accepted finding SHOULD have an entry in `reasons`, keyed "
                        "\"path::rule\". An accepted finding with no reason is a silenced "
                        "one, and nobody can tell later which it was.")
        out.write_text(json.dumps(doc, indent=2) + "\n")
        print("gt-scan-code: recorded %d finding(s) as accepted in %s"
              % (len(findings), a.write_baseline))
        return CLEAN

    skips = [{"rule": rid, "why": why, "tier": next((r["tier"] for r in rules
                                                     if r["id"] == rid), None)}
             for rid, why in sorted(skipped_ids.items())]

    if a.sarif:
        Path(a.sarif).write_text(json.dumps(
            sarif_document(rules, findings, skips, scanned), indent=2) + "\n")

    if a.json:
        print(json.dumps({"findings": findings, "skipped": skips,
                          "summary": {"files": scanned, "rules": len(rules),
                                      "evaluated": len(rules) - len(skips),
                                      "tiers": tiers}}, indent=2))
    else:
        for f in findings:
            print("%s:%d  %s  %s" % (f["path"], f["line"], f["rule"], f["message"]))
        for s in skips:
            print("SKIPPED %s — %s" % (s["rule"], s["why"]))
        for where, what in problems:
            print("PROBLEM %s: %s" % (where, what))
        # The affirmative line, always, and it names how many rules RAN out of how many were
        # given -- never just the finding count.
        parts = ["%d file(s)" % scanned, "%d rule(s)" % len(rules),
                 "%d evaluated (%s)" % (len(rules) - len(skips), ", ".join(tiers))]
        if skips:
            parts.append("%d SKIPPED" % len(skips))
        parts.append("%d finding(s)" % len(findings))
        if skipped_files:
            parts.append("%d file(s) not code" % skipped_files)
        if excluded:
            parts.append("%d excluded by %d glob(s)" % (excluded, len(a.exclude)))
        if unparsed:
            parts.append("%d did not parse" % unparsed)
        print("gt-scan-code: %s — %s" % ("clean" if not findings else "findings",
                                         ", ".join(parts)))

    if skips or problems:
        # PARTIAL outranks FINDINGS: "part of this did not run" is a bigger fact about the
        # result than "this is what the part that ran found".
        return PARTIAL
    return FINDINGS if findings else CLEAN


if __name__ == "__main__":
    sys.exit(main())
