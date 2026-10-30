"""`lotr setup <template>`: the execution run (0.5.0).

A template (a research run's output, or one you wrote) says HOW to reach a service. `setup` turns
it into a working, tested connection in as few steps as the service allows:

  check the template -> check the runtime -> make sure the credential is where the template says
  (or tell the user exactly where to put it) -> add the connection -> smoke test -> say in plain
  words what happened and what is left.

This module holds the parts that are not the CLI: secret presence, the smoke test, and the report.
The orchestration (it calls the add-* / connect commands) lives in lotr.py so it reuses their
validation instead of copying it. Nothing here reads, prints or returns a secret value: it asks
whether a reference resolves, never what it resolves to, and a hidden prompt writes straight to
the store.
"""
import os
import re
import sys

from .errors import GatewayError
from .util import has_terminal

STATUSES = ("done", "needs_input", "needs_credential", "needs_terminal", "needs_runtime", "needs_confirmation",
            "template_not_ready", "failed")
_PLACEHOLDER = re.compile(r"<[^>]*>|\bTODO\b")


def person_terminal():
    """True when a person is plausibly typing here: a terminal on stdin and not inside Claude Code.
    (The OAuth sign-in applies the stricter process-tree gate itself; this only decides whether to
    offer a hidden prompt.)"""
    return has_terminal(sys.stdin) and not os.environ.get("CLAUDECODE")


def identity_for(tpl, override=None):
    """The identity to record: the user's, else the template's unless it is still a placeholder."""
    if override:
        return override
    ident = (tpl.get("connect") or {}).get("identity")
    if isinstance(ident, str) and ident.strip() and not _PLACEHOLDER.search(ident):
        return ident
    return f"{tpl['name']} account"


def secret_state(ref):
    """'present' | 'missing' | 'unknown'. Only stats a file / asks the broker; never reads a value."""
    from . import secrets as secrets_mod
    try:
        d = secrets_mod.describe(ref)
    except GatewayError:
        return "missing"
    p = d.get("present")
    if p is True:
        return "present"
    if p is False:
        return "missing"
    return "unknown"


# The built-in connectors (0.5.0 review). Their REST profiles need no template; their hosted MCP
# servers come as catalog templates (a named service never ships in the module). Someone who types
# `lotr setup jira` with neither in place is told both routes instead of only "no template".
_BUILTIN = {
    "github": ("github", "GitHub"),
    "jira": ("jira-v3", "Jira"),
    "atlassian": ("jira-v3", "Jira"),
    "m365": ("graph", "Microsoft 365"),
    "microsoft365": ("graph", "Microsoft 365"),
    "graph": ("graph", "Microsoft 365"),
    "outlook": ("graph", "Microsoft 365"),
}


def builtin_hints(name):
    """Plain-words next steps for a built-in connector that has no template here yet; [] otherwise."""
    hit = _BUILTIN.get(str(name or "").strip().lower())
    if not hit:
        return []
    profile, title = hit
    out = [f"{title} is a built-in connector.",           # lines stay within 80 columns
           "A hosted MCP template for it, if the vendor has a server, is in the catalog",
           "(lotr template list; lotr template update refreshes it)"]
    if profile.startswith("jira"):
        out.append("in the catalog, Jira is in the template named atlassian: lotr setup atlassian")
    out.append(f"the REST connection needs no template: lotr add-http ID --profile {profile} ...")
    if profile.startswith("jira"):
        out.append("(profile jira-v3 is Jira Cloud, jira-v2 is Jira Data Center)")
    return out


def put_lines(ref, label):
    """What the user does to put the secret at `ref` -- in their own terminal, not in Claude."""
    scheme, _, target = ref.partition(":")
    out = [f"{label} is not there yet. LOTR looks for it at {ref}."]
    if scheme == "store":
        out += [f"In YOUR OWN terminal (not in Claude), run:   lotr secret put {target}",
                "It asks for the value with nothing shown on screen, and stores it for LOTR only.",
                "Or just run this setup again in your own terminal: it will ask."]
    elif scheme == "keychain":
        svc, _, acct = target.partition("/")
        out += [f"In your own terminal run:   security add-generic-password -s {svc} -a "
                f"{acct or '$USER'} -w",
                "It asks for the value without showing it."]
    elif scheme == "file":
        out += [f"Create that file yourself with the value in it and mode 600 (chmod 600 "
                f"{target.split('#')[0]})."]
    else:
        out += ["Put it in that store yourself, the way your secrets tool does."]
    out.append("Never paste the value into Claude; LOTR only needs the reference.")
    return out


def pick_smoke(tpl, entry):
    """-> (op, args) of the one harmless read to try, or (None, why)."""
    sm = tpl.get("smoke")
    if sm:
        return sm["op"], dict(sm.get("args") or {})
    if entry and entry.get("kind") == "mcp":
        from . import profiles
        for t in entry.get("tools") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            if profiles.mcp_tier(t, strict=True) != "read":
                continue
            if (t.get("inputSchema") or {}).get("required"):
                continue
            return t["name"], {}
    return None, ("no read the connection can make without arguments is known; add a "
                  '"smoke": {"op": "...", "args": {...}} entry to the template to enable this check')


def _row(eng, cid, check):
    st = eng.status(check=check)
    return next((c for c in st.get("connections", []) if c.get("id") == cid), None)


