"""`lotr setup-all` (gt-lotr 0.5.1, plan phase 105, N43): one pass over every MCP server this machine
has configured, for Claude Code and the desktop apps (Claude Desktop, Cursor).

It reuses `pull`'s read-only scan, turns each candidate into ONE planned action, and shows the plan
as a table: name, transport, what LOTR would create, whether it is connected already. Nothing is
added unless the caller applies the plan after an explicit confirm; the CLI runs each action through
the existing add-mcp / add-stdio machinery, so every gate those have stays.

What a plan never holds: a credential value. It is built from `pull`'s report, which keeps names
only (header names, environment variable NAMES, a URL without its user info or query). A server
that needs a key, a sign-in or an environment secret is NOT added: it is skipped and the exact
command for the owner's own terminal is listed (`secret put`, `connect`).
"""
from . import conn_stdio, pull

ACTIONS = ("add_http_ref", "add_stdio_template", "add_stdio", "trust", "skip")


def _first(notes, default):
    return (notes or [default])[0]


def _row(c, action, creates, state, reason=None):
    return {"name": c["name"], "id": c["id"], "transport": c["transport"], "scopes": c.get("scopes", []),
            "status": c["status"], "action": action, "creates": creates, "state": state,
            "reason": reason, "commands": list(c["commands"] if action == "skip" else [])}


def plan(home, report):
    """report (from pull.scan) -> {"rows": [...], "counts": {...}}. Each row has an `action`; only
    the rows whose action is not "skip" change anything when applied. Also carries, per row, the
    private `_args` an executor needs (names, URLs and references, never a value)."""
    templates, _ = pull.load_templates(home)
    rows = []
    for c in report["candidates"]:
        st = c["status"]
        if st == "already_connected":
            cmd = c.get("command")
            if c["transport"] == "stdio" and cmd and not conn_stdio.is_approved(home, c["id"], cmd):
                r = _row(c, "trust", "record its command as approved (phase 112)", "connected, not yet trusted")
                r["_args"] = {"command": list(cmd)}
            else:
                r = _row(c, "skip", "-", "already connected", "nothing to do")
        elif st == "ready_to_import":
            r = _row(c, "add_http_ref", "http connection, token by reference to %s" % c["token"]["ref"],
                     "not connected")
            r["_args"] = {"endpoint": c["url"], "identity": c["identity"], "ref": c["token"]["ref"]}
        elif st == "needs_setup" and c["transport"] == "stdio":
            argv = c["command"]
            tpl, params = pull.match_stdio(home, templates, argv)
            if c["env_names"]:
                r = _row(c, "skip", "stdio connection%s" % (" from the %s template" % c["template"]
                                                            if c["template"] else ""),
                         "not connected",
                         "needs a secret for %s: not copied; put each in the store from your own terminal"
                         % ", ".join(c["env_names"]))
            elif c["template"] and tpl:
                r = _row(c, "add_stdio_template", "stdio connection from the %s template" % c["template"],
                         "not connected")
                r["_args"] = {"template": c["template"], "params": dict(params or {}), "command": list(argv),
                              "identity": c["identity"]}
            else:
                r = _row(c, "add_stdio", "stdio entry that runs: " + pull.join_command(argv), "not connected")
                r["_args"] = {"command": list(argv), "identity": c["identity"]}
        elif st in ("needs_key", "needs_sign_in", "needs_setup"):
            what = "needs a key" if st == "needs_key" else "needs a sign-in" if st == "needs_sign_in" else "needs setup"
            r = _row(c, "skip", ("%s template" % c["template"]) if c["template"] else "http connection",
                     "not connected", "%s: yours to do in your own terminal; the commands are listed" % what)
        else:
            r = _row(c, "skip", "-", "not connected", _first(c["notes"], pull.STATUS_WORDS.get(st, st)))
        rows.append(r)
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {"rows": rows, "counts": counts}


def public(p):
    """The plan without the executor's private fields."""
    return {"rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in p["rows"]],
            "counts": p["counts"]}


def select(p, names):
    """Narrow a plan to the servers named (name or id); an unknown name is an error, never ignored."""
    from .errors import GatewayError
    keep = []
    for n in names:
        hits = [r for r in p["rows"] if n in (r["name"], r["id"])]
        if not hits:
            raise GatewayError("setup_all_unknown", "no MCP server named %s was found" % pull.oauth.clean_line(repr(n), 80),
                               hints=["see them: lotr setup-all", "names: " + ", ".join(r["name"] for r in p["rows"][:20])])
        keep += [r for r in hits if r not in keep]
    return {"rows": keep, "counts": _counts(keep)}


def _counts(rows):
    out = {}
    for r in rows:
        out[r["action"]] = out.get(r["action"], 0) + 1
    return out


def todo(p):
    return [r for r in p["rows"] if r["action"] != "skip"]


def render_table(report, p, applied=None):
    out = ["MCP servers on this machine: Claude Code and desktop apps (gateway zone %s)" % report["zone"],
           pull._wrapped("looked in: ", ", ".join("%s (%s)" % (s["path"], "found" if s["found"] else "not there")
                                                for s in report["sources"]))]
    if not p["rows"]:
        out += ["", "none found: nothing is configured in the files above."]
    else:
        out += ["", "  %s %s %s %s" % (pull._cell("server", 16), pull._cell("transport", 9),
                                       pull._cell("state", 24), "LOTR would create")]
        for r in p["rows"]:
            out.append("  %s %s %s %s" % (pull._cell(r["name"], 16), pull._cell(r["transport"], 9),
                                          pull._cell(r["state"], 24), r["creates"]))
        for r in p["rows"]:
            if r["action"] == "skip" and (r["reason"] and r["reason"] != "nothing to do" or r["commands"]):
                out += ["", pull._wrapped("%s: " % r["name"], r["reason"] or "skipped")]
                for cmd in r["commands"]:
                    out.append("    " + cmd)
    for prob in report["problems"]:
        out.append(pull._wrapped("problem: ", prob))
    if applied is None:
        n = len(todo(p))
        out += ["", pull._wrapped("", "Nothing was changed. %d of %d would be added or trusted; the rest are "
                                 "listed above with the command for your own terminal." % (n, len(p["rows"]))),
                "  to apply: lotr setup-all --apply   (asks first)"]
    else:
        for k in ("done", "skipped", "failed"):
            out += ["", "%s (%d)" % (k, len(applied[k]))]
            for e in applied[k]:
                out.append(pull._wrapped("  %s: " % e["name"], e.get("detail") or e.get("error", {}).get("message") or "-"))
    return "\n".join(out) + "\n"
