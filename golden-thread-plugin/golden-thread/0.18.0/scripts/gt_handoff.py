#!/usr/bin/env python3
"""gt_handoff.py -- gather what the next session needs, and mark what it must not assume.

A handoff exists because the session that DESIGNS something is rarely the session that builds
it. The design session ends holding context that never reached a file, and the next one starts
confident and wrong.

  gt_handoff.py --vault V --project SLUG [--repo PATH] [--out PATH] [--force] [--json]
                [--session ID] [--since-commit SHA]

`--force` asks to replace an existing handoff file. Without it an existing file is REFUSED rather
than overwritten: a handoff is someone's record of a session, and overwriting one loses it silently.

THROUGH THE WRITE QUEUE (0.17.11). A handoff inside the vault is not written by this script: it is
queued as a `create` (gt_write_queue.py) and the queue is drained at once (gt_broker.py), so the
broker's Core-rule-1 claim check applies -- a script's own write is invisible to the hooks. A
`create` never overwrites, so `--force` over an existing handoff is ESCALATED: both versions are
kept in spool/broker/conflicts/ and the owner gets a #conflict task to choose. `--out` outside the
vault, or to a non-Markdown file, is not vault content and is written directly.

WHAT THIS SCRIPT DOES, AND WHAT IT REFUSES TO DO

It gathers FACTS: the project's stated goal, its open tasks, its recent decisions, the state of
the code repository, what is uncommitted, what the last commits were. Each fact carries where it
came from, so the next session can go and check it.

It does NOT write the narrative -- what we decided, why, and what to do next. That is the
skill's job, with a person, because it is the part that requires knowing what happened. A script
that invents that section produces a document that READS finished and is not, which is worse
than no handoff at all: the next session inherits false confidence instead of no confidence.

WHAT CHANGED THIS SESSION (0.18.0). "What is the current state" is not "what did this session
add". The vault tool gt_session.py records the vault's HEAD as `start_commit:` when a session
registers; this reads it from `Projects/golden-thread/sessions/<id>_*.md` (the id from
`--session`, else $CLAUDE_CODE_SESSION_ID / $CLAUDE_SESSION_ID / $GT_SESSION_ID) and appends
`## What Changed This Session`: new memory files (with their frontmatter description),
research.md files with the `##` headings added, new ADRs (spool slots and decisions.md
headings), design.md files touched, and Knowledge pages created or updated -- derived from
`git diff --name-status <start>..HEAD` and labelled `self-verified`. `--since-commit` overrides
the start. With no start commit to be found the section is omitted, never guessed.

THE VERIFICATION LINE. Every claim in the output is labelled `verified`, `unverified` or
`unknown`, per Core rule 10. A handoff is exactly where an unverified claim gets promoted to
settled fact by being written down in a formal-looking document -- "the tests pass" becomes
folklore the moment it is stated without saying who ran them and when. So this never says a
thing was verified: it says what it observed, and marks everything it could not observe.

Exit: 0 written (or rendered, with --json) | 2 usage: --project is not a slug | 3 NO HANDOFF WAS
WRITTEN, for any of these reasons, each of which prints its own line on stderr: the project could
not be read, the destination resolves outside the vault, the destination exists and --force was
not given, the write failed, the queue refused the path, or the request is still QUEUED (another
live session holds the file, or --force met an existing handoff and was escalated). They share a code deliberately -- to a caller they are one
outcome, "there is no handoff" -- and the reason is in the message, never in the number.
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


# -- what changed this session (0.18.0) ---------------------------------------------------------
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID")


def start_commit_for(vault, session):
    """-> (sha, source) from the session's registration file, or (None, why)."""
    sid = session or next((os.environ[v].strip() for v in SESSION_ENV if os.environ.get(v)), None)
    if not sid:
        return None, "no session id (pass --session or --since-commit)"
    d = os.path.join(vault, "Projects", "golden-thread", "sessions")
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith(sid + "_") and n.endswith(".md"))
    except OSError:
        names = []
    for n in reversed(names):
        text = _read(os.path.join(d, n)) or ""
        m = re.search(r"^start_commit:\s*([0-9a-f]{7,64})\s*$", text, re.M)
        if m:
            return m.group(1), "Projects/golden-thread/sessions/%s" % n
    return None, "no start_commit recorded for session %s" % sid