_SETTING_HINT = re.compile(r"`(gt_settings\.py set \w+ on)`")


def settings_hint(tpl):
    """-> the gt setting a template's notes say to turn on first (`gt_settings.py set vault_mcp on`), or
    None. A smoke read that fails for want of that setting prints it (phase 69)."""
    for note in tpl.get("notes") or []:
        m = _SETTING_HINT.search(note) if isinstance(note, str) else None
        if m:
            return m.group(1)
    return None


def smoke(home, cid, tpl):
    """The three checks a person would do by hand, run in-process against the same engine the
    daemon uses: status (is it healthy; an MCP server is asked for its tool list), find (is it in
    the catalog, what does it offer), one read-tier call. The result of the read is NEVER
    returned or printed, only that it answered and how big it was.
    -> {'ok': bool, 'lines': [...], 'ops': n, 'read': {...}|None, 'state': str}"""
    from .engine import Engine
    from . import profiles
    eng = Engine(home)
    lines, ok = [], True
    row = _row(eng, cid, True)
    if row is None:
        return {"ok": False, "lines": [f"status: {cid} is not in the registry"], "ops": 0,
                "read": None, "state": None}
    state = row.get("state")
    if state in ("error", "needs sign-in", "disabled"):
        ok = False
        lines.append(f"status: {state}" + (f" ({row['last_error'].get('code')})"
                                           if isinstance(row.get("last_error"), dict) else ""))
        if row.get("fix"):
            lines.append(f"fix: {row['fix']}")
    else:
        lines.append(f"status: {state}")
    cat = (eng.find("", connection=cid).get("results") or [])
    try:
        entry = eng.registry.connection(cid)
        n_ops = len(profiles.for_connection(entry).get("ops") or [])
    except GatewayError:
        entry, n_ops = None, 0
    if not cat:
        ok = False
        lines.append("find: the connection did not appear in the catalog")
    else:
        lines.append("find: listed in the catalog, " + (
            f"{n_ops} operation(s) offered" if n_ops else "no named operations (raw calls only)"))
    op, args_or_why = pick_smoke(tpl, entry)
    read = None
    if op is None:
        lines.append(f"read: skipped, {args_or_why}")
    elif not ok:
        lines.append(f"read: not tried, the connection is not healthy")
    else:
        try:
            r = eng.call("call_read", cid, op, args_or_why)
        except GatewayError as e:
            r = {"ok": False, "error": e.to_dict()}
        if r.get("ok") is False:
            err = r.get("error") or {}
            if err.get("code") == "wrong_tool":
                lines.append(f"read: skipped, {op} is not a read-tier operation here")
            else:
                ok = False
                lines.append(f"read: {op} failed: {err.get('code')}: {str(err.get('message'))[:200]}")
                for h in (err.get("hints") or [])[:2]:
                    lines.append(f"  hint: {h}")
                want = settings_hint(tpl)
                if want:
                    lines.append(f"  hint: if its tools are switched off, turn them on: {want}")
        else:
            size = len(repr(r.get("data")))
            read = {"op": op, "chars": size}
            lines.append(f"read: {op} answered ({size} characters, not shown)")
    final = _row(eng, cid, False)
    return {"ok": ok, "lines": lines, "ops": n_ops, "read": read,
            "state": (final or {}).get("state", state)}


class Report:
    """Collects the steps and renders them as plain words (ASCII only: a Windows console is fine)."""

    def __init__(self, title):
        self.title = title
        self.steps = []          # (mark, name, text)
        self.after = []          # lines under "What to do next"
        self.status = "done"

    def ok(self, name, text):
        self.steps.append(("ok", name, text))

    def todo(self, name, text):
        self.steps.append(("todo", name, text))

    def warn(self, name, text):
        self.steps.append(("note", name, text))

    def stop(self, status, name, text, next_lines=()):
        assert status in STATUSES
        self.status = status
        self.steps.append(("stop" if status in ("failed", "template_not_ready") else "todo", name, text))
        self.after.extend(next_lines)

    def render(self):
        out = [self.title, ""]
        for mark, name, text in self.steps:
            tag = {"ok": "[ok]  ", "todo": "[todo]", "stop": "[stop]", "note": "[note]"}[mark]
            first, *rest = text.split("\n")
            out.append(f"  {tag} {name:<11} {first}")
            out += [f"         {' ' * 11} {r}" for r in rest]
        out.append("")
        out.append({
            "done": "Result: set up and working.",
            "needs_input": "Result: not finished yet. It needs a value from you (see above).",
            "needs_credential": "Result: not finished yet. It needs the credential first (see above).",
            "needs_terminal": "Result: not finished yet. This step must be run by you, in your own terminal.",
            "needs_runtime": "Result: not finished yet. A program this server needs is missing.",
            "needs_confirmation": "Result: waiting for your yes before it runs a program on this machine.",
            "template_not_ready": "Result: the template is not ready to use.",
            "failed": "Result: it did not work. Nothing half-finished was left behind.",
        }[self.status])
        if self.after:
            out.append("")
            out.append("What to do next:")
            out += [f"  {a}" for a in self.after]
        return "\n".join(out) + "\n"

    def to_dict(self):
        return {"ok": self.status == "done", "status": self.status,
                "steps": [{"mark": m, "step": n, "text": t} for m, n, t in self.steps],
                "next": list(self.after)}
