#!/usr/bin/env python3
"""gt_next.py -- the one best next action, from the vault (0.22.0, phase 95a). No model call.

    gt_next.py [--vault V] [--persona a,b] [--today YYYY-MM-DD] [--json]
               [--outlook] [--outlook-connection m365@personal] [--outlook-secret-file F]

Prints at most four short lines:

    Next:  <the one action> -- <the command that does it>
    Why:   <what makes it the one>
    After: <a command to try once that is done>

It reads the project READMEs' `## Tasks` (same line shape the task tools write: `- [ ] text
[p:: 1] [waiting:: user] [due:: D] [defer:: D]`) and the waiting handoffs, and chooses by a fixed
order: a task past its due date (highest priority, then most overdue), a task waiting on you, a
handoff waiting, an open priority 1-2 task; with none of those, a start (`/gt:gt-create`,
`/gt:gt-ingest`, or `/gt:gt-init` when there is no vault yet). A persona (gt_persona) only shapes
the After line, its first featured command; it never hides or reorders what needs doing. The
wording of After comes from the commands' own `plain` blocks (gt_hints), one copy of the text.
Read-only; a vault it cannot read is one plain line, never a traceback. Stdlib only.

OUTLOOK (phase 95f). With --outlook (or when a LOTR home exists on this machine) the open Microsoft
To Do tasks are read live through the LOTR m365 connection and ranked with the vault's tasks, shown
labelled "(Outlook)"; nothing is copied into the vault. Reads only: `GET /me/todo/lists` and
`GET /me/todo/lists/{listId}/tasks`, through the enrolled read-tier client of gt_connect (`lotr
app-connect gt-next`), because a script cannot use the MCP tools. Every title is untrusted data:
control characters stripped, cut to one short line, never run. With no connection, or any gateway
failure, a fourth line `Note:  Outlook ...` says why and the vault's answer stands. The Graph
payload shape is not yet proved against a live gateway (unverified).
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from gt_connect import AppClient, LotrError
except Exception:                                               # noqa: BLE001 - Outlook is optional
    AppClient = None

    class LotrError(Exception):
        """The gateway could not be used."""
TASK = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")
FIELD = re.compile(r"\[(p|waiting|due|defer|since)::\s*([^\]]*)\]")
DONE_STAGES = ("archived", "complete", "completed")
CUT = 90
OUTLOOK = "Outlook"
MAX_LISTS, MAX_TASKS = 10, 25

DEFAULT_AFTER = {
    "overdue": "/gt:gt-list tasks mine",
    "waiting": "/gt:gt-list tasks mine",
    "handoff": "/gt:gt-list tasks mine",
    "open": "/gt:gt-work",
    "clear": "/gt:gt-ingest",
}


def vault_path(explicit=None):
    if explicit:
        return Path(explicit)
    try:
        cfg = Path(os.path.expanduser("~")) / ".claude" / "vault-config.json"
        return Path(json.loads(cfg.read_text(encoding="utf-8"))["vault_path"])
    except (OSError, ValueError, KeyError):
        return None


def short(text, cap=CUT):
    text = " ".join(re.sub(r"[*_`~]", "", text).split())
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0].rstrip(",;: -") + "..."


def frontmatter(text):
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    for l in (m.group(1).splitlines() if m else []):
        if ":" in l:
            k, v = l.split(":", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def projects(vault):
    root = vault / "Projects"
    if not root.is_dir():
        return
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        yield p.name, p
        for sub in sorted(p.iterdir()):
            if sub.is_dir() and (sub / "README.md").is_file():
                yield "%s/%s" % (p.name, sub.name), sub


def date_of(s):
    try:
        return datetime.date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def tasks(vault, today):
    """-> [{project, text, p, waiting, due}] open and not deferred past today."""
    out = []
    for slug, d in projects(vault):
        try:
            text = (d / "README.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if frontmatter(text).get("stage", "").lower() in DONE_STAGES:
            continue
        inside = False
        for line in text.splitlines():
            if line.startswith("## "):
                inside = line.strip().lower() == "## tasks"
                continue
            m = TASK.match(line) if inside else None
            if not m or m.group(1) != " ":
                continue
            fields = {k: v.strip() for k, v in FIELD.findall(m.group(2))}
            defer = date_of(fields.get("defer"))
            if defer and defer > today:
                continue
            p = fields.get("p", "")
            out.append({"project": slug, "text": short(FIELD.split(m.group(2))[0]),
                        "p": int(p) if p.isdigit() else 3,
                        "waiting": fields.get("waiting", "").lower(),
                        "due": date_of(fields.get("due"))})
    return out


def clean(text):
    """A title from outside is data: no control characters, one short line."""
    return short(re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(text or "")))


def _graph_due(task):
    return date_of(((task.get("dueDateTime") or {}).get("dateTime") or "")[:10]
                   if isinstance(task.get("dueDateTime"), dict) else None)


def outlook_tasks(client, today, connection="m365@personal"):
    """-> {"tasks": [same shape as tasks()], "note": None | one line}. Never raises."""
    out = []
    try:
        lists = (client.read(connection, "GET /me/todo/lists", {"$top": MAX_LISTS}) or {}).get("value") or []
        for lst in [l for l in lists if isinstance(l, dict) and l.get("id")][:MAX_LISTS]:
            data = client.read(connection, "GET /me/todo/lists/{listId}/tasks",
                               {"listId": lst["id"], "$top": MAX_TASKS,
                                "$filter": "status ne 'completed'"}) or {}
            for t in (data.get("value") or []):
                if not isinstance(t, dict) or t.get("status") == "completed":
                    continue
                title = clean(t.get("title"))
                if not title:
                    continue
                out.append({"project": OUTLOOK, "source": "outlook", "text": title,
                            "p": 1 if t.get("importance") == "high" else 3, "waiting": "",
                            "due": _graph_due(t)})
    except (LotrError, OSError, ValueError, AttributeError, TypeError) as e:
        return {"tasks": [], "note": "Outlook tasks could not be read (%s)." % clean(e)[:60]}
    return {"tasks": out, "note": None}


def lotr_home():
    env = os.environ.get("LOTR_HOME")
    return Path(env).expanduser() if env else Path.home() / ".config" / "gt-lotr" / "personal"


def default_client(secret_file=None):
    """-> (client, None) or (None, the Note line) when no enrolled client exists here."""
    secret = Path(secret_file).expanduser() if secret_file else lotr_home() / "clients" / "gt-next.secret"
    if AppClient is None or not secret.is_file():
        return None, ("Outlook tasks are not connected (no m365 connection for gt-next; "
                      "run `lotr app-connect gt-next`, then /gt:gt-lotr).")
    return AppClient("http://127.0.0.1:8765/mcp", "gt-next", str(secret), timeout=15), None


def waiting_handoffs(vault):
    sys.path.insert(0, str(HERE))
    try:
        import gt_handoff_status as hs
        return hs.waiting(vault)
    except Exception:                                           # noqa: BLE001 - a hint, never a failure
        return []


def _label(t):
    return "%s (%s)" % (t["text"], OUTLOOK if t.get("source") == "outlook" else t["project"])


def _cmd(t, default):
    return "finish it in Outlook To Do" if t.get("source") == "outlook" else default


def choose(vault, today, outlook=()):
    """-> {kind, action, command, why}. `outlook` is extra tasks from outlook_tasks()."""
    if vault is None or not (vault / "Projects").is_dir():
        return {"kind": "init", "action": "Set up your vault", "command": "/gt:gt-init",
                "why": "no vault was found, so there is nothing to read yet."}
    ts = tasks(vault, today) + list(outlook)
    due = sorted((t for t in ts if t["due"] and t["due"] < today), key=lambda t: (t["p"], t["due"]))
    if due:
        t = due[0]
        days = (today - t["due"]).days
        more = " (%d more overdue)" % (len(due) - 1) if len(due) > 1 else ""
        return {"kind": "overdue", "action": _label(t),
                "command": _cmd(t, "/gt:gt-handle task"),
                "why": "overdue since %s, %d day%s ago%s." % (t["due"], days, "" if days == 1 else "s", more)}
    mine = sorted((t for t in ts if t["waiting"] == "user"), key=lambda t: t["p"])
    if mine:
        t = mine[0]
        return {"kind": "waiting", "action": _label(t),
                "command": "/gt:gt-handle task",
                "why": "it is waiting on you%s." % (" (%d in all)" % len(mine) if len(mine) > 1 else "")}
    hand = waiting_handoffs(vault)
    if hand:
        h = hand[0]
        n = h.get("open_items") or 0
        return {"kind": "handoff", "action": "Work through the handoff in %s" % h["project"],
                "command": "/gt:gt-handle handoff",
                "why": "a handoff is waiting%s." % (", %d open item%s" % (n, "" if n == 1 else "s") if n else "")}
    urgent = sorted((t for t in ts if t["p"] <= 2), key=lambda t: t["p"])
    if urgent:
        t = urgent[0]
        return {"kind": "open", "action": _label(t),
                "command": _cmd(t, "/gt:gt-open %s" % t["project"].split("/")[0]),
                "why": "it is your highest-priority open task."}
    if any(True for _ in projects(vault)):
        return {"kind": "clear", "action": "Nothing is waiting. Start something new", "command": "/gt:gt-create project",
                "why": "no task is overdue, waiting on you or high priority, and no handoff is waiting."}
    return {"kind": "clear", "action": "Nothing is waiting. Start your first project", "command": "/gt:gt-create project",
            "why": "the vault has no projects yet."}


def persona_names(explicit):
    if explicit is not None:
        return [n for n in re.split(r"[,\s]+", explicit) if n]
    try:
        sys.path.insert(0, str(HERE))
        import gt_persona
        return list(gt_persona.resolve() or [])
    except Exception:                                           # noqa: BLE001
        return []


def after_line(kind, command, names):
    """The After command: the persona's first featured one that is not the Next command, else the
    default for this kind. Its words are the command's own `does` text."""
    cmd = None
    if names:
        try:
            import gt_persona
            for f in gt_persona.featured_skills(names):
                if f.split()[0] != command.split()[0]:
                    cmd = f
                    break
        except Exception:                                       # noqa: BLE001
            pass
    cmd = cmd or DEFAULT_AFTER.get(kind)
    if not cmd:
        return None
    does = ""
    try:
        import gt_hints
        block = gt_hints.catalog().get(cmd)
        if not block:                                           # a bare skill name: any block of it
            block = next((b for c, b in gt_hints.catalog().items() if c.split()[0] == cmd.split()[0]), None)
        if block:
            does = " -- " + gt_hints._soft(gt_hints._first(block["does"], 90))
    except Exception:                                           # noqa: BLE001
        pass
    return cmd + does


