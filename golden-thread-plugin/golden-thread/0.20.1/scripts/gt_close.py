#!/usr/bin/env python3
"""gt_close.py -- close a project, a task or a handoff, and refuse while anything is undecided.

    gt_close.py project SLUG --vault V [--json]                          # what stands in the way
    gt_close.py project SLUG --vault V --archive [--reason TEXT] [--move] [--dry-run]
    gt_close.py task ID --vault V [--reason TEXT] [--dry-run]
    gt_close.py handoff FILE --vault V --reason TEXT [--dry-run]

/gt:gt-close drives this, and /gt:gt-handle's per-item "close" runs the same two verbs, so there
is ONE definition of what closing each thing means (0.18.1, owner decision 2026-10-01):

  project   Read-only by default: every open task in the project (and its sub-projects) --
            deferred ones too, because a deferral comes back -- and every handoff that is open
            or deferred, plus the research.md entries to offer for graduation. Exit 1 while
            any task or handoff is undecided: closing halts until each one has a disposition
            (close, drop, move to another project, or keep -- which in a closing project means
            shelved at p:: 7, `gt_task.py shelve`). With --archive and nothing undecided it runs
            `vault_init.py archive-project`: ARCHIVED IN PLACE (stage: archived, a banner,
            the Projects/README.md row marked) -- nothing moves and every link still resolves.
            --move also relocates the folder to Archive/<slug>/ and re-points path links through
            the write queue; it is the explicit request, never the default.
  task      `gt_task.py done` (the README line is checked off through the write queue), then
            the TASKS.md rollup, which is what records the `task.done` event -- one emitter
            for task events, as gt_events requires. The rollup emits nothing until the event
            stream holds a task event (`gt_events.py backfill` seeds it), by design.
  handoff   Refused while a task citing the handoff is still open -- settle those first.
            Then `gt_handoff_status.py mark --status handled`, through the write queue, and a
            `retire` event whose note starts `handoff.close` (schema v1 has no handoff kind,
            and a new kind would break every reader that validates against v1).

Nothing here writes vault Markdown itself: every write is a sanctioned tool's, and those go
through the write queue (Core rule 1). --vault is required (Core rule 2); --dry-run rehearses.

Exit: 0 done (or nothing in the way) | 1 refused: something is undecided, or the write did not
land | 2 usage | 3 could not do it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gt_handoff_status as H                                  # noqa: E402

TOOLS_REL = Path("Projects") / "golden-thread" / "tools"
SHELVED_P = 7


def tool(vault: Path, name: str) -> Path:
    return vault / TOOLS_REL / name


def run(args, timeout=180):
    return subprocess.run([sys.executable] + [str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout)


def _in(slug: str, project: str) -> bool:
    return project == slug or project.startswith(slug + "/")


# ------------------------------------------------------------------- project ----

def open_tasks(vault: Path, slug: str):
    """Every open, unshelved task in the project and its sub-projects, deferred ones included."""
    t = tool(vault, "gt_task.py")
    if not t.is_file():
        return None, "no gt_task.py in the vault (%s)" % t
    rows = {}
    for extra in ([], ["deferred"]):
        p = run([t, "list", "--vault", vault, slug] + extra + ["--json"])
        if p.returncode != 0:
            return None, "gt_task.py list failed: %s" % (p.stderr.strip() or p.returncode)
        try:
            for r in json.loads(p.stdout):
                if r.get("p", 0) < SHELVED_P:
                    rows[r["id"]] = r
        except ValueError:
            return None, "gt_task.py list printed no JSON"
    return sorted(rows.values(), key=lambda r: (r["p"], r["project"], r["line"])), None


def open_handoffs(vault: Path, slug: str):
    return [r for r in H.all_handoffs(vault) if _in(slug, r["project"])
            and r["status"] in ("open", "deferred")]


def research_entries(proj: Path):
    """`## ` headings of research.md -- the candidates /gt:gt-close offers for graduation."""
    try:
        text = (proj / "research.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", text, re.M)]


def _spool():
    """The vault tools' gt_spool (templates/tools): the ONE slug -> path resolver."""
    tools = str(HERE.parent / "templates" / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    return gt_spool


def project_dir(vault: Path, slug: str):
    """-> (folder, None) or (None, why), via gt_spool.resolve_project (0.18.1): a bare
    sub-project slug finds Projects/<parent>/<slug>, a slug two projects share names both."""
    if not slug or ".." in slug.split("/") or slug.startswith("/"):
        return None, "no project %r under Projects/" % slug
    S = _spool()
    try:
        return vault / "Projects" / S.resolve_project(vault, slug), None
    except S.ProjectNotFound as exc:
        return None, str(exc)


def do_project(a, vault: Path) -> int:
    proj, why = project_dir(vault, a.slug)
    if proj is None:
        print("%s (sub-project as parent/child)" % why, file=sys.stderr)
        return 3
    # Everything below -- task IDs, handoff projects, archive-project -- keys on the path.
    a.slug = proj.relative_to(vault / "Projects").as_posix()
    tasks, err = open_tasks(vault, a.slug)
    if err:
        print(err, file=sys.stderr)
        return 3
    handoffs = open_handoffs(vault, a.slug)
    research = research_entries(proj)
    undecided = len(tasks) + len(handoffs)
    if a.json and not a.archive:
        print(json.dumps({"project": a.slug, "open_tasks": tasks, "open_handoffs": handoffs,
                          "research_entries": research, "undecided": undecided}, indent=1))
        return 1 if undecided else 0
    if undecided or not a.archive:
        print("%s: %d open task(s), %d open handoff(s), %d research entr%s"
              % (a.slug, len(tasks), len(handoffs), len(research),
                 "y" if len(research) == 1 else "ies"))
        for r in tasks:
            print("  task     %s  [P%d%s]  %s" % (r["id"], r["p"],
                                                 (", deferred to " + r["defer"]) if r.get("defer")
                                                 else "", r["text"]))
        for r in handoffs:
            print("  handoff  %s  [%s]" % (r["path"], r["status"]))
    if undecided:
        print("\nHALT: %d item(s) have no disposition. For each task: close (`gt_close.py task`), "
              "drop, move to another project (`gt_task.py move`) or keep (`gt_task.py shelve`, "
              "p:: %d). For each handoff: settle its items, then `gt_close.py handoff`. "
              "Nothing was archived." % (undecided, SHELVED_P), file=sys.stderr)
        return 1
    if not a.archive:
        print("\nnothing undecided -- ready to archive (--archive%s)"
              % ("" if a.move else "; add --move to relocate to Archive/"))
        return 0
    cmd = [HERE / "vault_init.py", "archive-project", "--vault", vault, "--slug", a.slug,
           "--reason", a.reason or "Closed."]
    if a.move:
        cmd.append("--move")
    if a.dry_run:
        cmd.append("--dry-run")
    p = run(cmd)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return 0 if p.returncode == 0 else 1


# ---------------------------------------------------------------------- task ----

def do_task(a, vault: Path) -> int:
    t = tool(vault, "gt_task.py")
    if not t.is_file():
        print("no gt_task.py in the vault (%s)" % t, file=sys.stderr)
        return 3
    cmd = [t, "done", a.id, "--vault", vault]
    if a.reason:
        cmd += ["--reason", a.reason]
    if a.dry_run:
        cmd.append("--dry-run")
    p = run(cmd)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    if p.returncode != 0 or a.dry_run:
        return p.returncode
    rollup = tool(vault, "gt_tasks.py")
    if rollup.is_file():
        r = run([rollup, "--vault", vault])
        if r.returncode != 0:
            print("closed, but the TASKS.md rollup did not run (so no task.done event yet): %s"
                  % (r.stderr.strip().splitlines() or [r.returncode])[-1], file=sys.stderr)
    return 0


# ------------------------------------------------------------------- handoff ----

def _events_module(vault: Path):
    for cand in (tool(vault, "gt_events.py"), HERE.parent / "templates" / "tools" / "gt_events.py"):
        if cand.is_file():
            try:
                sys.dont_write_bytecode = True
                spec = importlib.util.spec_from_file_location("_gt_close_events", str(cand))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:                                   # noqa: BLE001
                continue
    return None


def do_handoff(a, vault: Path) -> int:
    f = Path(a.file).expanduser()
    f = f if f.is_absolute() else vault / f
    try:
        rel = f.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        print("%s is not inside the vault" % a.file, file=sys.stderr)
        return 3
    rec = next((r for r in H.all_handoffs(vault) if r["path"] == rel), None)
    if rec is None:
        print("no handoff at %s (handoffs live in Projects/<slug>/handoff/)" % rel,
              file=sys.stderr)
        return 3
    if rec["open_items"]:
        print("refused: %d task(s) citing %s are still open -- close, drop, move or keep each "
              "one first (/gt:gt-handle handoff)" % (rec["open_items"], rel), file=sys.stderr)
        return 1
    cmd = [HERE / "gt_handoff_status.py", "mark", rel, "--vault", vault, "--status", "handled",
           "--reason", a.reason]
    if a.dry_run:
        cmd.append("--dry-run")
    p = run(cmd)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    if p.returncode != 0:
        return 1 if p.returncode == 3 else p.returncode
    if a.dry_run:
        return 0
    ev = _events_module(vault)
    if ev is None:
        print("handled, but no gt_events.py found -- handoff.close event NOT recorded",
              file=sys.stderr)
        return 0
    ev.safe_emit(vault, "retire", rel, frm=rel, project=rec["project"],
                 note=ev.clip("handoff.close: " + " ".join(a.reason.split())))
    return 0


# ----------------------------------------------------------------------- CLI ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_close.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("project", help="what stands in the way of closing; --archive to close")
    pr.add_argument("slug")
    pr.add_argument("--vault", required=True)
    pr.add_argument("--archive", action="store_true",
                    help="archive in place once nothing is undecided")
    pr.add_argument("--move", action="store_true",
                    help="with --archive: also relocate to Archive/<slug>/")
    pr.add_argument("--reason", help="one line, recorded in the archive banner")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    tk = sub.add_parser("task", help="close one task by ID (gt_task.py done, then the rollup)")
    tk.add_argument("id")
    tk.add_argument("--vault", required=True)
    tk.add_argument("--reason")
    tk.add_argument("--dry-run", action="store_true")
    ho = sub.add_parser("handoff", help="mark a handoff handled once nothing citing it is open")
    ho.add_argument("file", help="the handoff, relative to the vault or absolute")
    ho.add_argument("--vault", required=True)
    ho.add_argument("--reason", required=True)
    ho.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    vault = Path(a.vault).expanduser().resolve()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    if a.cmd == "project":
        if a.move and not a.archive:
            print("--move only means something with --archive", file=sys.stderr)
            return 2
        return do_project(a, vault)
    if a.cmd == "task":
        return do_task(a, vault)
    return do_handoff(a, vault)


if __name__ == "__main__":
    sys.exit(main())
