#!/usr/bin/env python3
"""gt_write_check.py -- the subject check on a vault write (0.22.0, convention A).

    check(vault, path, op, content) -> {"status", "findings"}
    superseded(vault)               -> {old page name: page that supersedes it}

A page may carry `subject: <slug>` in its frontmatter (lowercase, hyphens, dots for hierarchy, for example
`host-fleet` or `host-fleet.node-6`). A `create` whose subject another page already holds is a
duplicate; the finding names that page. The new page may instead say `supersedes: [[Old Page]]`: then it is
allowed, the old page stays where it is, and superseded() reports it so recall can show it as superseded.
Nothing is deleted or overwritten here. A page with no subject is not checked; an append or a section
replace is not checked for duplicates. Findings hold a kind and a page path, never text; a check that
cannot run says "not-checked".
"""
import os
import re

FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
SKIP_DIRS = {".git", ".obsidian", "node_modules"}
LINK = re.compile(r"\[\[([^\]|#\\]+?)\\?(?:[|#][^\]]*)?\]\]")


def _front(text):
    m = FRONT.match(text) if isinstance(text, str) else None
    return m.group(1) if m else ""


def _field(front, name):
    for line in front.splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def subject_of(text):
    return _field(_front(text), "subject").strip("\"' ").lower()


def supersedes_of(text):
    raw = _field(_front(text), "supersedes")
    names = LINK.findall(raw) or [x for x in re.split(r"[,;]", raw.strip("[]")) if x.strip()]
    return [n.strip().strip("\"'") for n in names if n.strip()]


def _pages(vault):
    for dp, dns, fns in os.walk(str(vault)):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".md"):
                yield os.path.join(dp, fn)


def _head(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(4000)
    except OSError:
        return ""


def check(vault, path, op, content):
    if not isinstance(content, str) or not os.path.isdir(str(vault)):
        return {"status": "not-checked", "findings": []}
    subject = subject_of(content) if op == "create" else ""
    if not subject:
        return {"status": "checked", "findings": []}
    mine = os.path.normpath(os.path.join(str(vault), path))
    named = {n.lower() for n in supersedes_of(content)}
    findings = []
    for p in _pages(vault):
        if os.path.normpath(p) == mine or subject_of(_head(p)) != subject:
            continue
        if os.path.splitext(os.path.basename(p))[0].lower() in named:
            continue
        findings.append({"kind": "duplicate-subject",
                         "page": os.path.relpath(p, str(vault)).replace(os.sep, "/")})
    return {"status": "checked", "findings": sorted(findings, key=lambda f: f["page"])}


def superseded(vault):
    out = {}
    if not os.path.isdir(str(vault)):
        return out
    for p in _pages(vault):
        for old in supersedes_of(_head(p)):
            out.setdefault(old, os.path.splitext(os.path.basename(p))[0])
    return out
