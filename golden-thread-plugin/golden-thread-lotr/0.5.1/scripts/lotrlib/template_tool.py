"""`lotr template guide | new | check | list | facts-check` (0.5.0, owner 2026-10-13; archetypes and
fact sheets 0.5.0): author your own connection template from an API's documentation without
reading gt's code.

A template is a small JSON file in `~/.config/gt-lotr/<zone>/connections/NAME.json` (yours wins by
name over the shipped and catalog ones). It holds NO secret. It pre-fills `lotr connect|add-http|
add-stdio|add-mcp NAME --from-template T`, and `lotr setup NAME` runs the whole thing. This module
only writes and checks such files; it never talks to the network and never reads a credential.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import conn_templates as ct
from . import facts as facts_mod
from . import runtimes
from .errors import GatewayError
from .util import atomic_write

TODO = "TODO"

GUIDE = """\
LOTR connection templates: how they work
========================================

A template is a JSON file that remembers how to reach one service, so that
connecting it is one command: say "connect Linear", paste a key or sign in,
done. It is a form with the boxes filled in. It holds NO secret: only the
NAME of where a secret lives (a reference), never the secret itself. A file
with a token, password or key in it is refused.

The two runs (how a service gets its template)
  research run   a session reads the vendor's documentation, picks an archetype
                 (below), fills the template, and writes a fact sheet that gives
                 the source page and the verification state of every fact.
                 Skill: /gt-lotr:gt-lotr-research.  Never touches a credential.
  execution run  lotr setup NAME   checks the template and the runtime, tells you
                 where the credential goes (or signs you in, in YOUR terminal),
                 adds the connection and tests it. Safe to run again.

Where templates live (later wins by name)
  <plugin>/templates/connections/NAME.json        shipped with gt-lotr (archetypes)
  <plugin>/templates/catalog/templates/           the catalog, included
  ~/.config/gt-lotr/<zone>/catalog/templates/     fetched by: lotr template update
  ~/.config/gt-lotr/<zone>/connections/NAME.json  yours
  The included catalog works with no network. A fetched catalog wins over it only
  when its catalog_version is higher; both are checked against their hashes.

