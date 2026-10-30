"""`lotr onboard NAME --url URL | --package SPEC`: from "I have this MCP" to a checked template (gt-lotr 0.5.1, plan phase 38).

    build(home, name, target, net=None, force=False, today=None) -> {"status", "name", "archetype", "probe", "wrote",
                                                                    "facts_file", "problems", "facts_problems", "next"}

The probe (onboard_probe) names the archetype; this module copies that archetype under the new name and fills in ONLY
what the probe observed: the endpoint, its host, the auth scheme, the scopes the server advertises, or the launcher
and package. The vendor's free text is never copied. What cannot be observed stays a TODO and the result says so.
It also writes the fact sheet (NAME.facts.json) beside the template: the source of each fact is the endpoint itself,
with the probe's verification state, so `lotr template facts-check` reads it like any research run's sheet.

It touches no credential: the template holds a REFERENCE (`store:NAME-key`), the fact sheet holds none, and nothing here
reads the store. The stage after this one is the existing `lotr setup`, which asks where the credential goes and
signs a person in only in their own terminal.

Statuses: template_ready | template_not_ready | template_exists | no_template_fits | refused | unreachable.
"""
import json
import re
import urllib.parse
from datetime import date
from pathlib import Path

from . import conn_templates as ct
from . import facts, onboard_probe, shaping, template_tool
from .errors import GatewayError
from .util import atomic_write

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
RESERVED = ("discover", "agent", "compare", "tune")      # `lotr onboard <word>` is a sub-command, not a template name


def secret_in(text):
    """-> a rule id when `text` holds a credential-shaped string, else None (never the string)."""
    found = shaping.scan_credentials(text)
    return found[0]["rule"] if found else None


def _host(probe):
    obs = probe.get("observed") or {}
    url = obs.get("url") or ""
    h = urllib.parse.urlsplit(url).hostname if url else None
    if h:
        return h
    hosts = obs.get("hosts") or []
    return hosts[0].split(":")[0] if hosts else None


def fill_template(name, probe):
    """The picked archetype, copied under `name`, with only the probe's observations filled in."""
    best = probe["best"]
    tpl = template_tool.new_template(name, None, archetype=best)
    obs = probe.get("observed") or {}
    c = tpl["connect"]
    tpl.pop("docs", None)
    tpl.pop("smoke", None)                          # a smoke tool needs a signed-in listing: a later phase
    host = _host(probe)
    if probe.get("kind") == "url":
        url = obs["url"]
        if best in ("mcp-oauth", "mcp-oauth-preregistered", "mcp-http-key"):
            c["url"] = url
            tpl["network"] = {"hosts": [host]}
            tpl["summary"] = "MCP server at %s" % host
            c["identity"] = "your account at %s" % host
            c["description"] = "%s (hosted MCP)" % host
            if best != "mcp-http-key":
                scopes = obs.get("scopes") or []
                if scopes:
                    c["scopes"] = list(scopes)
            else:
                scheme = obs.get("auth_scheme") or "bearer"
                c["auth_scheme"] = scheme
                if scheme == "none":
                    c.pop("credential_ref", None)
        elif best == "sse-bridge":
            c["command"] = [url if str(x).startswith("https://todo-host") else x for x in c["command"]]
            tpl["summary"] = "Legacy SSE MCP server at %s" % host
            c["identity"] = "your account at %s" % host
            c["description"] = "%s (legacy SSE via mcp-remote)" % host
    else:
        pkg, command = obs.get("package"), obs.get("command")
        label = pkg or "the downloaded program"
        tpl["summary"] = "Local MCP server %s" % label
        c["identity"] = "you"
        c["description"] = "%s (%s)" % (label, command)
        c["env_refs"] = {}
        if best == "stdio-npx":
            c["command"] = ["npx", "-y", pkg]
        elif best == "stdio-uvx":
            c["command"] = ["uvx", pkg]
        elif best == "stdio-docker":
            c["command"] = ["docker", "run", "-i", "--rm", pkg]
        tpl["notes"] = tpl.get("notes", []) + [
            "If the program needs a key, add NAME: store:secret-name under connect.env_refs; the secret lives there."]
    tpl["notes"] = [n for n in tpl.get("notes", []) if not n.startswith("Started from")] + [
        "Drafted by `lotr onboard` from what the server answered; run `lotr setup %s` in your own terminal." % name]
    return tpl