def render(r, after, note=None):
    lines = ["Next:  %s -- %s" % (r["action"], r["command"]), "Why:   %s" % r["why"]]
    if after:
        lines.append("After: %s" % after)
    if note:
        lines.append("Note:  %s" % note)
    return "\n".join(lines) + "\n"


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description="the one best next action, from the vault")
    ap.add_argument("--vault")
    ap.add_argument("--persona", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--outlook", action="store_true", help="also read open Outlook To Do tasks (read-only)")
    ap.add_argument("--outlook-connection", default="m365@personal")
    ap.add_argument("--outlook-secret-file")
    a = ap.parse_args(argv)
    today = date_of(a.today) or datetime.date.today()
    extra, note = [], None
    if client is not None or a.outlook or lotr_home().is_dir():
        c, note = (client, None) if client is not None else default_client(a.outlook_secret_file)
        if c is not None:
            o = outlook_tasks(c, today, a.outlook_connection)
            extra, note = o["tasks"], o["note"]
    r = choose(vault_path(a.vault), today, extra)
    after = None if r["kind"] == "init" else after_line(r["kind"], r["command"], persona_names(a.persona))
    if a.json:
        print(json.dumps(dict(r, after=after, note=note)))
    else:
        sys.stdout.write(render(r, after, note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
