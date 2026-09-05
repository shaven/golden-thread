#!/usr/bin/env python3
"""Roll every project's tasks into /TASKS.md.

Reads `## Tasks` checkbox lines from each Projects/<slug>/README.md, combines the
task's own `p::` with its project's `pp:` frontmatter, applies the four escalation
rules, and writes the rollup. Two more sources feed the top of the file:

  INBOX.md (vault root)   thoughts captured from anywhere, not yet filed to a project.
                          Unchecked lines render under "Inbox"; /gt:gt-review routes them.
  gt_closeout.py          projects whose signals say they may be finished render under
                          "Review", with the reasons, so closing a project is asked
                          about rather than forgotten.

Tasks at `p:: 7` or higher are SHELVED: kept in the README as a record, excluded from
every section and every escalation rule, counted in Project standing only. That is
how a finished project's leftover tasks stop outranking live work without being
deleted.

The README files are the only source of truth. TASKS.md is a projection — it is
overwritten on every run and must never be hand-edited.

Situational priority is computed HERE, at run time, against the real clock. It is
deliberately never stored: a written-down "current priority" is stale the moment it
lands on disk, while a stored rule stays correct forever.

Usage:  python3 Projects/golden-thread/tools/gt_tasks.py [--vault PATH]
"""
import argparse, datetime as dt, pathlib, re, sys
from zoneinfo import ZoneInfo

STALE_P1_DAYS = 7      # a p::1 older than this escalates its project one level
DEADLINE_DAYS = 3      # a due date within this many days escalates one level
SHELVED_P = 7          # p at or above this is shelved: kept, never ranked, never escalates
CLOSED_STAGES = ("complete", "archived")   # no soft escalation for a closed project

