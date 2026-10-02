#!/usr/bin/env python3
"""Draft a self-contained CLAUDE.md section for a project's code, from its vault notes.

    gt_brief.py --vault V <slug> [--repo PATH]

Prints the section to STDOUT for a person to review. Writes nothing -- not to the repo,
not to the vault. What is missing (no decisions, no source.md) is said on STDERR, so
the section on stdout stays pasteable.

## What it implements

PROTOCOL.md, "Graduating a fact out to a repo": a project's `CLAUDE.md` is committed to
its repo root, where every Claude Code session working in that code reads it with no
vault, no plugin and no configuration. The content rule is absolute -- the substantive
part must be SELF-CONTAINED, and the only vault reference is an optional trailing
section located through `~/.claude/vault-config.json`. The path existed as a rule with no
tool, so knowledge that qualified stayed in the vault. This drafts it:

  1. what the project is      -- the README vision line, else idea.md's first paragraph
  2. Constraints              -- every ADR that is neither superseded nor carrying an
                                 `Expires when:` / `Expires:` field: the stable invariants
  3. Where it runs            -- source.md's topology, repo, deployment targets, file plan
                                 and deploy procedure, when source.md exists
  4. What not to do           -- each current ADR's rejected alternatives, one line each

then the optional "Deeper context" trailer naming `Projects/<path>/` relative to the
vault_path that file gives.

## Self-contained, mechanically

ADR NUMBERS are left out (ADR-7 means nothing in a repo); `[[wikilinks]]` become their
text; any line naming a vault folder (`Projects/`, `Knowledge/`, `global-memory/`,
`core-rules/`) is dropped from sections 1-4. The teammate test itself -- would this make
sense to someone who has never seen the vault? -- is still the reviewer's: the draft is
a proposal, which is why it goes to stdout.

What it deliberately does NOT take: `runbook.md`. That is the incubator, where a fact
waits until it has stopped changing, and stability is a judgement this tool cannot make.

`--repo PATH` only says on stderr where the section would go and whether that repo has a
CLAUDE.md yet. Exit: 0 drafted (even partially) · 2 usage, or no such project.
"""
import argparse
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "templates" / "tools"
VAULT_REF = re.compile(r"(?:^|[\s(`\"'])(?:Projects|Knowledge|global-memory|core-rules|Sources)/")


def _tools():
    sys.path.insert(0, str(TOOLS))
    import gt_spool
    import gt_adr
    return gt_spool, gt_adr


