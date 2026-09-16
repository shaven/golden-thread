#!/usr/bin/env python3
"""gt_handoff.py -- gather what the next session needs, and mark what it must not assume.

A handoff exists because the session that DESIGNS something is rarely the session that builds
it. The design session ends holding context that never reached a file, and the next one starts
confident and wrong.

  gt_handoff.py --vault V --project SLUG [--repo PATH] [--out PATH] [--json]

WHAT THIS SCRIPT DOES, AND WHAT IT REFUSES TO DO

It gathers FACTS: the project's stated goal, its open tasks, its recent decisions, the state of
the code repository, what is uncommitted, what the last commits were. Each fact carries where it
came from, so the next session can go and check it.

It does NOT write the narrative -- what we decided, why, and what to do next. That is the
skill's job, with a person, because it is the part that requires knowing what happened. A script
that invents that section produces a document that READS finished and is not, which is worse
than no handoff at all: the next session inherits false confidence instead of no confidence.

THE VERIFICATION LINE. Every claim in the output is labelled `verified`, `unverified` or
`unknown`, per Core rule 10. A handoff is exactly where an unverified claim gets promoted to
settled fact by being written down in a formal-looking document -- "the tests pass" becomes
folklore the moment it is stated without saying who ran them and when. So this never says a
thing was verified: it says what it observed, and marks everything it could not observe.

Exit: 0 written | 2 usage | 3 the project could not be read
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

MAX_ITEMS = 12


def _run(args, cwd):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _cell(value, limit=300):
    """A table cell: truncate FIRST, then escape, so a cut cannot land on an inserted
    backslash, and escape every cell -- the `source` column was interpolated from the slug and
    was not escaped, so a slug containing pipes could forge a `verified` row (2026-09-16)."""
    text = value if isinstance(value, str) else ", ".join(value)
    text = text.replace("\n", " ").replace("\r", " ")[:limit]
    return text.replace("|", "\\|")


def project_facts(vault, slug):
    """-> {fact: {"value", "source", "verification"}} from the project's own documents."""
    base = os.path.join(vault, "Projects", slug)
    out = {}
    readme = _read(os.path.join(base, "README.md"))
    if readme is None:
        return None, "no README.md at Projects/%s" % slug

    # Goal: the first non-heading, non-frontmatter paragraph.
    body, in_fm = [], False
    for line in readme.split("\n"):
        if line.strip() == "---":
            in_fm = not in_fm
            continue
        if in_fm or line.startswith("#") or not line.strip():
            if body:
                break
            continue
        body.append(line.strip())
    if body:
        out["goal"] = {"value": " ".join(body)[:400],
                       "source": "Projects/%s/README.md" % slug,
                       "verification": "unverified"}

    tasks = []
    in_tasks = False
    for line in readme.split("\n"):
        if re.match(r"^##\s+Tasks\b", line):
            in_tasks = True
            continue
        if in_tasks and line.startswith("## "):
            break
        if in_tasks and re.match(r"^\s*[-*]\s*\[", line):
            tasks.append(line.strip())
    if tasks:
        out["open_tasks"] = {"value": tasks[:MAX_ITEMS],
                             "source": "Projects/%s/README.md ## Tasks" % slug,
                             "verification": "unverified"}

    decisions = _read(os.path.join(base, "decisions.md"))
    if decisions:
        heads = [ln.strip() for ln in decisions.split("\n") if re.match(r"^#{2,3}\s+", ln)]
        if heads:
            out["recent_decisions"] = {"value": heads[-MAX_ITEMS:],
                                       "source": "Projects/%s/decisions.md" % slug,
                                       "verification": "unverified"}

    research = _read(os.path.join(base, "research.md"))
    if research:
        heads = [ln.strip() for ln in research.split("\n") if re.match(r"^#{2,3}\s+", ln)]
        if heads:
            out["recent_research"] = {"value": heads[-MAX_ITEMS:],
                                      "source": "Projects/%s/research.md" % slug,
                                      "verification": "unverified"}
    return out, None


def repo_facts(repo):
    """-> facts about the code, each OBSERVED now rather than remembered."""
    out = {}
    if not repo or not os.path.isdir(repo):
        return out
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if branch:
        out["branch"] = {"value": branch, "source": "git, observed now",
                         "verification": "verified"}
    status = _run(["git", "status", "--short"], repo)
    if status is not None:
        lines = [ln for ln in status.split("\n") if ln.strip()]
        out["uncommitted"] = {"value": "%d file(s)" % len(lines),
                              "source": "git status, observed now",
                              "verification": "verified"}
        if lines:
            out["uncommitted_sample"] = {"value": [ln.strip() for ln in lines[:MAX_ITEMS]],
                                         "source": "git status, observed now",
                                         "verification": "verified"}
    log = _run(["git", "log", "--oneline", "-8"], repo)
    if log:
        out["recent_commits"] = {"value": log.split("\n"),
                                 "source": "git log, observed now",
                                 "verification": "verified"}
    return out