def _fact(i, field, value, claim, source, today, state, evidence):
    return {"id": "f%d" % i, "field": field, "value": value, "claim": claim, "source": source, "retrieved": today,
            "state": state, "evidence": evidence[:380]}


def fact_sheet(name, probe, tpl, today):
    best = probe["best"]
    cand = (probe.get("candidates") or [{}])[0]
    state = cand.get("state") if cand.get("state") in facts.STATES else "unverified"
    ev = "; ".join((cand.get("evidence") or probe.get("evidence") or [])[:3]) or "observed by lotr onboard"
    obs = probe.get("observed") or {}
    url = obs.get("url")
    out, i = [], 0
    opens = ["Nothing about the account, plan or permissions was checked: no credential was sent.",
             "Tools were not listed: that needs a signed-in connection (lotr setup, then lotr onboard discover)."]
    if probe.get("kind") == "url":
        i += 1
        out.append(_fact(i, "connect.url", tpl["connect"].get("url"), "the endpoint answered an MCP probe as a %s server" % best,
                         url, today, state, ev))
        i += 1
        out.append(_fact(i, "network.hosts", tpl["network"]["hosts"], "the endpoint is served from this host", url, today,
                         state, "the host of the URL the probe was given"))
        if best == "mcp-http-key":
            i += 1
            out.append(_fact(i, "connect.auth_scheme", tpl["connect"]["auth_scheme"],
                             "the server's authentication is %s" % tpl["connect"]["auth_scheme"], url, today, state, ev))
        if best.startswith("mcp-oauth") and tpl["connect"].get("scopes"):
            i += 1
            out.append(_fact(i, "connect.scopes", tpl["connect"]["scopes"],
                             "the protected-resource document advertises these scopes", url, today, state, ev))
        if best == "mcp-oauth-preregistered":
            opens.append("The server advertises no registration endpoint: create an OAuth app and set connect.app_setup_url.")
        if best == "mcp-oauth" and not obs.get("registration_endpoint"):
            opens.append("The authorization server's metadata was not read, so dynamic registration is unconfirmed.")
    else:
        pkg = obs.get("package")
        page = None
        if best == "stdio-npx" and pkg:
            page = "https://www.npmjs.com/package/%s" % re.sub(r"(?<=.)@[^@/]+$", "", pkg)
        elif best == "stdio-uvx" and pkg:
            page = "https://pypi.org/project/%s/" % re.split(r"==|@", pkg)[0]
        if page:
            i += 1
            out.append(_fact(i, "connect.command", tpl["connect"]["command"],
                             "the package was named by the person running onboard", page, today, "unverified",
                             "the registry was not queried; the name was typed by the person"))
        opens.append("The package was not looked up or run: pin the version and read its page before the first run.")
    if not out:
        opens.append("No source page could be cited for this archetype, so the fact sheet holds no fact: "
                     "`lotr template facts-check` will say so until you add one.")
    return {"schema": 1, "template": name, "service": _host(probe) or obs.get("package") or name, "archetype": best,
            "researched": today, "researcher": "lotr onboard (probe)", "facts": out,
            "choices": [{"decision": "archetype %s" % best,
                         "because": "the probe's evidence: %s" % ev[:300], "facts": [f["id"] for f in out]}],
            "open": opens}


def _next(name, target_file, problems, ready):
    if ready:
        return ["lotr setup %s     (in your own terminal: it checks the runtime, asks where the credential goes, "
                "signs you in if needed, adds the connection and tests it)" % name]
    return ["edit it: vim %s" % target_file, "check it: lotr template check %s" % name,
            "then: lotr setup %s" % name]