def _git_show(vault, rev, path):
    return _run(["git", "show", "%s:%s" % (rev, path)], vault) if rev else None


def _headings(text, level="## "):
    return [ln.strip() for ln in (text or "").split("\n") if ln.startswith(level)]


def _description(text):
    m = re.search(r"^---\s*\n(.*?)\n---", text or "", re.S)
    if m:
        d = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if d:
            return d.group(1).strip().strip('"').strip("'")
    return None


def vault_changes(vault, start):
    """-> dict of categorised changes start..HEAD, or None when git cannot answer."""
    if not _run(["git", "rev-parse", "--verify", "-q", start + "^{commit}"], vault):
        return None
    raw = _run(["git", "diff", "--name-status", "--no-renames", "%s..HEAD" % start, "--", "."],
               vault)
    if raw is None:
        return None
    out = {"memory": [], "research": [], "adrs": [], "design": [], "knowledge": [],
           "start": start}
    seen_adr = set()
    for line in raw.split("\n"):
        if not line.strip() or "\t" not in line:
            continue
        status, path = line.split("\t", 1)
        st = status[:1]
        if st == "D":
            continue
        parts = path.split("/")
        if (len(parts) >= 4 and parts[0] == "Projects" and "memory" in parts[2:-1]
                and path.endswith(".md") and parts[-1] != "MEMORY.md" and st == "A"):
            out["memory"].append((path, _description(_git_show(vault, "HEAD", path))))
        elif len(parts) >= 3 and parts[0] == "Projects" and parts[-1] == "research.md":
            new = _headings(_git_show(vault, "HEAD", path))
            old = set(_headings(_git_show(vault, start, path))) if st != "A" else set()
            added = [h for h in new if h not in old]
            if added:
                out["research"].append((path, added))
        elif (len(parts) >= 5 and parts[:4] == ["Projects", "golden-thread", "spool", "decisions"]
              and re.fullmatch(r"\d{4}\.md", parts[-1]) and st == "A"):
            for h in _headings(_git_show(vault, "HEAD", path)):
                if h.startswith("## ADR-") and h not in seen_adr:
                    seen_adr.add(h)
                    out["adrs"].append((parts[4], h[3:]))
        elif len(parts) >= 3 and parts[0] == "Projects" and parts[-1] == "decisions.md":
            old = set(_headings(_git_show(vault, start, path))) if st != "A" else set()
            for h in _headings(_git_show(vault, "HEAD", path)):
                if h.startswith("## ADR-") and h not in old and h not in seen_adr:
                    seen_adr.add(h)
                    out["adrs"].append((parts[1], h[3:]))
        elif len(parts) >= 3 and parts[0] == "Projects" and parts[-1] == "design.md":
            out["design"].append(path)
        elif parts[0] == "Knowledge" and path.endswith(".md"):
            out["knowledge"].append((path, "created" if st == "A" else "updated"))
    return out


def render_changes(ch, source):
    """The `## What Changed This Session` section, as Markdown lines."""
    L = ["## What Changed This Session", "",
         "_(comparing %s → HEAD; derived from `git diff --name-status`, start commit from %s — "
         "`self-verified`)_" % (ch["start"][:12], source), ""]
    if not any(ch[k] for k in ("memory", "research", "adrs", "design", "knowledge")):
        L += ["No vault changes this session.", ""]
        return L
    if ch["memory"]:
        L += ["**New memory files (%d):**" % len(ch["memory"])]
        L += ["- %s%s" % (p, (' — "%s"' % d) if d else "") for p, d in ch["memory"]] + [""]
    if ch["research"]:
        L += ["**Updated research entries (%d):**" % len(ch["research"])]
        for p, heads in ch["research"]:
            L.append("- %s — %d new section(s) appended: %s"
                     % (p, len(heads), "; ".join(h.lstrip("# ").strip() for h in heads)))
        L.append("")
    if ch["adrs"]:
        L += ["**New ADRs (%d):**" % len(ch["adrs"])]
        L += ['- %s: "%s"' % (proj, t) for proj, t in ch["adrs"]] + [""]
    if ch["design"]:
        L += ["**Updated design files (%d):**" % len(ch["design"])]
        L += ["- %s" % p for p in ch["design"]] + [""]
    if ch["knowledge"]:
        new = sum(1 for _, k in ch["knowledge"] if k == "created")
        L += ["**Knowledge pages (%d new, %d updated):**" % (new, len(ch["knowledge"]) - new)]
        L += ["- %s — %s" % (p, k) for p, k in ch["knowledge"]] + [""]
    return L


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