# The things a handoff must NOT let the next session assume. Each is stated as a question the
# next session has to answer for itself, because none of them can be observed from here.
CANNOT_KNOW = [
    "Do the tests pass right now? This script did not run them. If the handoff says they "
    "pass, name who ran them, when, and which ones.",
    "Is the design in these decisions still correct, or was it overtaken by something later "
    "in the session that never reached a file?",
    "Was anything agreed verbally in the session that is not written in the documents above? "
    "If so it does not exist yet -- write it down or lose it.",
    "Which of the open tasks are actually next, as opposed to merely unfinished?",
]


def render(slug, facts, repo, when):
    L = []
    L.append("# Handoff: %s" % slug)
    L.append("")
    L.append("Generated %s by `gt_handoff.py`. Every line below is either an OBSERVATION made "
             "at that moment or a QUOTE from a project document -- nothing here is analysis."
             % when)
    L.append("")
    L.append("## What the next session must not assume")
    L.append("")
    L.append("These cannot be observed from a script. Answer them before relying on anything "
             "below.")
    L.append("")
    for q in CANNOT_KNOW:
        L.append("- [ ] %s" % q)
    L.append("")
    L.append("## Observed state")
    L.append("")
    L.append("| fact | value | source | verification |")
    L.append("|---|---|---|---|")
    for key in ("goal", "branch", "uncommitted"):
        f = facts.get(key)
        if f:
            v = f["value"] if isinstance(f["value"], str) else ", ".join(f["value"])
            L.append("| %s | %s | %s | `%s` |" % (_cell(key, 40), _cell(v),
                                                  _cell(f["source"], 120),
                                                  _cell(f["verification"], 20)))
    L.append("")
    for key, title in (("open_tasks", "Open tasks"),
                       ("recent_decisions", "Recent decisions"),
                       ("recent_research", "Recent research"),
                       ("uncommitted_sample", "Uncommitted files"),
                       ("recent_commits", "Recent commits")):
        f = facts.get(key)
        if not f:
            continue
        L.append("### %s" % title)
        L.append("")
        L.append("_%s — `%s`_" % (f["source"], f["verification"]))
        L.append("")
        for item in f["value"]:
            L.append("- %s" % item)
        L.append("")
    L.append("## The design, in the author's words")
    L.append("")
    L.append("> TO BE WRITTEN BY THE SESSION THAT DID THE WORK. A script cannot write this "
             "section and must not pretend to: what was decided, what was rejected and why, "
             "and what the next session should build first. If this paragraph is still here, "
             "the handoff is INCOMPLETE and the next session should say so rather than infer "
             "the design from the file list above.")
    L.append("")
    # Only claim a repository when git actually answered. Printing the path while every git
    # fact was silently absent invited the reader to infer a state never obtained.
    if repo and any(k in facts for k in ("branch", "uncommitted", "recent_commits")):
        L.append("Repository: `%s`" % repo)
    elif repo:
        L.append("Repository `%s` was named but git returned nothing for it -- treat every "
                 "statement about the code as UNVERIFIED." % repo)
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--project", required=True, help="project slug")
    ap.add_argument("--repo", help="the code repository this project builds")
    ap.add_argument("--out", help="where to write; default is under the project")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing handoff")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # A slug, not a path. `--project ../../elsewhere` and an absolute slug both wrote outside
    # the vault entirely (validation 2026-09-16).
    if ("/" in args.project or "\\" in args.project or args.project in ("..", ".")
            or os.path.isabs(args.project)):
        print("--project takes a slug, not a path: %r" % args.project, file=sys.stderr)
        return 2

    vault = os.path.abspath(os.path.expanduser(args.vault))
    facts, err = project_facts(vault, args.project)
    if err:
        print(err, file=sys.stderr)
        return 3
    repo = os.path.abspath(os.path.expanduser(args.repo)) if args.repo else None
    facts.update(repo_facts(repo))

    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    text = render(args.project, facts, repo, when)

    if args.json:
        print(json.dumps({"version": 1, "project": args.project, "generated": when,
                          "facts": facts, "cannot_know": CANNOT_KNOW,
                          "markdown": text}, indent=2))
        return 0

    out = args.out or os.path.join(vault, "Projects", args.project, "handoff",
                                   "%s-handoff.md" % datetime.date.today().isoformat())
    real_vault = os.path.realpath(vault)
    if not args.out and not os.path.realpath(os.path.dirname(out)).startswith(real_vault):
        print("refusing to write outside the vault: %s" % out, file=sys.stderr)
        return 3
    if os.path.exists(out) and not args.force:
        print("%s already exists; pass --force to replace it. A handoff is someone's record "
              "of a session -- overwriting one silently loses it." % out, file=sys.stderr)
        return 3
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print("could not write %s: %s" % (out, exc), file=sys.stderr)
        return 3
    print("wrote %s" % out)
    print("\nIt is NOT finished: the design section is a placeholder a script must not fill,")
    print("and the checklist at the top is what the next session has to establish for itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