Which archetype? Answer in order and stop at the first yes
  1  Hosted MCP URL, you sign in on a web page, nothing to create first
                                                  mcp-oauth                kind mcp-oauth
  2  Hosted MCP URL, but you must create an OAuth app and get a client id
                                                  mcp-oauth-preregistered  kind mcp-oauth
  3  Hosted MCP URL, you paste an API key or token
                                                  mcp-http-key             kind mcp-http
  4  Program started with npx (Node)              stdio-npx                kind mcp-stdio
  5  Program started with uvx (Python)            stdio-uvx                kind mcp-stdio
  6  Program run as a docker image                stdio-docker             kind mcp-stdio
  7  Program you download (a binary)              stdio-binary             kind mcp-stdio
  8  Old HTTP+SSE MCP URL only (no streamable)    sse-bridge               kind mcp-stdio
  9  No MCP server, just a REST API               http-rest                kind http
  Start one:  lotr template new acme --archetype mcp-http-key
  Prefer 1-3 (hosted) over 4-7 (code runs on your machine) over 8 (token kept outside
  LOTR's store) over 9 (you choose every operation yourself).

Using a template by hand
  lotr setup NAME [--id name@zone]               the whole thing (recommended)
  lotr connect NAME --from-template T            kind mcp-oauth (your own terminal)
  lotr add-mcp ID --from-template T              kind mcp-http
  lotr add-http ID --from-template T             kind http
  lotr add-stdio ID --from-template T            kind mcp-stdio
Explicit flags always win over the template.

The four kinds (field "kind") and their connect.* fields
  http        a REST API. base_url, profile (generic unless github, jira-v2, jira-v3
              or graph), auth_scheme (bearer | basic | header | none), auth_header
              (for header), auth_user (for basic), credential_ref (a SUGGESTED
              reference, see below). Optional: health ("GET /health": what
              `lotr status --check` asks), locality (local | remote: a local
              engine is labelled as one), provider (a short label such as "acme-cloud", shown in
              status and describe). For a model server behind it: timeouts
              ({"call_s": 120}: seconds a reply may take, 20 if absent), limits
              ({"max_calls_per_min": 30, "max_concurrent": 1}: the gateway's own brake,
              off if absent) and llm ({"tools": "none" | "read" | "write"}: what a model
              op can trigger; every op on the connection is at least that tier).
  mcp-oauth   a hosted MCP server with an OAuth sign-in. url (https), scopes, trust,
              offline_access, send_resource, skip_audience_check, redirect_host.
              client_registration: "auto" (the default: LOTR registers itself) or
              "preregistered" (you created an app): then client_secret_required
              (true/false), app_setup_url (where you create it) and redirect_port (the
              loopback port to register as http://127.0.0.1:PORT/callback). The client
              id and secret are asked for at connect time as references, never stored
              here. auth_params: extra sign-in parameters from a short allow-list
              ({"access_type": "offline", "prompt": "consent"} for Google; also
              include_granted_scopes). Nothing secret, nothing LOTR sets itself.
  mcp-http    a hosted MCP server with a static key. url, auth_scheme (bearer |
              header | prefix | none), auth_header (for header), auth_prefix (for
              prefix: the text before the key, "Token token=" sends
              Authorization: Token token=KEY), credential_ref. Optional: limits, llm
              (as for http, above).
  mcp-stdio   a local program LOTR starts and talks to over stdin/stdout. command is a
              LIST: ["npx", "-y", "some-mcp-server@1.2.3"]. runtime (node | uv | docker
              | binary | python): checked before launch, with how to install it.
              env_refs: VARIABLE -> reference (secrets). env: VARIABLE -> plain value
              (a region, a URL; a value that looks like a secret is refused). cwd,
              timeouts (start_s, call_s, idle_s). bridge: "sse" marks a legacy SSE
              bridge. params: values only you can give, written {name} in the command
              or in an env value ({"dir": {"prompt": "Folder it may use", "kind":
              "path"}}); setup asks, or pass --param dir=/path. Not for secrets (never
              in env_refs). No shell is used, so no pipes or quoting. Optional: limits,
              llm (as for http, above).

Fields every template has
  schema   always 1            name      lowercase id, e.g. "acme"
  summary  one line            kind      one of the four above
  connect  the kind's fields, plus optional trust (T0 | T1 | T2), identity (who you
           are there), description, policy (below)
  network  {"hosts": ["api.acme.example"]}: the only hosts it may reach
  notes    list of strings, shown when the template is used
  archetype  which row of the table above it started from (optional)
  docs     https URL of the vendor's own documentation (optional)
  smoke    {"op": "...", "args": {...}}: the one harmless read `lotr setup` makes to
           prove it works (optional; without it setup picks a read that needs no input)

A service that hands each call to one of several backends (optional, any kind)
  Say so and LOTR remembers which backend failed, from real calls only (nothing is
  pinged): health_tracking true, sub_endpoints [{"id": "db-agent", "description":
  "...", "ops": ["POST /db"]}], sub_endpoint_arg (the argument whose value names the
  backend) and health_expires_h (hours a record lasts, 24 if absent). `lotr status`
  lists each backend; `lotr read ID endpoint_health` returns the records;
  `lotr reset-health ID [SUB]` clears them.

Secrets are references
  A reference says where to look: store:NAME (LOTR's secret store, a file in
  ~/.secrets), keychain:SERVICE/ACCOUNT, file:/abs/path#field, env:NAME. You put the
  secret there yourself, once, in your own terminal:  lotr secret put NAME
  LOTR reads it when it needs it and never prints it. credential_ref and env_refs in a
  template are only suggestions of those names. Never paste a key into Claude.

Operation tiers (what asking-first means)
  Every operation is read, write or consent, decided by the gateway, never by the caller:
    read     looks only (GET, or a tool named get_/list_/search..., or getX/listX)
    write    changes something (POST, PUT, PATCH, a tool not known to be safe)
    consent  hard to undo (DELETE, send, merge; a tool with a risky name):
             asks you first with Touch ID / Windows Hello
  Anything unknown is a write. Pin your own rule per connection with
  connect.policy: {"deny": [...], "consent": [...], "write": [...], "read": [...]}
  as globs matched against the op name and "METHOD /path", for example
  "consent": ["DELETE *", "POST /v1/charges*"]. deny wins, then consent, write, read.

Trust, honestly
  A template cannot hold a secret, but it can name a program to run (stdio) or a host to
  talk to. So `lotr setup` shows you the exact command before it runs one and waits for
  your yes. Catalog templates (included with gt-lotr, or fetched by `lotr template update`
  from a public repository) are checked against a published hash, which catches damage,
  not a bad publisher: read what setup shows you. The sse-bridge archetype keeps its
  token OUTSIDE LOTR's store.

Making one from an API's documentation
  1. lotr template new acme --archetype http-rest --from-doc acme-docs.txt
     (save the docs page to a text file first; lotr does not fetch pages) fills in what
     it can see and marks the rest TODO. Or let a session do it: /gt-lotr:gt-lotr-research.
  2. Open the file, fix every TODO, add policy for the dangerous endpoints.
  3. lotr template check acme       says what is still wrong, in plain words.
  4. lotr template facts-check acme.facts.json   checks the fact sheet beside it.
  5. lotr setup acme

Minimal examples (kind http, kind mcp-stdio)
%s

%s
"""

EXAMPLE_HTTP = {
    "schema": 1, "name": "acme", "summary": "Acme billing API (REST, bearer token)",
    "kind": "http",
    "connect": {
        "base_url": "https://api.acme.example/v1", "profile": "generic",
        "auth_scheme": "bearer", "credential_ref": "store:acme-api",
        "trust": "T1", "identity": "<your account at acme>",
        "description": "Acme billing",
        "policy": {"consent": ["DELETE *", "POST /charges*"]},
    },
    "network": {"hosts": ["api.acme.example"]},
    "notes": ["Put the API key in the store yourself: it is never typed into this file."],
}

EXAMPLE_STDIO = {
    "schema": 1, "name": "notes-server", "summary": "A local notes MCP server over stdio",
    "kind": "mcp-stdio",
    "connect": {
        "command": ["npx", "-y", "@example/notes-mcp"],
        "env_refs": {"NOTES_API_KEY": "store:notes-api-key"},
        "trust": "T1", "identity": "<you>", "description": "Notes (local stdio server)",
        "timeouts": {"start_s": 30, "call_s": 120, "idle_s": 600},
    },
    "notes": ["LOTR starts the command on first use, keeps it running, and stops it when idle."],
}


def guide_text():
    return GUIDE % (json.dumps(EXAMPLE_HTTP, indent=2), json.dumps(EXAMPLE_STDIO, indent=2))


# ------------------------------------------------------------------------------ scaffold

_TODO_RX = re.compile(r"(?:^|[^A-Za-z0-9])TODO(?:[^A-Za-z0-9]|$)|todo-host\.example")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_NAME = ct._NAME


def _guess_from_doc(text):
    """What a documentation page lets us see. Deliberately small: a hint, never a decision."""
    found = {}
    low = text.lower()
    # the first API-looking URL (prefer one with api/ v1 in it) is the base URL candidate
    urls = [u.rstrip(".,;") for u in _URL.findall(text)]
    ranked = sorted(urls, key=lambda u: (0 if re.search(r"//api[.-]|/v\d+\b|/api\b", u) else 1))
    for u in ranked:
        p = urlparse(u)
        if p.scheme == "https" and p.hostname:
            m = re.match(r"(https://[^/\s]+(?:/(?:api/)?v\d+)?)", u)
            found["base_url"] = m.group(1) if m else f"https://{p.hostname}"
            break
    m = re.search(r"\b(x-[a-z0-9-]*(?:key|token|auth)[a-z0-9-]*)\b\s*[:=]", low) \
        or re.search(r"\b(x-[a-z0-9-]*(?:key|token|auth)[a-z0-9-]*)\b", low)
    if "bearer" in low:
        found["auth_scheme"] = "bearer"
    elif re.search(r"basic auth|http basic|authorization:\s*basic", low):
        found["auth_scheme"] = "basic"
    elif m:
        found["auth_scheme"] = "header"
        found["auth_header"] = "-".join(w.capitalize() for w in m.group(1).split("-")).replace("Api", "API")
    elif "oauth" in low:
        found["oauth"] = True
    if re.search(r"\bnpx\b|\buvx\b|\bpython3? -m\b|mcp server", low) and "stdio" in low:
        found["stdio"] = True
    return found


def from_archetype(name, archetype, doc_text=None):
    """A copy of a shipped archetype under a new name: refs renamed after the service, the archetype
    recorded, every TODO kept. The archetype's own file is the single source of its shape."""
    if archetype not in ct.ARCHETYPES:
        raise GatewayError("template_invalid", f"--archetype must be one of {', '.join(ct.ARCHETYPES)}")
    src = ct.SHIPPED_CONNECTIONS / f"{archetype}.json"
    try:
        base = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GatewayError("missing_file", f"the shipped archetype {archetype} cannot be read")
    base["name"] = name
    base["archetype"] = archetype
    base["summary"] = f"{TODO}: one line about {name}"
    c = base["connect"]
    ref = f"store:{name}-key"
    if "credential_ref" in c:
        c["credential_ref"] = ref if base["kind"] != "http" else f"store:{name}-api"
    for k in list(c.get("env_refs") or {}):
        c["env_refs"][k] = f"store:{name}-{k.lower().replace('_', '-').replace('todo-', '')}"
    c["description"] = f"{name} ({base['kind']})"
    base["notes"] = [n for n in base.get("notes", []) if not n.startswith("ARCHETYPE:")] + [
        f"Started from the {archetype} archetype. Fix every TODO, then: lotr template check {name}"]
    if doc_text:
        seen = _guess_from_doc(doc_text)
        if base["kind"] == "http" and seen.get("base_url"):
            c["base_url"] = seen["base_url"]
            host = urlparse(c["base_url"]).hostname
            if host:
                base["network"] = {"hosts": [host]}
        base["notes"].append("Drafted from a documentation file: check each value against it.")
    return base


def new_template(name, kind, doc_text=None, archetype=None):
    if not _NAME.match(name or ""):
        raise GatewayError("template_invalid", "name must be a lowercase id like 'acme'")
    if archetype:
        return from_archetype(name, archetype, doc_text)
    if kind not in ct._KIND:
        raise GatewayError("template_invalid", f"--kind must be one of {', '.join(ct._KIND)}")
    if kind == "mcp-http":
        return from_archetype(name, "mcp-http-key", doc_text)
    seen = _guess_from_doc(doc_text) if doc_text else {}
    base = {"schema": 1, "name": name, "summary": f"{TODO}: one line about {name}", "kind": kind}
    if kind == "http":
        url = seen.get("base_url", "https://todo-host.example/v1")
        host = urlparse(url).hostname or "todo-host.example"
        scheme = seen.get("auth_scheme", "bearer")
        c = {"base_url": url, "profile": "generic", "auth_scheme": scheme}
        if scheme == "header":
            c["auth_header"] = seen.get("auth_header", f"{TODO}-Header-Name")
        if scheme == "basic":
            c["auth_user"] = f"{TODO}: the username, or a fixed one the API wants"
        c.update({"credential_ref": f"store:{name}-api", "trust": "T1",
                  "identity": f"{TODO}: your account at {name}", "description": f"{name} API",
                  "policy": {"consent": ["DELETE *"]}})
        base.update(connect=c, network={"hosts": [host]})
        notes = ["Fix every TODO, then run: lotr template check " + name,
                 "Put the secret where credential_ref points yourself; never in this file."]
        if seen.get("oauth"):
            notes.append("The docs mention OAuth: if there is a remote MCP server, use kind "
                         "mcp-oauth instead; for a plain OAuth REST API ask for help.")
    elif kind == "mcp-stdio":
        cmd = [f"{TODO}-program", f"{TODO}-argument"]  # a word-boundary TODO
        base.update(connect={"command": cmd, "env_refs": {}, "trust": "T1",
                              "identity": f"{TODO}: you", "description": f"{name} (stdio)"})
        notes = ["command is a LIST of arguments, one per item; no shell.",
                 "For each environment variable the program needs, add "
                 '"NAME": "store:secret-name" under env_refs (the secret lives there).']
    else:
        host = "todo-host.example"
        base.update(connect={"url": f"https://{host}/mcp", "scopes": ["mcp"],
                             "offline_access": True, "trust": "T1",
                             "identity": f"{TODO}: your account", "description": f"{name} (MCP)"},
                    network={"hosts": [host]})
        notes = ["Run `lotr setup " + name + "` in your own terminal."]
    base["notes"] = notes + (["Drafted from a documentation file: check each value against it."]
                             if doc_text else [])
    return base


def todo_paths(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else k
            if isinstance(k, str) and _TODO_RX.search(k) and not path.startswith("notes"):
                out.append(sub)                       # a placeholder variable / header name
            out += todo_paths(v, sub)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += todo_paths(v, f"{path}[{i}]")
    elif isinstance(obj, str) and _TODO_RX.search(obj) and not path.startswith("notes["):
        out.append(path)
    return out


def write_new(home, name, kind, doc=None, out=None, force=False, archetype=None):
    doc_text = None
    if doc:
        if doc == "-":
            doc_text = sys.stdin.read()
        else:
            p = Path(doc).expanduser()
            try:
                doc_text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise GatewayError("missing_file", f"cannot read {p}: {type(e).__name__}",
                                   hints=["--from-doc takes a text file you saved, or - for stdin; "
                                          "save a web page's text first (lotr does not fetch URLs)"])
        doc_text = doc_text[:2_000_000]
    tpl = new_template(name, kind, doc_text, archetype)
    target = Path(out).expanduser() if out else Path(home) / "connections" / f"{name}.json"
    if target.exists() and not force:
        raise GatewayError("exists", f"{target} already exists",
                           hints=["pass --force to replace it, or choose another name"])
    atomic_write(target, json.dumps(tpl, indent=2) + "\n", 0o644)
    return {"ok": True, "wrote": str(target), "kind": tpl["kind"], "archetype": tpl.get("archetype"),
            "todo": todo_paths(tpl),
            "next": [f"edit it: vim {target}", f"check it: lotr template check {target}"]}


# ------------------------------------------------------------------------------ check / list

def find_template_file(arg, home):
    """A template named by path, or by name across yours, the catalog and the shipped folder."""
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    fn = arg if arg.endswith(".json") else arg + ".json"
    for d in reversed(ct.template_dirs(home)):
        if (Path(d) / fn).is_file() and (Path(d) / fn) in ct.template_files(d, home):
            return Path(d) / fn
    raise GatewayError("missing_file", f"no template file {arg!r}",
                       hints=["give a path, or a name from `lotr template list`"])


def check_file(arg, home):
    """Validate a template given as a path or as a name known to lotr. Problems are plain
    sentences; ok is true only when the file is valid AND has no TODO left."""
    p = find_template_file(arg, home)
    problems, warnings = [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return {"ok": False, "file": str(p), "problems": [f"not valid JSON: {e}"], "todo": []}
    valid = True
    try:
        ct.validate(data, p.name)
    except GatewayError as e:
        valid = False
        problems.append(e.message.split(": ", 1)[-1] if e.message.startswith(p.name) else e.message)
        if e.code == "template_has_secret":
            problems[-1] = e.message
    todo = todo_paths(data)
    for t in todo:
        problems.append(f"{t} still says TODO")
    if isinstance(data, dict) and valid:
        warnings += advice(data)
    return {"ok": not problems, "file": str(p), "problems": problems, "warnings": warnings,
            "todo": todo, "kind": data.get("kind") if isinstance(data, dict) else None,
            "archetype": data.get("archetype") if isinstance(data, dict) else None}


def advice(data):
    """Warnings for a template that is valid: things worth a second look, in plain words."""
    out = []
    c = data.get("connect") or {}
    kind = data.get("kind")
    from . import contracts
    for field, text in (("summary", data.get("summary")), ("connect.description", c.get("description"))):
        if isinstance(text, str) and text.strip() and "TODO" not in text:
            bad = contracts.lint_summary(text, 0)
            if bad:
                out.append(f"{field} reads like an API, not an outcome ({bad[0].split(':')[0]}): say "
                           "what the connection gives you; the model reads it when it chooses where to look")
    if kind == "http":
        if "policy" not in c:
            out.append("no connect.policy: unknown operations are writes, DELETE is consent; "
                       "add globs for any endpoint that bills, sends or deletes")
        if not c.get("credential_ref") and c.get("auth_scheme") != "none":
            out.append("no credential_ref: you will pass --token-ref when connecting")
        if "smoke" not in data:
            out.append("no smoke entry: `lotr setup` cannot test a REST connection without one "
                       '(add "smoke": {"op": "GET /a-harmless-read"})')
    if kind == "mcp-stdio":
        cmd = c.get("command") or []
        if c.get("bridge"):
            out.append("BRIDGE: this runs a third-party bridge (" + " ".join(cmd[:3]) + " ...) that "
                       "does its own sign-in and keeps the token OUTSIDE LOTR's secrets store "
                       "(LOTR cannot seal, rotate or revoke it). Use it only for a server with no "
                       "streamable HTTP endpoint.")
        if "runtime" not in c:
            out.append(f"no connect.runtime: assumed {runtimes.infer(cmd)}; name it so a missing "
                       "program is explained before launch")
        if cmd and cmd[0] in ("npx", "bunx", "pnpm") and not any(
                re.search(r"@\d", x) for x in cmd[1:]):
            out.append("the package version is not pinned (name@1.2.3): every start runs whatever "
                       "is newest")
        if cmd and cmd[0] == "uvx" and not any("==" in x or re.search(r"@\d", x) for x in cmd[1:]):
            out.append("the package version is not pinned (name==1.2.3)")
        if cmd and cmd[0] in ("docker", "podman") and any(x.endswith(":latest") or
                                                          (":" not in x.rsplit("/", 1)[-1] and
                                                           "." not in x and "/" in x and
                                                           not x.startswith("-")) for x in cmd[1:]):
            out.append("the image is not pinned to a tag or digest")
    if kind in ("mcp-oauth", "mcp-http", "mcp-stdio") and "smoke" not in data:
        out.append("no smoke entry: `lotr setup` will pick a read tool that needs no input, if any")
    if kind == "mcp-oauth" and c.get("client_registration") == "preregistered" and \
            "app_setup_url" not in c:
        out.append("preregistered but no app_setup_url: say where the user creates the app")
    if not data.get("docs"):
        out.append("no docs URL: add the vendor's documentation page")
    return out


def facts_check(arg, home, template=None):
    """Check a fact sheet (a path), against its template when it can be found."""
    p = Path(arg).expanduser()
    if not p.is_file():
        raise GatewayError("missing_file", f"no fact sheet file {arg!r}")
    try:
        sheet = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return {"ok": False, "file": str(p), "problems": [f"not valid JSON: {e}"], "warnings": []}
    tpl = None
    tname = template or (sheet.get("template") if isinstance(sheet, dict) else None)
    if tname:
        cand = []
        if template and Path(template).expanduser().is_file():
            cand = [Path(template).expanduser()]
        else:
            cand = [p.parent.parent / "templates" / f"{tname}.json", p.parent / f"{tname}.json"]
        for c in cand:
            if c.is_file():
                try:
                    tpl = json.loads(c.read_text(encoding="utf-8"))
                except ValueError:
                    pass
                break
        if tpl is None:
            try:
                tpl = json.loads(find_template_file(tname, home).read_text(encoding="utf-8"))
            except (GatewayError, ValueError, OSError):
                tpl = None
    r = facts_mod.check(sheet, tpl)
    r.update(file=str(p), template_found=tpl is not None)
    if tpl is None:
        r["warnings"].append("the template was not found, so the sheet was not compared with it")
    return r


def list_templates(home):
    out = []
    for d, where in ((ct.SHIPPED_CONNECTIONS, "shipped"), (ct.BUNDLED_CATALOG / "templates", "bundled"),
                     (ct.catalog_dir(home), "catalog"), (Path(home) / "connections", "yours")):
        if not d.is_dir():
            continue
        for f in ct.template_files(d, home):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({"name": data.get("name"), "kind": data.get("kind"),
                            "summary": data.get("summary"), "where": where, "file": str(f),
                            "archetype": data.get("archetype"),
                            "is_archetype": data.get("archetype") == data.get("name")})
            except (ValueError, UnicodeDecodeError):
                out.append({"name": f.stem, "where": where, "file": str(f), "kind": "unreadable"})
    return {"ok": True, "templates": out}