DAYS = {d: i for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])}
FIELD = re.compile(r"\[([a-z_]+)::\s*([^\]]*)\]")
TASK = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r'^([a-z_]+):\s*(.*)$', line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def parse_tasks(text):
    """Checkbox lines under a `## Tasks` heading, until the next `## ` heading."""
    m = re.search(r'^## Tasks\s*$', text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r'^## ', rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    tasks = []
    for line in block.splitlines():
        t = TASK.match(line)
        if not t:
            continue
        fields = dict(FIELD.findall(t.group(2)))
        label = FIELD.sub("", t.group(2)).strip().rstrip("—-").strip()
        tasks.append({
            "done": t.group(1).lower() == "x",
            "text": label,
            "p": int(fields.get("p", 3) or 3),
            "waiting": fields.get("waiting", "agent"),
            "due": fields.get("due"),
            "since": fields.get("since"),
            "blocks": fields.get("blocks"),
        })
    return tasks


def parse_inbox(path):
    """Unchecked checkbox lines in INBOX.md. `[project:: slug]` is a hint, not a filing:
    the line stays in the inbox until gt-review moves it and checks it off."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        t = TASK.match(line)
        if not t or t.group(1).lower() == "x":
            continue
        fields = dict(FIELD.findall(t.group(2)))
        out.append({"text": FIELD.sub("", t.group(2)).strip().rstrip("—-").strip(),
                    "project": fields.get("project"), "since": fields.get("since")})
    return out


def closeout_candidates(vault):
    """Projects gt_closeout.py thinks may be finished. Imported from beside this file;
    absent or broken, the section is simply empty -- the rollup must never fail
    because an advisory probe did."""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import gt_closeout
        return gt_closeout.candidates(vault)
    except Exception:
        return []


def window_open(rule, now):
    """`Mon-Fri 07:00-15:15 America/Chicago -> 0` — is it open right now?"""
    if not rule:
        return None
    m = re.match(
        r'\s*(\w{3})-(\w{3})\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})\s+(\S+)\s*->\s*(\d)\s*$',
        rule)
    if not m:
        return None
    d1, d2, h1, m1, h2, m2, tz, target = m.groups()
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:
        return None
    if not (DAYS[d1] <= local.weekday() <= DAYS[d2]):
        return None
    start = local.replace(hour=int(h1), minute=int(m1), second=0, microsecond=0)
    end = local.replace(hour=int(h2), minute=int(m2), second=0, microsecond=0)
    return int(target) if start <= local <= end else None


def days_since(datestr, today):
    if not datestr:
        return None
    try:
        return (today - dt.date.fromisoformat(datestr)).days
    except ValueError:
        return None


def effective(pp, escalate, tasks, now, today, stage="active"):
    """Lower is more urgent.

    PP0 means active harm accruing right now. Only an explicitly declared window
    (`pp_escalate`) can reach it — that is a deliberate statement that this period
    IS the harm window. The softer rules below are evidence of neglect, not of
    active harm, so they escalate but floor at PP1. Without that floor, three
    projects sat at PP0 simultaneously on the first run and the level stopped
    meaning anything.
    """
    eff, why = pp, []
    tgt = window_open(escalate, now)
    if tgt is not None and tgt < eff:
        eff = tgt
        why.append(f"window open -> PP{tgt}")
    # Shelved tasks are a record, not work: they never escalate. Nor does a closed
    # project -- its leftover dated tasks used to hold it at PP1 for weeks.
    if stage in CLOSED_STAGES:
        return max(eff, 0), why + [f"stage {stage}: no escalation"]
    open_tasks = [t for t in tasks if not t["done"] and t["p"] < SHELVED_P]
    soft = eff  # soft rules may not push below PP1

    stale = [t for t in open_tasks
             if t["p"] == 1 and (days_since(t["since"], today) or 0) > STALE_P1_DAYS]
    if stale:
        oldest = max(days_since(t["since"], today) for t in stale)
        soft = min(soft, pp - 1)
        why.append(f"{len(stale)} stale P1 (oldest {oldest}d)")

    soon = [t for t in open_tasks
            if t["due"] and (days_since(t["due"], today) or -99) >= -DEADLINE_DAYS]
    if soon:
        soft = min(soft, pp - 1)
        why.append(f"{len(soon)} due within {DEADLINE_DAYS}d")

    blocking = [t for t in open_tasks if t["blocks"]]
    if blocking:
        soft = min(soft, pp - 1)
        why.append("blocks " + ", ".join(sorted({t["blocks"] for t in blocking})))

    eff = min(eff, max(soft, 1))
    return max(eff, 0), why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(pathlib.Path(__file__).resolve().parents[3]))
    args = ap.parse_args()
    vault = pathlib.Path(args.vault)
    projects_dir = vault / "Projects"
    if not projects_dir.is_dir():
        sys.exit(f"no Projects/ under {vault}")

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone().date()
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M %Z")

    rows, projects = [], []
    # Sub-projects live one level deeper (parent: in frontmatter). A single-level
    # glob silently drops them -- the same class of bug CONVENTIONS.md warns about
    # for gt-lint's memory-unlisted check. Scan both levels.
    readmes = sorted(set(projects_dir.glob("*/README.md")) | set(projects_dir.glob("*/*/README.md")))
    for readme in readmes:
        text = readme.read_text()
        fm = frontmatter(text)
        if fm.get("type") != "project":
            continue
        slug = fm.get("slug", readme.parent.name)
        if fm.get("parent"):
            slug = fm["parent"] + "/" + slug
        pp = int(fm.get("pp", 3) or 3)
        tasks = parse_tasks(text)
        stage = fm.get("stage", "?")
        eff, why = effective(pp, fm.get("pp_escalate"), tasks, now, today, stage)
        projects.append({"slug": slug, "domain": fm.get("domain", "?"),
                         "stage": stage, "pp": pp, "eff": eff, "why": why,
                         "n": len([t for t in tasks if not t["done"] and t["p"] < SHELVED_P]),
                         "shelved": len([t for t in tasks if not t["done"] and t["p"] >= SHELVED_P])})
        for t in tasks:
            if not t["done"] and t["p"] < SHELVED_P:
                rows.append({**t, "slug": slug, "pp": pp, "eff": eff})

    inbox = parse_inbox(vault / "INBOX.md")
    review = closeout_candidates(vault)

    rows.sort(key=lambda r: (r["eff"], r["p"], r["due"] or "9999", r["slug"]))
    projects.sort(key=lambda p: (p["eff"], p["pp"], p["slug"]))

    def fmt(r):
        bits = []
        if r["due"]:
            d = days_since(r["due"], today)
            bits.append(f"due {r['due']}" + (f" (**{abs(d)}d overdue**)" if d and d > 0 else ""))
        if r["since"]:
            age = days_since(r["since"], today)
            if age is not None and r["p"] == 1 and age > STALE_P1_DAYS:
                bits.append(f"**stale {age}d**")
            elif age is not None:
                bits.append(f"{age}d old")
        if r["blocks"]:
            bits.append(f"blocks `{r['blocks']}`")
        return " · ".join(bits)

    L = []
    L.append("# Tasks — all projects\n")
    L.append(f"> **Generated {stamp}** by `Projects/golden-thread/tools/gt_tasks.py`.")
    L.append("> Do not edit this file. Every task lives in its project's `README.md`")
    L.append("> under `## Tasks`; this is a projection of those files.\n")
    L.append("Sort key is `PP<effective>-P<task>`. **PP is the project's priority at this")
    L.append("moment** — baseline from frontmatter, raised by any escalation rule that is")
    L.append("currently firing. Re-run the script to re-evaluate; the answer changes with")
    L.append("the clock even when no file has changed.\n")
    L.append(f"See [[CONVENTIONS]] > Priority. Thresholds: stale P1 > {STALE_P1_DAYS}d, deadline within {DEADLINE_DAYS}d; p >= {SHELVED_P} is shelved.\n")
    L.append("---\n")

    L.append(f"## Inbox — not yet filed ({len(inbox)})\n")
    L.append("Captured from anywhere, in `INBOX.md`. `/gt:gt-review` routes each one to a project;")
    L.append("until then it is here so it is not lost, and nowhere else so it does not rank.\n")
    if inbox:
        L.append("| Captured | Thought | Hint |")
        L.append("|---|---|---|")
        for i in inbox:
            L.append(f"| {i['since'] or '—'} | {i['text']} | {('`' + i['project'] + '`') if i['project'] else '—'} |")
    else:
        L.append("*Empty. Add a checkbox line to `INBOX.md` from any session.*")

    L.append(f"\n## Review — projects that may be ready to close ({len(review)})\n")
    L.append("Computed by `gt_closeout.py` from task state and write-back history. A row here is a")
    L.append("question, not a verdict: answer it with `gt_closeout.py answer <slug> yes|no|later`.\n")
    if review:
        L.append("| Project | Stage | Why it looks finished | Open / shelved |")
        L.append("|---|---|---|---|")
        for s in review:
            L.append(f"| `{s['slug']}` | {s['stage']} | {'; '.join(s['reasons'])} | {s['open']} / {s['shelved']} |")
    else:
        L.append("*Nothing looks finished right now.*")
    L.append("\n---\n")

    yours = [r for r in rows if r["waiting"] == "user"]
    mine = [r for r in rows if r["waiting"] == "agent"]

    L.append(f"## Waiting on you ({len(yours)})\n")
    L.append("Decisions and actions nobody else can take.\n")
    L.append("| | Task | Project | Notes |")
    L.append("|---|---|---|---|")
    for r in yours:
        L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | {fmt(r)} |")

    L.append(f"\n## Ready to work ({len(mine)})\n")
    L.append("Nothing blocking these — say the word.\n")
    L.append("| | Task | Project | Notes |")
    L.append("|---|---|---|---|")
    for r in mine:
        L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | {fmt(r)} |")

    other = [r for r in rows if r["waiting"] not in ("user", "agent")]
    if other:
        L.append(f"\n## External and parked ({len(other)})\n")
        L.append("| | Task | Project | State |")
        L.append("|---|---|---|---|")
        for r in other:
            L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | `{r['waiting']}` |")

    L.append("\n---\n")
    L.append("## Project standing\n")
    L.append("| Project | Domain | Stage | Baseline | Now | Why it moved | Open | Shelved |")
    L.append("|---|---|---|---|---|---|---|---|")
    for p in projects:
        moved = f"**PP{p['eff']}**" if p["eff"] != p["pp"] else f"PP{p['eff']}"
        L.append(f"| `{p['slug']}` | {p['domain']} | {p['stage']} | PP{p['pp']} | {moved} | "
                 f"{'; '.join(p['why']) or '—'} | {p['n']} | {p['shelved'] or '—'} |")

    L.append("\n---\n")
    L.append("## Live views (Obsidian only)\n")
    L.append("Dataview renders these; they are inert as raw markdown, which is why the")
    L.append("static tables above exist. Same dual-reader pattern as `Projects/README.md`.\n")
    L.append("### Everything waiting on you\n")
    L.append("```dataview\nTASK\nFROM \"Projects\"\nWHERE !completed AND waiting = \"user\"\nSORT p ASC\n```\n")
    L.append("### Every open P1\n")
    L.append("```dataview\nTASK\nFROM \"Projects\"\nWHERE !completed AND p = 1\nSORT file.folder ASC\n```\n")
    L.append("### Projects by baseline priority\n")
    L.append("```dataview\nTABLE WITHOUT ID link(file.folder, slug) AS Project, pp AS PP, stage AS Stage, domain AS Domain\n"
             "FROM \"Projects\"\nWHERE type = \"project\" AND file.name = \"README\"\nSORT pp ASC, slug ASC\n```\n")

    (vault / "TASKS.md").write_text("\n".join(L) + "\n")
    print(f"TASKS.md written — {len(rows)} open tasks across {len(projects)} projects")
    print(f"  waiting on you: {len(yours)}   ready to work: {len(mine)}   other: {len(other)}   "
          f"inbox: {len(inbox)}   review: {len(review)}   shelved: {sum(p['shelved'] for p in projects)}")
    for s in review:
        print(f"  REVIEW {s['slug']}: {'; '.join(s['reasons'])}")
    for p in projects:
        if p["eff"] != p["pp"]:
            print(f"  ESCALATED {p['slug']}: PP{p['pp']} -> PP{p['eff']} ({'; '.join(p['why'])})")


if __name__ == "__main__":
    main()