_WORDS = {"template_ready": "the template is written and checked",
          "template_not_ready": "the template is written but not finished",
          "template_exists": "a template of that name already exists",
          "no_template_fits": "no template fits this MCP",
          "refused": "the target was refused",
          "unreachable": "the server could not be reached"}


def render(res):
    """Plain words for a person: what was found, what was written, what is left."""
    out = ["Onboarding %s: %s" % (res["name"], _WORDS.get(res["status"], res["status"]))]
    if res.get("archetype"):
        out.append("  archetype: %s (%d request%s sent)" % (
            res["archetype"], res["probe"]["requests"], "" if res["probe"]["requests"] == 1 else "s"))
    for e in res["probe"].get("evidence", [])[:6]:
        out.append("  saw: " + e)
    if res["probe"].get("note") and res["status"] in ("no_template_fits", "refused", "unreachable"):
        out.append("  " + res["probe"]["note"])
    if res.get("wrote"):
        out.append("  wrote: %s" % res["wrote"])
        out.append("  facts: %s%s" % (res["facts_file"], "" if not res["facts_problems"] else
                                      " (facts-check: %s)" % "; ".join(res["facts_problems"][:2])))
    for p in res.get("problems", [])[:8]:
        out.append("  left to do: " + p)
    for n in res.get("next", []):
        out.append("  next: " + n)
    return "\n".join(out) + "\n"


def build(home, name, target, *, net=None, force=False, today=None, probe_fn=None):
    if not isinstance(name, str) or not _NAME.match(name) or name in RESERVED:
        raise GatewayError("template_invalid", "the template name must be a lowercase id like 'acme' (letters, digits, - _ .)")
    probe = (probe_fn or onboard_probe.probe)(target, net=net)
    res = {"name": name, "status": probe["status"], "archetype": probe.get("best"), "wrote": None, "facts_file": None,
           "problems": [], "facts_problems": [], "next": [],
           "probe": {"best": probe.get("best"), "requests": probe.get("requests", 0), "evidence": probe.get("evidence", []),
                     "candidates": probe.get("candidates", []), "note": probe.get("note", "")}}
    if probe["status"] != "ok" or not probe.get("best"):
        res["status"] = probe["status"] if probe["status"] != "ok" else "no_template_fits"
        res["next"] = [probe.get("note") or onboard_probe.NO_FIT_NOTE]
        return res
    try:
        ct.get_template(home, name)
        exists = True
    except GatewayError as e:
        if e.code != "unknown_template":
            raise
        exists = False
    dest = Path(home) / "connections" / ("%s.json" % name)
    if (exists or dest.exists()) and not force:
        res["status"] = "template_exists"
        res["next"] = ["a template named %s already exists; use another name, or pass --force to replace your own copy" % name]
        return res
    tpl = fill_template(name, probe)
    blob = json.dumps(tpl)
    rule = secret_in(blob)
    if rule:
        res["status"], res["next"] = "refused", ["the draft held a credential-shaped string (%s) and was not written" % rule]
        return res
    atomic_write(dest, json.dumps(tpl, indent=2) + "\n", 0o644)
    res["wrote"] = str(dest)
    sheet = fact_sheet(name, probe, tpl, today or date.today().isoformat())
    fdest = Path(home) / "connections" / ("%s.facts.json" % name)
    atomic_write(fdest, json.dumps(sheet, indent=2) + "\n", 0o644)
    res["facts_file"] = str(fdest)
    chk = template_tool.check_file(name, home)
    res["problems"] = list(chk.get("problems", []))
    res["facts_problems"] = list(facts.check(sheet, tpl).get("problems", []))
    ready = bool(chk.get("ok"))
    res["status"] = "template_ready" if ready else "template_not_ready"
    res["next"] = _next(name, dest, res["problems"], ready)
    return res