def render(slug, facts, repo, when, changes=None):
    L = []
    # `status: open` is what keeps it in front of people until someone deals with it
    # (gt_handoff_status, 0.17.2). Before 0.17.2 nothing recorded whether a handoff had been
    # handled, so one scrolled past once was as good as unwritten.
    L += ["---", "type: handoff", "project: %s" % slug, "status: open",
          "created: %s" % datetime.date.today().isoformat(), "---", ""]
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
    if changes:
        L += render_changes(changes["changes"], changes["source"])
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


def _queue_path(out, vault):
    """-> the vault-relative path when `out` is vault Markdown (written through the queue),
    else None (written directly: outside the vault, or not a .md file)."""
    real_vault = os.path.realpath(vault)
    real = os.path.realpath(out)
    if not real.endswith(".md") or os.path.commonpath([real_vault, real]) != real_vault:
        return None
    return os.path.relpath(real, real_vault).replace(os.sep, "/")


def _queue_create(vault, rel, out, text, replacing):
    """Create the handoff through the write queue and drain at once (0.17.11). -> 0 or 3.

    `create` never overwrites: with --force over an existing handoff the broker ESCALATES --
    both versions go to spool/broker/conflicts/ and the owner gets a #conflict task -- so a
    forced replace is a decision for a person, not a silent overwrite by a script."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gt_write_queue as wq                                   # noqa: PLC0415
    from pathlib import Path                                      # noqa: PLC0415
    hint = ("gt_handoff --force over an existing handoff: keep the version that stands"
            if replacing else None)
    try:
        results, note = wq.submit(Path(vault), [{"path": rel, "op": "create", "content": text,
                                                 "hint": hint}])
    except OSError as exc:
        print("could not queue %s: %s" % (out, exc), file=sys.stderr)
        return 3
    r = results[0]
    d = r["decision"]
    if d in ("apply", "deduplicate"):
        return 0
    if d == "refused":
        print("refusing to write %s: %s" % (out, r["reason"]), file=sys.stderr)
    elif d == "escalate":
        print("%s already exists and the write broker never overwrites one: both versions are in "
              "%s and a #conflict task asks the owner which stands. Nothing was replaced."
              % (out, r.get("conflict", "spool/broker/conflicts/")), file=sys.stderr)
    else:
        print("queued, NOT written yet: %s -- %s; %s" % (
            rel, note or r.get("reason") or d, wq.drain_hint(Path(vault))), file=sys.stderr)
    return 3


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--project", required=True, help="project slug")
    ap.add_argument("--repo", help="the code repository this project builds")
    ap.add_argument("--out", help="where to write; default is under the project")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing handoff")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--session", help="session id whose start_commit to diff from (default: "
                                      "the CLAUDE_CODE_SESSION_ID environment)")
    ap.add_argument("--since-commit", help="diff the vault from this commit instead of the "
                                           "session's recorded start_commit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the handoff that would be written; write nothing")
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

    if args.since_commit:
        start, src = args.since_commit.strip(), "--since-commit"
    else:
        start, src = start_commit_for(vault, args.session)
    changes = None
    if start:
        ch = vault_changes(vault, start)
        if ch is not None:
            changes = {"changes": ch, "source": src}
        else:
            print("note: could not diff the vault from %s; no 'What Changed' section" % start,
                  file=sys.stderr)

    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    text = render(args.project, facts, repo, when, changes)

    if args.json:
        print(json.dumps({"version": 1, "project": args.project, "generated": when,
                          "facts": facts, "cannot_know": CANNOT_KNOW,
                          "vault_changes": changes, "markdown": text}, indent=2))
        return 0
    if args.dry_run:
        print(text)
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
    rel = _queue_path(out, vault)
    if rel is None:
        # Outside the vault, or not Markdown: not vault content, so not the queue's business.
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print("could not write %s: %s" % (out, exc), file=sys.stderr)
            return 3
    else:
        rc = _queue_create(vault, rel, out, text, replacing=os.path.exists(out))
        if rc:
            return rc
    print("wrote %s" % out)
    print("\nIt is NOT finished: the design section is a placeholder a script must not fill,")
    print("and the checklist at the top is what the next session has to establish for itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