def _plain(text):
    """Wikilinks to their text, HTML comments out, vault-path lines out."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\[\[([^\]|]+?)\\?\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]#|]+)(?:#[^\]]*)?\]\]", r"\1", text)
    return "\n".join(l for l in text.splitlines() if not VAULT_REF.search(l))


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def description(proj):
    readme = _read(proj / "README.md") or ""
    m = re.search(r"^>\s*(.+)$", readme, re.M)
    if m and not m.group(1).strip().startswith("<!--"):
        return _plain(m.group(1)).strip()
    idea = _read(proj / "idea.md")
    if idea:
        body = _plain(re.sub(r"^---\s*\n.*?\n---\s*\n", "", idea, count=1, flags=re.S))
        para = []
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("#"):
                if para:
                    break
                continue
            if not s:
                if para:
                    break
                continue
            para.append(s)
        if para:
            return " ".join(para)
    return ""


def _clip(s, n=200):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def constraints(adrs, superseded):
    """(stable ADRs, rejected one-liners) -- see the module docstring."""
    stable, rejected = [], []
    for a in adrs:
        f = a["fields"]
        if a["n"] in superseded or a["title"].strip() in ("", "TITLE"):
            continue
        for r in f.get("rejected") or []:
            r = _plain(r).strip()
            if r:
                rejected.append(_clip(r, 160))
        if f.get("expires when") or f.get("expires"):
            continue
        what = _plain(f.get("decision") or "").strip()
        title = _plain(a["title"]).strip()
        if title:
            stable.append("**%s**%s" % (title, (" — " + _clip(what)) if what else ""))
    return stable, rejected


def topology(proj):
    src = _read(proj / "source.md")
    if src is None:
        return None
    text = _plain(src)
    out = []
    for key in ("Topology", "Repo"):
        m = re.search(r"^\*\*%s:\*\*\s*(.+)$" % key, text, re.M)
        if m and m.group(1).strip() not in ("", "TODO", "n/a"):
            out.append("- %s: %s" % (key, m.group(1).strip()))
    for head in ("Deployment targets", "File plan", "Deploy procedure"):
        m = re.search(r"^## %s\s*\n(.*?)(?=^## |\Z)" % re.escape(head), text, re.M | re.S)
        if not m:
            continue
        rows = []
        for line in m.group(1).splitlines():
            s = line.strip()
            if not s:
                continue
            # A table row with no content (the template's `|  |  |`) is a blank, not a fact.
            if s.startswith("|") and not re.sub(r"[|\s:-]", "", s):
                continue
            rows.append(line.rstrip())
        body = [r for r in rows if r.strip().startswith("|")]
        if len(body) <= 2 and all(r.strip().startswith("|") for r in rows):
            continue                    # header + separator only: nothing filled in
        if rows:
            out.append("\n**%s**\n\n%s" % (head, "\n".join(rows)))
    return "\n".join(out).strip()


def brief(vault, rel):
    S, A = _tools()
    proj = Path(vault) / "Projects" / rel
    title = rel.rsplit("/", 1)[-1]
    rm = re.search(r"^#\s+(.+)$", _read(proj / "README.md") or "", re.M)
    if rm:
        title = rm.group(1).strip()
    missing = []
    out = ["## %s" % title, ""]
    desc = description(proj)
    if desc:
        out += [desc, ""]
    else:
        missing.append("no description: the README vision line and idea.md are both empty")
    try:
        adrs = A.project_adrs(vault, rel)
    except Exception:
        adrs = []
    if not adrs:
        missing.append("no ADRs (decisions.md absent or empty): Constraints and What not to "
                       "do are omitted")
    stable, rejected = constraints(adrs, A.superseded_by(adrs))
    if stable:
        out += ["### Constraints", ""] + ["- " + s for s in stable] + [""]
    elif adrs:
        missing.append("every ADR is superseded or carries an expiry: no stable constraints")
    topo = topology(proj)
    if topo is None:
        missing.append("no source.md: Where it runs is omitted")
    elif topo:
        out += ["### Where it runs", "", topo, ""]
    else:
        missing.append("source.md has no topology or targets filled in: Where it runs omitted")
    if rejected:
        out += ["### What not to do", ""] + ["- " + r for r in rejected] + [""]
    out += ["## Deeper context, if this machine has the vault", "",
            "If `~/.claude/vault-config.json` exists, read `vault_path` from it; the notes are "
            "at `Projects/%s/`. **If it is absent, skip this section — everything above "
            "stands on its own.**" % rel, ""]
    return "\n".join(out), missing


def main(argv=None):
    p = argparse.ArgumentParser(description="draft a self-contained CLAUDE.md section "
                                            "(stdout only; writes nothing)")
    p.add_argument("slug")
    p.add_argument("--vault", default=None)
    p.add_argument("--repo", default=None, help="the code repo it is for (reported, never written)")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="accepted for the CLI contract; this tool never writes")
    a = p.parse_args(argv)
    S, _ = _tools()
    try:
        vault = S.vault_root(a.vault)
        rel = S.resolve_project(vault, a.slug)
    except S.ProjectNotFound as exc:
        print("gt_brief: %s" % exc, file=sys.stderr)
        return 2
    text, missing = brief(vault, rel)
    sys.stdout.write(text)
    for m in missing:
        print("gt_brief: %s" % m, file=sys.stderr)
    if a.repo:
        dest = Path(a.repo).expanduser() / "CLAUDE.md"
        print("gt_brief: proposed for %s (%s) — review, then append it yourself; nothing was "
              "written" % (dest, "exists" if dest.is_file() else "does not exist yet"),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
